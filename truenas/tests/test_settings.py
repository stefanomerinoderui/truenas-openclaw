import json
import re
from pathlib import Path

import pytest

from compose_builder import ExtraNetwork, Secrets, Storage, TrustedProxy
from settings import (
    SECRET_LOOKING_KEY,
    ConfigError,
    Settings,
    build_app_config,
    load_settings,
    parse_settings,
    save_settings,
    settings_to_dict,
)

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.json"
NPM = ExtraNetwork("ix-nginx-proxy-manager_default", "openclaw")

PROXY_JSON = {
    "network_mode": "proxy",
    "public_urls": ["https://oc.example.com"],
    "storage": {"kind": "volumes"},
    "timezone": "UTC",
}


def problems_of(data) -> list[str]:
    with pytest.raises(ConfigError) as info:
        parse_settings(data)
    return info.value.problems


def test_minimal_proxy_config_gets_the_defaults():
    settings = parse_settings(PROXY_JSON)
    assert settings.image_source == "inline"
    assert settings.proxy_network == NPM
    assert settings.public_urls == ("https://oc.example.com",)
    assert settings.packages == ("openclaw", "@anthropic-ai/claude-code")
    assert settings.skip_update is False
    assert settings.storage == Storage("volumes")


def test_bridge_config_is_read_and_derives_the_app_config():
    settings = parse_settings(
        {
            "network_mode": "bridge",
            "port": 8080,
            "bind_addresses": ["192.168.1.5", "::1"],
            "hosts": ["truenas.local"],
            "extra_origins": ["https://oc.example.com/"],
            "proxy_network": {"name": "my-net", "hostname": "oc"},
            "storage": {"kind": "host", "base_path": "/mnt/tank/apps/oc/"},
            "timezone": "Europe/Madrid",
            "packages": ["openclaw"],
            "skip_update": True,
        }
    )
    assert settings.storage == Storage("host", "/mnt/tank/apps/oc")
    config = build_app_config(settings, Secrets("tok"))
    assert config.host_port == 8080
    assert config.bind_addresses == ("192.168.1.5", "::1")
    assert config.extra_network == ExtraNetwork("my-net", "oc")
    assert config.control_ui_origins == (
        "http://truenas.local:8080",
        "http://localhost:8080",
        "http://127.0.0.1:8080",
        "https://oc.example.com",
    )
    assert config.packages == "openclaw"
    assert config.skip_update is True
    assert config.secrets == Secrets("tok")


def test_proxy_config_publishes_nothing_and_uses_the_public_url():
    config = build_app_config(parse_settings(PROXY_JSON), Secrets("tok"))
    assert config.network_mode == "proxy"
    assert config.control_ui_origins == (
        "http://localhost:18789",
        "http://127.0.0.1:18789",
        "https://oc.example.com",
    )


