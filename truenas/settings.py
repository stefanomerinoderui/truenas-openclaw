"""The non-secret generator settings, their JSON form and the derived AppConfig.

``Settings`` holds everything the interactive prompts ask except secrets. It can
be loaded from / saved to a JSON config file, and combined with the secrets
(read from an env file or typed in) into the ``AppConfig`` the Compose builder
consumes.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from compose_builder import (
    CONTAINER_PORT,
    DEFAULT_IMAGE,
    DEFAULT_PACKAGES,
    AppConfig,
    ExtraNetwork,
    Secrets,
    Storage,
    TrustedProxy,
    format_host,
)
from envfile import SECRET_VARIABLES
from validators import (
    parse_bind_addresses,
    validate_bind_addresses,
    validate_dataset_path,
    validate_header_name,
    validate_header_names,
    validate_hostname,
    validate_hosts,
    validate_image,
    validate_network_name,
    validate_optional_trusted_proxies,
    validate_port,
    validate_required,
    validate_required_urls,
    validate_urls,
)

DEFAULT_PROXY_NETWORK = "ix-nginx-proxy-manager_default"
DEFAULT_PROXY_HOSTNAME = "openclaw"
DEFAULT_PUBLIC_URL = "https://openclaw.example.com"
DEFAULT_HOSTS = "truenas.local"
DEFAULT_STORAGE_PATH = "/mnt/tank/apps/openclaw"

ImageSource = Literal["inline", "prebuilt"]
NetworkMode = Literal["proxy", "bridge", "host"]

IMAGE_SOURCES = ("inline", "prebuilt")
NETWORK_MODES = ("proxy", "bridge", "host")
STORAGE_KINDS = ("host", "volumes")

TOP_LEVEL_KEYS = frozenset(
    {
        "image_source",
        "image",
        "network_mode",
        "port",
        "bind_addresses",
        "hosts",
        "extra_origins",
        "public_urls",
        "proxy_network",
        "trusted_proxies",
        "trusted_proxy",
        "storage",
        "timezone",
        "packages",
        "skip_update",
    }
)
PROXY_NETWORK_KEYS = frozenset({"name", "hostname"})
STORAGE_KEYS = frozenset({"kind", "base_path"})
TRUSTED_PROXY_KEYS = frozenset(
    {
        "proxies",
        "user_header",
        "required_headers",
        "allow_users",
        "allow_loopback",
        "device_auto_approve",
        "identity_scopes",
    }
)
DEVICE_AUTO_APPROVE_KEYS = frozenset({"enabled", "scopes"})
SECRET_LOOKING_KEY = re.compile(r"token|key|secret|password", re.IGNORECASE)


@dataclass(frozen=True)
class Settings:
    image_source: ImageSource
    network_mode: NetworkMode
    storage: Storage
    prebuilt_image: str = ""
    port: int = CONTAINER_PORT
    bind_addresses: tuple[str, ...] = ("0.0.0.0",)
    hosts: tuple[str, ...] = ()
    extra_origins: tuple[str, ...] = ()
    public_urls: tuple[str, ...] = ()
    proxy_network: ExtraNetwork | None = None
    trusted_proxies: tuple[str, ...] = ()
    trusted_proxy: TrustedProxy | None = None
    timezone: str = "UTC"
    packages: tuple[str, ...] = tuple(DEFAULT_PACKAGES.split())
    skip_update: bool = False


Issue = tuple[str, str]  # (dotted JSON path, or "" for the whole file; message)


def format_issue(issue: Issue) -> str:
    path, message = issue
    return f"{path}: {message}" if path else message


class ConfigError(ValueError):
    """The JSON config is unreadable or invalid; lists every problem found."""

    def __init__(self, issues: list[Issue]) -> None:
        self.issues = issues
        super().__init__("\n".join(f"- {problem}" for problem in self.problems))

    @property
    def problems(self) -> list[str]:
        return [format_issue(issue) for issue in self.issues]


def default_timezone() -> str:
    if os.environ.get("TZ"):
        return os.environ["TZ"]
    localtime = Path("/etc/localtime")
    if localtime.is_symlink():
        target = str(localtime.resolve())
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    return "UTC"


def build_origins(
    hosts: Iterable[str], port: int, extra: Iterable[str]
) -> tuple[str, ...]:
    origins = [f"http://{format_host(host)}:{port}" for host in hosts]
    origins += [f"http://localhost:{port}", f"http://127.0.0.1:{port}"]
    origins += [url.rstrip("/") for url in extra]
    return tuple(dict.fromkeys(origins))  # de-duplicate, keep order


def build_app_config(settings: Settings, secrets: Secrets) -> AppConfig:
    """Combine the settings with the secrets into what the Compose builder needs."""
    if settings.network_mode == "proxy":
        port = CONTAINER_PORT  # nothing is published; the proxy talks to the container
        origins = build_origins([], port, settings.public_urls)
    else:
        port = settings.port
        origins = build_origins(settings.hosts, port, settings.extra_origins)
    return AppConfig(
        image_source=settings.image_source,
        prebuilt_image=settings.prebuilt_image,
        network_mode=settings.network_mode,
        host_port=port,
        bind_addresses=settings.bind_addresses,
        extra_network=settings.proxy_network,
        trusted_proxies=settings.trusted_proxies,
        trusted_proxy=settings.trusted_proxy,
        storage=settings.storage,
        secrets=secrets,
        control_ui_origins=origins,
        timezone=settings.timezone,
        skip_update=settings.skip_update,
        packages=" ".join(settings.packages),
    )


# --- JSON out ----------------------------------------------------------------


def settings_to_dict(settings: Settings) -> dict[str, Any]:
    """The JSON form: only the keys that matter for the chosen modes."""
    data: dict[str, Any] = {"image_source": settings.image_source}
    if settings.image_source == "prebuilt":
        data["image"] = settings.prebuilt_image
    data["network_mode"] = settings.network_mode

    network = settings.proxy_network
    proxy_network = (
        {"name": network.name, "hostname": network.hostname} if network else None
    )
    if settings.network_mode == "proxy":
        if proxy_network:
            data["proxy_network"] = proxy_network
        data["public_urls"] = list(settings.public_urls)
    else:
        data["port"] = settings.port
        if settings.network_mode == "bridge":
            data["bind_addresses"] = list(settings.bind_addresses)
            if proxy_network:
                data["proxy_network"] = proxy_network
        data["hosts"] = list(settings.hosts)
        data["extra_origins"] = list(settings.extra_origins)

    if settings.trusted_proxies:
        data["trusted_proxies"] = list(settings.trusted_proxies)
    if settings.trusted_proxy:
        data["trusted_proxy"] = _trusted_proxy_to_dict(settings.trusted_proxy)

    data["storage"] = {"kind": settings.storage.kind}
    if settings.storage.kind == "host":
        data["storage"]["base_path"] = settings.storage.base_path
    data["timezone"] = settings.timezone
    data["packages"] = list(settings.packages)
    data["skip_update"] = settings.skip_update
    return data


def _trusted_proxy_to_dict(proxy: TrustedProxy) -> dict[str, Any]:
    data: dict[str, Any] = {
        "user_header": proxy.user_header,
        "required_headers": list(proxy.required_headers),
        "allow_users": list(proxy.allow_users),
    }
    # Advanced options are only written when they differ from the defaults.
    if proxy.allow_loopback:
        data["allow_loopback"] = True
    if proxy.device_auto_approve or proxy.device_scopes:
        data["device_auto_approve"] = {"enabled": proxy.device_auto_approve}
        if proxy.device_scopes:
            data["device_auto_approve"]["scopes"] = list(proxy.device_scopes)
    if proxy.identity_scopes:
        data["identity_scopes"] = {
            identity: list(scopes) for identity, scopes in proxy.identity_scopes.items()
        }
    return data


def save_settings(settings: Settings, path: Path) -> None:
    text = json.dumps(settings_to_dict(settings), indent=2) + "\n"
    path.write_text(text, encoding="utf-8")


# --- JSON in -----------------------------------------------------------------

_REQUIRED: Any = object()  # marks a key without default


class _Reader:
    """Typed access to one JSON object that collects every problem it finds."""

    def __init__(
        self, data: dict[str, Any], issues: list[Issue], prefix: str = ""
    ) -> None:
        self.data = data
        self.issues = issues
        self.prefix = prefix

    def fail(self, key: str, message: str) -> None:
        self.issues.append((f"{self.prefix}{key}", message))

    def _get(self, key: str, default: Any) -> Any:
        if key in self.data:
            return self.data[key]
        if default is _REQUIRED:
            self.fail(key, "required")
        return default if default is not _REQUIRED else None

    def _validate(
        self, key: str, text: str, validate: Callable[[str], bool | str] | None
    ) -> bool:
        result = validate(text) if validate else True
        if result is not True:
            self.fail(key, str(result))
        return result is True

    def string(
        self,
        key: str,
        default: Any = _REQUIRED,
        validate: Callable[[str], bool | str] | None = None,
    ) -> str:
        value = self._get(key, default)
        if not isinstance(value, str):
            if key in self.data:
                self.fail(key, "must be a string")
            return ""
        value = value.strip()
        self._validate(key, value, validate)
        return value

    def integer(
        self,
        key: str,
        default: int,
        validate: Callable[[str], bool | str] | None = None,
    ) -> int:
        value = self._get(key, default)
        if isinstance(value, bool) or not isinstance(value, int):
            self.fail(key, "must be an integer")
            return default
        self._validate(key, str(value), validate)
        return value

    def boolean(self, key: str, default: bool) -> bool:
        value = self._get(key, default)
        if not isinstance(value, bool):
            self.fail(key, "must be true or false")
            return default
        return value

    def choice(self, key: str, options: tuple[str, ...], default: str) -> str:
        value = self._get(key, default)
        if value not in options:
            self.fail(key, f"must be one of: {', '.join(options)}")
            return default
        return value

    def strings(
        self,
        key: str,
        default: Any = _REQUIRED,
        validate: Callable[[str], bool | str] | None = None,
    ) -> tuple[str, ...]:
        value = self._get(key, default)
        if not isinstance(value, (list, tuple)) or not all(
            isinstance(item, str) for item in value
        ):
            if key in self.data:
                self.fail(key, "must be a list of strings")
            return ()
        items = tuple(item.strip() for item in value)
        if any(not item or re.search(r"[\s,]", item) for item in items):
            self.fail(key, "entries must not be empty or contain spaces or commas")
            return ()
        if not self._validate(key, ",".join(items), validate):
            return ()  # callers may normalise the items and need valid input
        return items

    def section(self, key: str, allowed: frozenset[str]) -> _Reader | None:
        """A nested object, or None if absent (or invalid, which is reported)."""
        value = self.data.get(key)
        if value is None:
            return None
        if not isinstance(value, dict):
            self.fail(key, "must be an object")
            return None
        reader = _Reader(value, self.issues, prefix=f"{self.prefix}{key}.")
        reader.reject_unknown(allowed)
        return reader

    def reject_unknown(self, allowed: frozenset[str]) -> None:
        for key in self.data:
            if key in allowed:
                continue
            if SECRET_LOOKING_KEY.search(key):
                self.fail(
                    key,
                    "secrets don't belong in the JSON config; put them in the env "
                    f"file ({', '.join(SECRET_VARIABLES)})",
                )
            else:
                self.fail(key, f"unknown setting (valid: {', '.join(sorted(allowed))})")


def _read_proxy_network(top: _Reader, *, required: bool) -> ExtraNetwork | None:
    section = top.section("proxy_network", PROXY_NETWORK_KEYS)
    if section is None:
        if "proxy_network" in top.data and top.data["proxy_network"] is not None:
            return None  # invalid, already reported
        if required:
            return ExtraNetwork(DEFAULT_PROXY_NETWORK, DEFAULT_PROXY_HOSTNAME)
        return None
    return ExtraNetwork(
        section.string("name", DEFAULT_PROXY_NETWORK, validate_network_name),
        section.string("hostname", DEFAULT_PROXY_HOSTNAME, validate_hostname),
    )


def _read_storage(top: _Reader) -> Storage:
    section = top.section("storage", STORAGE_KEYS)
    if section is None:
        if "storage" not in top.data:
            top.fail("storage", 'required, e.g. {"kind": "volumes"}')
        return Storage("volumes")
    if section.choice("kind", STORAGE_KINDS, "host") == "volumes":
        return Storage("volumes")
    base_path = section.string("base_path", _REQUIRED, validate_dataset_path)
    return Storage("host", base_path.rstrip("/"))


def _read_identity_scopes(section: _Reader) -> dict[str, tuple[str, ...]]:
    value = section.data.get("identity_scopes")
    if value is None:
        return {}
    if not isinstance(value, dict):
        section.fail(
            "identity_scopes", "must be an object mapping an identity to its scopes"
        )
        return {}
    scopes = _Reader(value, section.issues, prefix=f"{section.prefix}identity_scopes.")
    return {identity: scopes.strings(identity, ()) for identity in value}


def _read_trusted_proxy(top: _Reader) -> dict[str, Any] | None:
    """The TrustedProxy arguments, or None when trusted-proxy auth is off.

    Returns the arguments instead of the object so that every problem can be
    collected before TrustedProxy's own checks run.
    """
    section = top.section("trusted_proxy", TRUSTED_PROXY_KEYS)
    if section is None:
        return None
    device = section.section("device_auto_approve", DEVICE_AUTO_APPROVE_KEYS)
    required_headers = section.strings("required_headers", (), validate_header_names)
    return {
        # Configs from before trusted_proxies was a top-level key.
        "legacy_proxies": section.strings(
            "proxies", (), validate_optional_trusted_proxies
        ),
        "user_header": section.string(
            "user_header", _REQUIRED, validate_header_name
        ).lower(),
        "required_headers": tuple(header.lower() for header in required_headers),
        "allow_users": section.strings("allow_users", ()),
        "allow_loopback": section.boolean("allow_loopback", False),
        "device_auto_approve": (
            device.boolean("enabled", False) if device is not None else False
        ),
        "device_scopes": device.strings("scopes", ()) if device is not None else (),
        "identity_scopes": _read_identity_scopes(section),
    }


def parse_settings(data: object) -> Settings:
    """Validate parsed JSON and turn it into Settings, or raise ConfigError."""
    if not isinstance(data, dict):
        raise ConfigError([("", "the top level must be a JSON object")])
    issues: list[Issue] = []
    top = _Reader(data, issues)
    top.reject_unknown(TOP_LEVEL_KEYS)

    image_source = top.choice("image_source", IMAGE_SOURCES, "inline")
    prebuilt_image = ""
    if image_source == "prebuilt":
        prebuilt_image = top.string("image", DEFAULT_IMAGE, validate_image)

    mode = top.choice("network_mode", NETWORK_MODES, "proxy")
    port = CONTAINER_PORT
    bind_addresses = ("0.0.0.0",)
    hosts: tuple[str, ...] = ()
    extra_origins: tuple[str, ...] = ()
    public_urls: tuple[str, ...] = ()
    proxy_network = None
    if mode == "proxy":
        proxy_network = _read_proxy_network(top, required=True)
        public_urls = top.strings("public_urls", _REQUIRED, validate_required_urls)
    else:
        port = top.integer("port", CONTAINER_PORT, validate_port)
        if mode == "bridge":
            bind_addresses = parse_bind_addresses(
                ",".join(
                    top.strings("bind_addresses", ("0.0.0.0",), validate_bind_addresses)
                )
            ) or ("0.0.0.0",)
            proxy_network = _read_proxy_network(top, required=False)
        hosts = top.strings("hosts", _REQUIRED, validate_hosts)
        extra_origins = top.strings("extra_origins", (), validate_urls)

    trusted_proxies = top.strings(
        "trusted_proxies", (), validate_optional_trusted_proxies
    )
    trusted_proxy = _read_trusted_proxy(top)
    if trusted_proxy is not None:
        legacy_proxies = trusted_proxy.pop("legacy_proxies")
        trusted_proxies = trusted_proxies or legacy_proxies
        already_reported = any(path == "trusted_proxies" for path, _ in issues)
        if not trusted_proxies and not already_reported:
            top.fail(
                "trusted_proxies",
                "required with trusted-proxy auth: the IP(s) of your proxy",
            )
    storage = _read_storage(top)
    timezone = top.string("timezone", default_timezone(), validate_required)
    packages = top.strings(
        "packages", tuple(DEFAULT_PACKAGES.split()), validate_required
    )
    skip_update = top.boolean("skip_update", False)

    if issues:
        raise ConfigError(issues)
    return Settings(
        image_source=image_source,
        network_mode=mode,
        storage=storage,
        prebuilt_image=prebuilt_image,
        port=port,
        bind_addresses=bind_addresses,
        hosts=hosts,
        extra_origins=extra_origins,
        public_urls=public_urls,
        proxy_network=proxy_network,
        trusted_proxies=trusted_proxies,
        trusted_proxy=TrustedProxy(**trusted_proxy) if trusted_proxy else None,
        timezone=timezone,
        packages=packages,
        skip_update=skip_update,
    )


def read_config_json(path: Path) -> object:
    """The parsed JSON of a config file, without validating it.

    Lets the editor open a config that has invalid values so they can be fixed.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as err:
        raise ConfigError([("", f"cannot read {path}: {err}")]) from err
    try:
        return json.loads(text)
    except json.JSONDecodeError as err:
        raise ConfigError([("", f"{path} is not valid JSON: {err}")]) from err


def load_settings(path: Path) -> Settings:
    return parse_settings(read_config_json(path))


def default_settings_dict() -> dict[str, Any]:
    """The starting point of a new config: the recommended reverse-proxy setup."""
    return {
        "image_source": "inline",
        "network_mode": "proxy",
        "proxy_network": {
            "name": DEFAULT_PROXY_NETWORK,
            "hostname": DEFAULT_PROXY_HOSTNAME,
        },
        "public_urls": [DEFAULT_PUBLIC_URL],
        "port": CONTAINER_PORT,
        "bind_addresses": ["0.0.0.0"],
        "hosts": [DEFAULT_HOSTS],
        "extra_origins": [],
        "storage": {"kind": "host", "base_path": DEFAULT_STORAGE_PATH},
        "timezone": default_timezone(),
        "packages": DEFAULT_PACKAGES.split(),
        "skip_update": False,
    }
