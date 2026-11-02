"""Build a self-contained Docker Compose YAML for TrueNAS "Install via YAML".

TrueNAS only receives the pasted YAML, without the repository files, so in
"inline" mode the Dockerfile is embedded with ``build.dockerfile_inline`` and the
entrypoint script with a ``configs`` entry. In "prebuilt" mode the stack uses an
image that was already pushed to a registry.
"""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

import yaml

CONTAINER_PORT = 18789
APP_UID = 1000
LOCAL_IMAGE = "truenas-openclaw:latest"
DEFAULT_IMAGE = "ghcr.io/stefanomerinoderui/truenas-openclaw:latest"
ENTRYPOINT_TARGET = "/opt/openclaw/docker-entrypoint.sh"
ENTRYPOINT_CONFIG = "openclaw-entrypoint"
DEFAULT_PACKAGES = "openclaw @anthropic-ai/claude-code"

# Volume name -> mount point inside the container.
MOUNTS = {
    "tools": "/home/node/.local",
    "openclaw": "/home/node/.openclaw",
    "claude": "/home/node/.claude",
    "gemini": "/home/node/.gemini",
}

COPY_ENTRYPOINT_PATTERN = re.compile(
    r"^COPY\b.*docker-entrypoint\.sh.*\n", re.MULTILINE
)


@dataclass(frozen=True)
class ExtraNetwork:
    """Pre-existing Docker network the gateway joins (e.g. a reverse proxy's)."""

    name: str
    hostname: str


@dataclass(frozen=True)
class TrustedProxy:
    """Delegate gateway authentication to a reverse proxy (``trusted-proxy`` mode)."""

    proxies: tuple[str, ...]
    user_header: str
    required_headers: tuple[str, ...] = ()
    allow_users: tuple[str, ...] = ()
    allow_loopback: bool = False
    device_auto_approve: bool = False
    device_scopes: tuple[str, ...] = ()
    identity_scopes: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.proxies:
            raise ValueError("trusted-proxy auth needs at least one trusted proxy")
        if not self.user_header:
            raise ValueError("trusted-proxy auth needs the user header name")


@dataclass(frozen=True)
class Storage:
    kind: Literal["volumes", "host"]
    base_path: str = ""


@dataclass(frozen=True)
class Secrets:
    # Empty with trusted-proxy auth: the gateway refuses to start with both.
    gateway_token: str
    anthropic_api_key: str = ""
    antigravity_api_key: str = ""
    # Only used with trusted-proxy auth, for internal clients that bypass the proxy.
    gateway_password: str = ""


@dataclass(frozen=True)
class AppConfig:
    image_source: Literal["inline", "prebuilt"]
    network_mode: Literal["proxy", "bridge", "host"]
    host_port: int
    storage: Storage
    secrets: Secrets
    control_ui_origins: tuple[str, ...]
    prebuilt_image: str = ""
    bind_addresses: tuple[str, ...] = ("0.0.0.0",)
    extra_network: ExtraNetwork | None = None
    trusted_proxy: TrustedProxy | None = None
    timezone: str = "UTC"
    skip_update: bool = False
    packages: str = DEFAULT_PACKAGES

    def __post_init__(self) -> None:
        if self.extra_network and self.network_mode == "host":
            raise ValueError("host network mode cannot join another Docker network")
        if self.network_mode == "proxy" and not self.extra_network:
            raise ValueError("proxy mode requires the proxy network to join")
        if self.network_mode == "bridge" and not self.bind_addresses:
            raise ValueError("at least one bind address is required in bridge mode")
        if self.trusted_proxy and self.secrets.gateway_token:
            raise ValueError(
                "the gateway refuses to start with trusted-proxy auth and a gateway token"
            )
        if not self.trusted_proxy and not self.secrets.gateway_token:
            raise ValueError("a gateway token is required without trusted-proxy auth")

    @property
    def gateway_port(self) -> int:
        """Port the gateway listens on inside the container."""
        return self.host_port if self.network_mode == "host" else CONTAINER_PORT

    @property
    def image(self) -> str:
        return self.prebuilt_image if self.image_source == "prebuilt" else LOCAL_IMAGE


def is_ipv6(host: str) -> bool:
    try:
        return ipaddress.ip_address(host.strip("[]")).version == 6
    except ValueError:
        return False