def test_host_mode_ignores_keys_that_do_not_apply():
    settings = parse_settings(
        {
            "network_mode": "host",
            "hosts": ["truenas.local"],
            "proxy_network": {"name": "ignored"},
            "bind_addresses": ["10.0.0.1"],
            "storage": {"kind": "volumes"},
            "timezone": "UTC",
        }
    )
    assert settings.proxy_network is None
    assert settings.bind_addresses == ("0.0.0.0",)


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({**PROXY_JSON, "public_urls": None}, "public_urls: must be a list"),
        (
            {k: v for k, v in PROXY_JSON.items() if k != "public_urls"},
            "public_urls: required",
        ),
        (
            {k: v for k, v in PROXY_JSON.items() if k != "storage"},
            "storage: required",
        ),
        (
            {**PROXY_JSON, "storage": {"kind": "host"}},
            "storage.base_path: required",
        ),
        (
            {**PROXY_JSON, "storage": {"kind": "host", "base_path": "/home/me"}},
            "storage.base_path: TrueNAS datasets live under",
        ),
        (
            {"network_mode": "bridge", "storage": {"kind": "volumes"}},
            "hosts: required",
        ),
    ],
)
def test_required_and_invalid_values_are_reported(data, expected):
    assert any(expected in problem for problem in problems_of(data))


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"network_mode": "weird"}, "network_mode: must be one of"),
        ({"image_source": "docker"}, "image_source: must be one of"),
        ({"skip_update": "yes"}, "skip_update: must be true or false"),
        ({"packages": "openclaw claude"}, "packages: must be a list of strings"),
        ({"packages": ["a b"]}, "packages: entries must not be empty"),
        ({"public_urls": ["oc.example.com"]}, "public_urls: Not an origin URL"),
        ({"timezone": 5}, "timezone: must be a string"),
        ({"proxy_network": {"name": "-bad"}}, "proxy_network.name: Use letters"),
        ({"proxy_network": "npm"}, "proxy_network: must be an object"),
        ({"proxy_network": {"nme": "typo"}}, "proxy_network.nme: unknown setting"),
    ],
)
def test_proxy_config_value_errors(override, expected):
    assert any(expected in p for p in problems_of({**PROXY_JSON, **override}))


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"port": "8080"}, "port: must be an integer"),
        ({"port": True}, "port: must be an integer"),
        ({"port": 70000}, "port: Enter a port"),
        ({"bind_addresses": ["not-an-ip"]}, "bind_addresses: Not a valid IPv"),
        ({"bind_addresses": ["0.0.0.0", "10.0.0.1"]}, "bind_addresses: The IPv4"),
        ({"hosts": ["http://truenas.local"]}, "hosts: Enter bare hostnames"),
        ({"extra_origins": ["oc.example.com"]}, "extra_origins: Not an origin URL"),
    ],
)
def test_bridge_config_value_errors(override, expected):
    data = {
        "network_mode": "bridge",
        "hosts": ["truenas.local"],
        "storage": {"kind": "volumes"},
        **override,
    }
    assert any(expected in p for p in problems_of(data))


def test_every_problem_is_reported_at_once():
    problems = problems_of({"network_mode": "proxy", "port_x": 1, "skip_update": 3})
    assert len(problems) >= 4  # unknown key, public_urls, storage, skip_update


@pytest.mark.parametrize(
    "key", ["gateway_token", "OPENCLAW_GATEWAY_TOKEN", "anthropic_api_key", "password"]
)
def test_secrets_are_rejected_with_a_pointer_to_the_env_file(key):
    problems = problems_of({**PROXY_JSON, key: "s3cret"})
    message = next(p for p in problems if p.startswith(f"{key}:"))
    assert "env file" in message
    assert "OPENCLAW_GATEWAY_TOKEN" in message
    assert "s3cret" not in message


def test_secret_in_a_nested_object_is_rejected_too():
    data = {**PROXY_JSON, "proxy_network": {"name": "n", "api_key": "x"}}
    assert any("proxy_network.api_key" in p for p in problems_of(data))


def test_top_level_must_be_an_object():
    assert problems_of(["not", "an", "object"]) == [
        "the top level must be a JSON object"
    ]


SETTINGS_VARIANTS = {
    "proxy": Settings(
        image_source="inline",
        network_mode="proxy",
        storage=Storage("host", "/mnt/tank/apps/oc"),
        public_urls=("https://oc.example.com",),
        proxy_network=NPM,
        timezone="Europe/Madrid",
    ),
    "bridge": Settings(
        image_source="prebuilt",
        prebuilt_image="ghcr.io/me/oc:1",
        network_mode="bridge",
        storage=Storage("volumes"),
        port=8080,
        bind_addresses=("192.168.1.5", "fd00::1"),
        hosts=("truenas.local", "10.0.0.2"),
        extra_origins=("https://oc.example.com",),
        proxy_network=ExtraNetwork("my-net", "oc"),
        timezone="UTC",
        packages=("openclaw",),
        skip_update=True,
    ),
    "host": Settings(
        image_source="inline",
        network_mode="host",
        storage=Storage("volumes"),
        port=9000,
        hosts=("truenas.local",),
        timezone="UTC",
    ),
}


