"""Build a self-contained Docker Compose YAML for TrueNAS "Install via YAML".

TrueNAS only receives the pasted YAML, without the repository files, so in
"inline" mode the Dockerfile is embedded with ``build.dockerfile_inline`` and the
entrypoint script with a ``configs`` entry. In "prebuilt" mode the stack uses an
image that was already pushed to a registry.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal

import yaml

CONTAINER_PORT = 18789
APP_UID = 1000
LOCAL_IMAGE = "truenas-openclaw:latest"
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
class Storage:
    kind: Literal["volumes", "host"]
    base_path: str = ""


@dataclass(frozen=True)
class Secrets:
    gateway_token: str
    anthropic_api_key: str = ""
    antigravity_api_key: str = ""


@dataclass(frozen=True)
class AppConfig:
    image_source: Literal["inline", "prebuilt"]
    network_mode: Literal["bridge", "host"]
    host_port: int
    storage: Storage
    secrets: Secrets
    control_ui_origins: tuple[str, ...]
    prebuilt_image: str = ""
    bind_address: str = "0.0.0.0"
    timezone: str = "UTC"
    skip_update: bool = False
    packages: str = DEFAULT_PACKAGES

    @property
    def gateway_port(self) -> int:
        """Port the gateway listens on inside the container."""
        return self.host_port if self.network_mode == "host" else CONTAINER_PORT

    @property
    def image(self) -> str:
        return self.prebuilt_image if self.image_source == "prebuilt" else LOCAL_IMAGE


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


def environment(config: AppConfig) -> dict[str, str]:
    env = {
        "TZ": config.timezone,
        "OPENCLAW_GATEWAY_TOKEN": config.secrets.gateway_token,
        "OPENCLAW_CONTROL_UI_ORIGINS": json.dumps(
            list(config.control_ui_origins), separators=(",", ":")
        ),
        "PNPM_GLOBAL_PACKAGES": config.packages,
        "SKIP_UPDATE": "1" if config.skip_update else "0",
    }
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
            f"{config.bind_address}:{config.host_port}:{CONTAINER_PORT}"
        ]
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