def format_host(host: str) -> str:
    """Bracket IPv6 literals so they can be followed by ``:port``."""
    return f"[{host.strip('[]')}]" if is_ipv6(host) else host


def escape_interpolation(text: str) -> str:
    """Escape ``$`` so Compose does not treat file contents as variables."""
    return text.replace("$", "$$")


def inline_dockerfile(dockerfile: str) -> str:
    """Drop the entrypoint COPY: there is no build context on TrueNAS."""
    result, count = COPY_ENTRYPOINT_PATTERN.subn("", dockerfile)
    if count != 1:
        raise ValueError(
            "expected exactly one COPY of docker-entrypoint.sh in the Dockerfile"
        )
    return result


def volume_mounts(storage: Storage) -> list[str]:
    if storage.kind == "host":
        base = storage.base_path.rstrip("/")
        return [f"{base}/{name}:{target}" for name, target in MOUNTS.items()]
    return [f"{name}:{target}" for name, target in MOUNTS.items()]


def config_overrides(config: AppConfig) -> list[dict[str, Any]]:
    """OpenClaw settings re-applied on every deploy (``openclaw config set`` batch).

    Always sets the auth mode, so switching between token and trusted-proxy auth
    in a regenerated YAML never leaves the previous mode behind.
    """
    proxy = config.trusted_proxy
    if proxy is None:
        return [{"path": "gateway.auth.mode", "value": "token"}]

    prefix = "gateway.auth.trustedProxy"
    overrides: list[dict[str, Any]] = [
        {"path": "gateway.auth.mode", "value": "trusted-proxy"},
        {"path": "gateway.trustedProxies", "value": list(proxy.proxies)},
        {"path": f"{prefix}.userHeader", "value": proxy.user_header},
        {"path": f"{prefix}.requiredHeaders", "value": list(proxy.required_headers)},
        {"path": f"{prefix}.allowUsers", "value": list(proxy.allow_users)},
        {"path": f"{prefix}.allowLoopback", "value": proxy.allow_loopback},
        {
            "path": f"{prefix}.deviceAutoApprove.enabled",
            "value": proxy.device_auto_approve,
        },
    ]
    if proxy.device_scopes:  # otherwise OpenClaw's own default scopes apply
        overrides.append(
            {
                "path": f"{prefix}.deviceAutoApprove.scopes",
                "value": list(proxy.device_scopes),
            }
        )
    overrides.append(
        {
            "path": "gateway.auth.identityScopes",
            "value": {
                identity: list(scopes)
                for identity, scopes in proxy.identity_scopes.items()
            },
        }
    )
    return overrides


def environment(config: AppConfig) -> dict[str, str]:
    env = {"TZ": config.timezone}
    if not config.trusted_proxy:
        env["OPENCLAW_GATEWAY_TOKEN"] = config.secrets.gateway_token
    env.update(
        {
            "OPENCLAW_CONTROL_UI_ORIGINS": json.dumps(
                list(config.control_ui_origins), separators=(",", ":")
            ),
            "OPENCLAW_CONFIG_OVERRIDES": json.dumps(
                config_overrides(config), separators=(",", ":")
            ),
            "PNPM_GLOBAL_PACKAGES": config.packages,
            "SKIP_UPDATE": "1" if config.skip_update else "0",
        }
    )
    if config.trusted_proxy and config.secrets.gateway_password:
        env["OPENCLAW_GATEWAY_PASSWORD"] = config.secrets.gateway_password
    if config.secrets.anthropic_api_key:
        env["ANTHROPIC_API_KEY"] = config.secrets.anthropic_api_key
    if config.secrets.antigravity_api_key:
        env["ANTIGRAVITY_API_KEY"] = config.secrets.antigravity_api_key
    # Values are literal: user input such as API keys may contain "$".
    return {key: escape_interpolation(value) for key, value in env.items()}


def _base_service(config: AppConfig, mounts: list[str]) -> dict:
    service: dict = {
        "image": config.image,
        "init": False,
        "environment": environment(config),
        "volumes": list(mounts),
    }
    if config.image_source == "inline":
        # Config files are mounted read-only and non-executable, so run via sh.
        service["entrypoint"] = ["/sbin/tini", "--", "sh", ENTRYPOINT_TARGET]
        service["configs"] = [
            {"source": ENTRYPOINT_CONFIG, "target": ENTRYPOINT_TARGET}
        ]
        # Only the update stage builds; the others reuse its image.
        service["pull_policy"] = "never"
    if config.network_mode == "host":
        service["network_mode"] = "host"
    return service