@pytest.mark.parametrize("variant", SETTINGS_VARIANTS)
def test_settings_survive_a_json_round_trip(variant):
    settings = SETTINGS_VARIANTS[variant]
    assert (
        parse_settings(json.loads(json.dumps(settings_to_dict(settings)))) == settings
    )


@pytest.mark.parametrize("variant", SETTINGS_VARIANTS)
def test_saved_json_contains_no_secret_looking_keys(variant):
    def keys(node):
        if isinstance(node, dict):
            for key, value in node.items():
                yield key
                yield from keys(value)

    saved = settings_to_dict(SETTINGS_VARIANTS[variant])
    assert not [key for key in keys(saved) if SECRET_LOOKING_KEY.search(key)]


def test_saved_json_only_has_keys_for_the_chosen_mode():
    proxy = settings_to_dict(SETTINGS_VARIANTS["proxy"])
    assert "port" not in proxy
    assert "hosts" not in proxy
    host = settings_to_dict(SETTINGS_VARIANTS["host"])
    assert "bind_addresses" not in host
    assert "public_urls" not in host
    assert "image" not in host


def test_save_and_load_a_file(tmp_path):
    path = tmp_path / "cfg.json"
    save_settings(SETTINGS_VARIANTS["bridge"], path)
    assert path.read_text().endswith("}\n")
    assert load_settings(path) == SETTINGS_VARIANTS["bridge"]


def test_load_reports_unreadable_and_invalid_files(tmp_path):
    with pytest.raises(ConfigError, match="cannot read"):
        load_settings(tmp_path / "missing.json")
    broken = tmp_path / "broken.json"
    broken.write_text('{"network_mode": ')
    with pytest.raises(ConfigError, match="not valid JSON"):
        load_settings(broken)


def test_example_config_is_valid():
    settings = load_settings(EXAMPLE)
    assert settings.network_mode == "proxy"
    assert re.fullmatch(r"https://\S+", settings.public_urls[0])


TRUSTED_JSON = {
    **PROXY_JSON,
    "trusted_proxies": ["172.18.0.0/16"],
    "trusted_proxy": {
        "user_header": "X-Auth-Request-Email",
        "required_headers": ["X-Forwarded-Proto"],
        "allow_users": ["me@example.com"],
    },
}


def test_trusted_proxy_is_off_by_default():
    assert parse_settings(PROXY_JSON).trusted_proxy is None


def test_trusted_proxy_section_is_read_and_headers_are_lowercased():
    settings = parse_settings(TRUSTED_JSON)
    assert settings.trusted_proxies == ("172.18.0.0/16",)
    assert settings.trusted_proxy == TrustedProxy(
        user_header="x-auth-request-email",
        required_headers=("x-forwarded-proto",),
        allow_users=("me@example.com",),
    )


def test_trusted_proxy_advanced_options_are_read():
    data = {
        **TRUSTED_JSON,
        "trusted_proxy": {
            **TRUSTED_JSON["trusted_proxy"],
            "allow_loopback": True,
            "device_auto_approve": {"enabled": True, "scopes": ["operator.read"]},
            "identity_scopes": {"admin@example.com": ["operator.admin"]},
        },
    }
    proxy = parse_settings(data).trusted_proxy
    assert proxy.allow_loopback is True
    assert proxy.device_auto_approve is True
    assert proxy.device_scopes == ("operator.read",)
    assert proxy.identity_scopes == {"admin@example.com": ("operator.admin",)}


IP = ["10.0.0.1"]


