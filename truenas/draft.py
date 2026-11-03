"""The editor's working state, independent of the TUI toolkit.

The TUI keeps every form field as text or a boolean, keyed by the dotted JSON
path of the setting it edits (``proxy_network.name``, ``trusted_proxy.user_header``
...). This module converts between those form values and the JSON config,
validates them with ``settings.parse_settings`` (so the rules live in one place),
and maps each problem back to its section and field.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Mapping
from typing import Any

from compose_builder import (
    CONTAINER_PORT,
    AppConfig,
    ExtraNetwork,
    Secrets,
    format_host,
)
from envfile import (
    ANTHROPIC_KEY_VAR,
    ANTIGRAVITY_KEY_VAR,
    GATEWAY_PASSWORD_VAR,
    GATEWAY_TOKEN_VAR,
)
from settings import (
    ConfigError,
    Issue,
    Settings,
    default_settings_dict,
    parse_settings,
)
from validators import parse_list

FormValues = dict[str, Any]  # str for text fields, bool for switches

SECTIONS = (
    ("image", "Image"),
    ("network", "Network"),
    ("auth", "Authentication"),
    ("storage", "Storage"),
    ("environment", "Environment"),
    ("secrets", "Secrets"),
    ("review", "Review & generate"),
)

# Form fields: dotted path -> kind ("text", "choice", "bool", "json", "secret").
# UI-only toggles (not JSON keys) are marked with a comment.
FIELDS: dict[str, str] = {
    "image_source": "choice",
    "image": "text",
    "network_mode": "choice",
    "bridge_extra_network": "bool",  # bridge mode: also join proxy_network
    "proxy_network.name": "text",
    "proxy_network.hostname": "text",
    "public_urls": "text",
    "port": "text",
    "bind_addresses": "text",
    "hosts": "text",
    "extra_origins": "text",
    "trusted_proxies": "text",
    "trusted_proxy.enabled": "bool",  # trusted-proxy auth on/off
    "trusted_proxy.user_header": "text",
    "trusted_proxy.required_headers": "text",
    "trusted_proxy.allow_users": "text",
    "trusted_proxy.allow_loopback": "bool",
    "trusted_proxy.device_auto_approve.enabled": "bool",
    "trusted_proxy.device_auto_approve.scopes": "text",
    "trusted_proxy.identity_scopes": "json",
    "storage.kind": "choice",
    "storage.base_path": "text",
    "timezone": "text",
    "packages": "text",
    "skip_update": "bool",
    "secrets.token": "secret",
    "secrets.anthropic": "secret",
    "secrets.antigravity": "secret",
    "secrets.password": "secret",
}

LIST_FIELDS = frozenset(
    {
        "public_urls",
        "bind_addresses",
        "hosts",
        "extra_origins",
        "trusted_proxies",
        "trusted_proxy.required_headers",
        "trusted_proxy.allow_users",
        "trusted_proxy.device_auto_approve.scopes",
        "packages",
    }
)

_SECTION_BY_PREFIX = (
    ("image_source", "image"),
    ("image", "image"),
    ("network_mode", "network"),
    ("bridge_extra_network", "network"),
    ("proxy_network", "network"),
    ("public_urls", "network"),
    ("port", "network"),
    ("bind_addresses", "network"),
    ("hosts", "network"),
    ("extra_origins", "network"),
    ("trusted_proxies", "auth"),
    ("trusted_proxy", "auth"),
    ("storage", "storage"),
    ("timezone", "environment"),
    ("packages", "environment"),
    ("skip_update", "environment"),
    ("secrets", "secrets"),
)

LIST_HINT = 'comma-separated or a JSON list, e.g. ["a", "b"]'


# --- config dict -> form ------------------------------------------------------


def _to_text(value: Any) -> str:
    """Show any JSON value in a text field, even invalid ones, so they can be fixed."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return ", ".join(value)
    return json.dumps(value)