def _permissions_service(config: AppConfig, mounts: list[str]) -> dict:
    """Root one-shot that hands host-path directories to the node user."""
    targets = " ".join(MOUNTS.values())
    # Only touches directories not yet owned by the app user, so restarts stay fast.
    chown_script = f"find {targets} -maxdepth 0 ! -user {APP_UID} -exec chown -R {APP_UID}:{APP_UID} {{}} +"
    service = _base_service(config, mounts)
    service.update(
        {
            "user": "0:0",
            "entrypoint": ["sh", "-c", chown_script],
            "restart": "no",
        }
    )
    service.pop("configs", None)
    return service


def _stage_service(
    config: AppConfig, mounts: list[str], stage: str, after: str | None
) -> dict:
    service = _base_service(config, mounts)
    service["command"] = [stage]
    service["restart"] = "no"
    if after:
        service["depends_on"] = {after: {"condition": "service_completed_successfully"}}
    return service


def _gateway_service(config: AppConfig, mounts: list[str]) -> dict:
    service = _base_service(config, mounts)
    service["environment"]["RUN_SETUP"] = "0"
    service["restart"] = "unless-stopped"
    service["depends_on"] = {"doctor": {"condition": "service_completed_successfully"}}
    service["command"] = [
        "openclaw",
        "gateway",
        "--bind",
        "lan",
        "--port",
        str(config.gateway_port),
    ]
    if config.network_mode == "bridge":
        service["ports"] = [
            f"{format_host(address)}:{config.host_port}:{CONTAINER_PORT}"
            for address in config.bind_addresses
        ]
    if config.extra_network:
        extra = config.extra_network
        service["hostname"] = extra.hostname
        # Docker DNS resolves aliases, not the hostname, on user-defined networks.
        # Listing only this network keeps the gateway off the project's own one.
        service["networks"] = {extra.name: {"aliases": [extra.hostname]}}
    service["healthcheck"] = {
        "test": [
            "CMD",
            "wget",
            "-q",
            "--spider",
            f"http://127.0.0.1:{config.gateway_port}/healthz",
        ],
        "interval": "30s",
        "timeout": "5s",
        "start_period": "5m",
        "retries": 3,
    }
    return service


def build_compose(config: AppConfig, dockerfile: str, entrypoint: str) -> dict:
    """Return the Compose document as a plain dict."""
    mounts = volume_mounts(config.storage)
    services: dict = {}

    first_stage_after = None
    if config.storage.kind == "host":
        services["permissions"] = _permissions_service(config, mounts)
        first_stage_after = "permissions"

    update = _stage_service(config, mounts, "update", first_stage_after)
    if config.image_source == "inline":
        update["build"] = {
            "context": ".",
            "dockerfile_inline": escape_interpolation(inline_dockerfile(dockerfile)),
        }
        update["pull_policy"] = "build"
        if "permissions" in services:
            # The permissions stage runs first, so it has to build the image.
            services["permissions"]["build"] = update.pop("build")
            services["permissions"]["pull_policy"] = "build"
            update["pull_policy"] = "never"
    else:
        update["pull_policy"] = "always"

    services["update"] = update
    services["doctor"] = _stage_service(config, mounts, "doctor", "update")
    services["openclaw"] = _gateway_service(config, mounts)

    compose: dict = {"services": services}
    if config.image_source == "inline":
        compose["configs"] = {
            ENTRYPOINT_CONFIG: {"content": escape_interpolation(entrypoint)}
        }
    if config.storage.kind == "volumes":
        compose["volumes"] = {name: {} for name in MOUNTS}
    if config.extra_network:
        compose["networks"] = {config.extra_network.name: {"external": True}}
    return compose


class _LiteralDumper(yaml.SafeDumper):
    """Multi-line strings as ``|`` blocks, and no anchors/aliases."""

    def ignore_aliases(self, data: object) -> bool:
        return True


def _represent_str(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_LiteralDumper.add_representer(str, _represent_str)


def dump_yaml(compose: dict) -> str:
    return yaml.dump(compose, Dumper=_LiteralDumper, sort_keys=False, width=1000)