@pytest.mark.parametrize(
    ("proxies", "section", "expected"),
    [
        (None, {"user_header": "x"}, "trusted_proxies: required with trusted-proxy"),
        (IP, {}, "trusted_proxy.user_header: required"),
        (
            ["0.0.0.0/0"],
            {"user_header": "x"},
            "trusted_proxies: 0.0.0.0/0 would trust every source",
        ),
        (
            ["proxy.local"],
            {"user_header": "x"},
            "trusted_proxies: Not a valid IP address or CIDR",
        ),
        (
            IP,
            {"user_header": "bad header"},
            "trusted_proxy.user_header: Enter an HTTP header name",
        ),
        (
            IP,
            {"user_header": "x", "allow_users": "me"},
            "trusted_proxy.allow_users: must be a list of strings",
        ),
        (
            IP,
            {"user_header": "x", "allow_loopback": "yes"},
            "trusted_proxy.allow_loopback: must be true or false",
        ),
        (
            IP,
            {"user_header": "x", "identity_scopes": ["a"]},
            "trusted_proxy.identity_scopes: must be an object",
        ),
        (
            IP,
            {"user_header": "x", "identity_scopes": {"a@example.com": "operator.read"}},
            "trusted_proxy.identity_scopes.a@example.com: must be a list",
        ),
        (
            IP,
            {"user_header": "x", "device_auto_approve": {"enabled": "no"}},
            "trusted_proxy.device_auto_approve.enabled: must be true or false",
        ),
        (
            IP,
            {"user_header": "x", "userheader": "typo"},
            "trusted_proxy.userheader: unknown setting",
        ),
    ],
)
def test_trusted_proxy_errors_are_reported_without_crashing(proxies, section, expected):
    data = {**PROXY_JSON, "trusted_proxy": section}
    if proxies is not None:
        data["trusted_proxies"] = proxies
    problems = problems_of(data)
    assert any(expected in problem for problem in problems), problems


def test_trusted_proxies_are_optional_with_token_auth():
    assert parse_settings(PROXY_JSON).trusted_proxies == ()
    settings = parse_settings({**PROXY_JSON, "trusted_proxies": ["10.0.0.1", "::1"]})
    assert settings.trusted_proxies == ("10.0.0.1", "::1")
    assert settings.trusted_proxy is None
    assert settings_to_dict(settings)["trusted_proxies"] == ["10.0.0.1", "::1"]
    assert "trusted_proxies" not in settings_to_dict(parse_settings(PROXY_JSON))


def test_invalid_trusted_proxies_are_reported_with_token_auth_too():
    problems = problems_of({**PROXY_JSON, "trusted_proxies": ["0.0.0.0/0"]})
    assert any(p.startswith("trusted_proxies: 0.0.0.0/0") for p in problems)


def test_older_configs_with_nested_proxies_still_load():
    data = {**PROXY_JSON, "trusted_proxy": {"proxies": IP, "user_header": "x"}}
    settings = parse_settings(data)
    assert settings.trusted_proxies == ("10.0.0.1",)
    saved = settings_to_dict(settings)
    assert saved["trusted_proxies"] == IP
    assert "proxies" not in saved["trusted_proxy"]


def test_trusted_proxy_must_be_an_object():
    assert any(
        "trusted_proxy: must be an object" in p
        for p in problems_of({**PROXY_JSON, "trusted_proxy": True})
    )


def test_trusted_proxy_round_trips_and_only_saves_non_defaults():
    plain = parse_settings(TRUSTED_JSON)
    saved = settings_to_dict(plain)["trusted_proxy"]
    assert set(saved) == {"user_header", "required_headers", "allow_users"}
    assert parse_settings(json.loads(json.dumps(settings_to_dict(plain)))) == plain

    advanced = parse_settings(
        {
            **TRUSTED_JSON,
            "trusted_proxy": {
                **TRUSTED_JSON["trusted_proxy"],
                "allow_loopback": True,
                "device_auto_approve": {"enabled": True},
                "identity_scopes": {"a@example.com": ["operator.read"]},
            },
        }
    )
    assert (
        parse_settings(json.loads(json.dumps(settings_to_dict(advanced)))) == advanced
    )


def test_settings_without_trusted_proxy_save_no_such_key():
    assert "trusted_proxy" not in settings_to_dict(parse_settings(PROXY_JSON))


def test_trusted_proxy_flows_into_the_app_config():
    config = build_app_config(parse_settings(TRUSTED_JSON), Secrets(""))
    assert config.trusted_proxy.user_header == "x-auth-request-email"
    with pytest.raises(ValueError, match="refuses to start"):
        build_app_config(parse_settings(TRUSTED_JSON), Secrets("tok"))