def _section(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = data.get(key)
    return value if isinstance(value, Mapping) else {}


def values_from_dict(data: Mapping[str, Any]) -> FormValues:
    """Form values for a (possibly invalid) config; missing keys get the defaults."""
    defaults = default_settings_dict()

    def pick(key: str) -> Any:
        return data[key] if key in data else defaults.get(key)

    proxy_network = _section(data, "proxy_network") or defaults["proxy_network"]
    storage = {**defaults["storage"], **_section(data, "storage")}
    trusted = _section(data, "trusted_proxy")
    device = _section(trusted, "device_auto_approve")
    identity_scopes = trusted.get("identity_scopes")

    return {
        "image_source": _to_text(pick("image_source")),
        "image": _to_text(data.get("image", "")),
        "network_mode": _to_text(pick("network_mode")),
        "bridge_extra_network": isinstance(data.get("proxy_network"), Mapping)
        and data.get("network_mode") == "bridge",
        "proxy_network.name": _to_text(proxy_network.get("name", "")),
        "proxy_network.hostname": _to_text(proxy_network.get("hostname", "")),
        "public_urls": _to_text(pick("public_urls")),
        "port": _to_text(pick("port")),
        "bind_addresses": _to_text(pick("bind_addresses")),
        "hosts": _to_text(pick("hosts")),
        "extra_origins": _to_text(pick("extra_origins")),
        # Older configs kept the list under trusted_proxy.proxies.
        "trusted_proxies": _to_text(
            data.get("trusted_proxies") or trusted.get("proxies")
        ),
        "trusted_proxy.enabled": bool(trusted),
        "trusted_proxy.user_header": _to_text(
            trusted.get("user_header", "x-forwarded-user")
        ),
        "trusted_proxy.required_headers": _to_text(
            trusted.get("required_headers", ["x-forwarded-proto", "x-forwarded-host"])
        ),
        "trusted_proxy.allow_users": _to_text(trusted.get("allow_users")),
        "trusted_proxy.allow_loopback": trusted.get("allow_loopback") is True,
        "trusted_proxy.device_auto_approve.enabled": device.get("enabled") is True,
        "trusted_proxy.device_auto_approve.scopes": _to_text(device.get("scopes")),
        "trusted_proxy.identity_scopes": (
            json.dumps(identity_scopes, indent=2) if identity_scopes else ""
        ),
        "storage.kind": _to_text(storage.get("kind")),
        "storage.base_path": _to_text(storage.get("base_path", "")),
        "timezone": _to_text(pick("timezone")),
        "packages": _to_text(pick("packages")),
        "skip_update": pick("skip_update") is True,
    }


def secret_values(known: Mapping[str, str]) -> FormValues:
    """Secret fields pre-filled from the env file / environment."""
    return {
        "secrets.token": known.get(GATEWAY_TOKEN_VAR, ""),
        "secrets.anthropic": known.get(ANTHROPIC_KEY_VAR, ""),
        "secrets.antigravity": known.get(ANTIGRAVITY_KEY_VAR, ""),
        "secrets.password": known.get(GATEWAY_PASSWORD_VAR, ""),
    }


# --- form -> config dict -------------------------------------------------------


class _Builder:
    """Collects the JSON config from form values plus local parse issues."""

    def __init__(self, values: Mapping[str, Any]) -> None:
        self.values = values
        self.issues: list[Issue] = []

    def text(self, path: str) -> str:
        return str(self.values.get(path, "")).strip()

    def flag(self, path: str) -> bool:
        return self.values.get(path) is True

    def items(self, path: str) -> list[str] | str:
        try:
            return parse_list(self.text(path))
        except ValueError:
            self.issues.append((path, f"Enter values {LIST_HINT}"))
            return self.text(path)

    def port(self, path: str) -> int | str:
        text = self.text(path)
        if text.isdigit():
            return int(text)
        self.issues.append((path, "Enter a port between 1 and 65535"))
        return text

    def json_object(self, path: str) -> Any:
        text = self.text(path)
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            self.issues.append(
                (
                    path,
                    'Enter a JSON object, e.g. {"admin@example.com": ["operator.admin"]}',
                )
            )
            return None


def _trusted_proxy_dict(build: _Builder) -> dict[str, Any]:
    prefix = "trusted_proxy."
    data: dict[str, Any] = {
        "user_header": build.text(prefix + "user_header"),
        "required_headers": build.items(prefix + "required_headers"),
        "allow_users": build.items(prefix + "allow_users"),
    }
    if build.flag(prefix + "allow_loopback"):
        data["allow_loopback"] = True
    if build.flag(prefix + "device_auto_approve.enabled"):
        data["device_auto_approve"] = {"enabled": True}
        scopes = build.items(prefix + "device_auto_approve.scopes")
        if scopes:
            data["device_auto_approve"]["scopes"] = scopes
    identity_scopes = build.json_object(prefix + "identity_scopes")
    if identity_scopes is not None:
        data["identity_scopes"] = identity_scopes
    return data


def dict_from_values(values: Mapping[str, Any]) -> tuple[dict[str, Any], list[Issue]]:
    """The JSON config for the form (only keys relevant to the chosen modes)."""
    build = _Builder(values)
    mode = build.text("network_mode")
    data: dict[str, Any] = {"image_source": build.text("image_source")}
    if data["image_source"] == "prebuilt":
        data["image"] = build.text("image")
    data["network_mode"] = mode

    proxy_network = {
        "name": build.text("proxy_network.name"),
        "hostname": build.text("proxy_network.hostname"),
    }
    if mode == "proxy":
        data["proxy_network"] = proxy_network
        data["public_urls"] = build.items("public_urls")
    else:
        data["port"] = build.port("port")
        if mode == "bridge":
            data["bind_addresses"] = build.items("bind_addresses")
            if build.flag("bridge_extra_network"):
                data["proxy_network"] = proxy_network
        data["hosts"] = build.items("hosts")
        data["extra_origins"] = build.items("extra_origins")

    trusted_proxies = build.items("trusted_proxies")
    if trusted_proxies:
        data["trusted_proxies"] = trusted_proxies
    if build.flag("trusted_proxy.enabled"):
        data["trusted_proxy"] = _trusted_proxy_dict(build)

    data["storage"] = {"kind": build.text("storage.kind")}
    if data["storage"]["kind"] == "host":
        data["storage"]["base_path"] = build.text("storage.base_path")
    data["timezone"] = build.text("timezone")
    data["packages"] = build.items("packages")
    data["skip_update"] = build.flag("skip_update")
    return data, build.issues


def validate(values: Mapping[str, Any]) -> tuple[Settings | None, list[Issue]]:
    """Settings for the form, or None plus every problem found."""
    data, local_issues = dict_from_values(values)
    try:
        settings = parse_settings(data)
        parsed_issues: list[Issue] = []
    except ConfigError as err:
        settings, parsed_issues = None, err.issues
    # A field that failed to parse locally would only repeat as a type error.
    local_paths = {path for path, _ in local_issues}
    issues = local_issues + [i for i in parsed_issues if i[0] not in local_paths]
    return (settings if not issues else None), issues


def load_warnings(data: object) -> list[str]:
    """Problems in a loaded config that editing won't show: dropped keys."""
    try:
        parse_settings(data)
    except ConfigError as err:
        return [
            f"{path}: {message}"
            for path, message in err.issues
            if message.startswith(("unknown setting", "secrets don't belong"))
        ]
    return []


# --- issue -> section / field ------------------------------------------------


def section_of(path: str) -> str:
    for prefix, section in _SECTION_BY_PREFIX:
        if path == prefix or path.startswith(prefix + "."):
            return section
    return "review"


def field_for(path: str) -> str | None:
    """The form field that shows a problem reported for ``path``."""
    candidates = [
        field for field in FIELDS if path == field or path.startswith(field + ".")
    ]
    if candidates:
        return max(candidates, key=len)
    nested = [field for field in FIELDS if field.startswith(path + ".")]
    return nested[0] if nested else None


def field_id(path: str) -> str:
    """Widget id for a field path (ids can't contain dots)."""
    return "f-" + path.replace(".", "--")


# --- secrets ----------------------------------------------------------------


def new_gateway_token() -> str:
    return secrets.token_hex(32)


def secrets_from_values(
    values: Mapping[str, Any], trusted_proxy: bool
) -> tuple[Secrets | None, list[Issue]]:
    """Secrets for the chosen auth mode; the token is required without trusted proxy."""

    def get(path: str) -> str:
        return str(values.get(path, "")).strip()

    if trusted_proxy:
        token = ""
    else:
        token = get("secrets.token")
        if not token:
            return None, [("secrets.token", "Enter or generate a gateway token")]
    return (
        Secrets(
            token,
            get("secrets.anthropic"),
            get("secrets.antigravity"),
            get("secrets.password") if trusted_proxy else "",
        ),
        [],
    )


def ignored_secret_notice(known: Mapping[str, str], trusted_proxy: bool) -> str | None:
    """Why a loaded secret won't be used with the chosen auth mode, if any."""
    if trusted_proxy and known.get(GATEWAY_TOKEN_VAR):
        return (
            f"{GATEWAY_TOKEN_VAR} from the env file is not used: the gateway refuses "
            "to start with trusted-proxy auth and a shared token."
        )
    if not trusted_proxy and known.get(GATEWAY_PASSWORD_VAR):
        return f"{GATEWAY_PASSWORD_VAR} is only used with trusted-proxy auth."
    return None


# --- display helpers ----------------------------------------------------------


def proxy_ip_hint(proxy_network: ExtraNetwork | None) -> str:
    """How to find the source IP the proxy connects to the gateway from."""
    if proxy_network:
        ip_format = (
            '{{(index .NetworkSettings.Networks "'
            + proxy_network.name
            + '").IPAddress}}'
        )
        how = (
            f"Your proxy's IP on {proxy_network.name}: "
            f"sudo docker inspect -f '{ip_format}' <proxy container>\n"
            "(list container names with: sudo docker ps --format '{{.Names}}')"
        )
    else:
        how = (
            "A proxy running on the TrueNAS host itself usually connects from the "
            "Docker network's gateway IP (e.g. 172.17.0.1)."
        )
    return (
        f"{how}\nContainer IPs can change when the proxy container is recreated: "
        "pin a static IP for it, or regenerate and redeploy after a change."
    )


def summary_lines(config: AppConfig) -> list[str]:
    storage = (
        f"host paths under {config.storage.base_path}"
        if config.storage.kind == "host"
        else "named volumes"
    )
    bindings = ", ".join(
        f"{format_host(address)}:{config.host_port}"
        for address in config.bind_addresses
    )
    network = {
        "proxy": "proxy only, no published ports",
        "bridge": f"bridge, {bindings} -> {CONTAINER_PORT}",
        "host": f"host, port {config.host_port}",
    }[config.network_mode]
    extra = config.extra_network
    joined = (
        f"{extra.name} (hostname {extra.hostname}, port {CONTAINER_PORT})"
        if extra
        else "none"
    )
    proxy = config.trusted_proxy
    auth = (
        "gateway token"
        if proxy is None
        else f"trusted proxy (header {proxy.user_header}, users: "
        f"{', '.join(proxy.allow_users) or 'any authenticated user'})"
    )
    proxies = (
        json.dumps(list(config.trusted_proxies)) if config.trusted_proxies else "none"
    )
    keys = config.secrets
    return [
        f"Image:       {config.image} ({config.image_source})",
        f"Network:     {network}",
        f"Joined net:  {joined}",
        f"Auth:        {auth}",
        f"Proxies:     gateway.trustedProxies {proxies}",
        f"Origins:     {', '.join(config.control_ui_origins)}",
        f"Storage:     {storage}",
        f"Timezone:    {config.timezone}",
        f"Packages:    {config.packages}",
        f"Skip update: {'yes' if config.skip_update else 'no'}",
        (
            f"API keys:    Anthropic {'set' if keys.anthropic_api_key else 'not set'}, "
            f"Antigravity {'set' if keys.antigravity_api_key else 'not set'}"
        ),
    ]
