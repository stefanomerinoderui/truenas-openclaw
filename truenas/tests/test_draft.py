import json
from pathlib import Path

import pytest
from test_settings import SETTINGS_VARIANTS

from compose_builder import ExtraNetwork, Secrets, Storage, TrustedProxy
from draft import (
    FIELDS,
    dict_from_values,
    field_for,
    field_id,
    ignored_secret_notice,
    load_warnings,
    secret_values,
    secrets_from_values,
    section_of,
    validate,
    values_from_dict,
)
from settings import (
    Settings,
    default_settings_dict,
    load_settings,
    settings_to_dict,
)

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.json"

TRUSTED_SETTINGS = Settings(
    image_source="inline",
    network_mode="proxy",
    storage=Storage("volumes"),
    public_urls=("https://oc.example.com",),
    proxy_network=ExtraNetwork("ix-nginx-proxy-manager_default", "openclaw"),
    trusted_proxies=("10.0.0.1", "172.17.0.1"),
    trusted_proxy=TrustedProxy(
        user_header="x-auth-request-email",
        required_headers=("x-forwarded-proto",),
        allow_users=("me@example.com",),
        allow_loopback=True,
        device_auto_approve=True,
        device_scopes=("operator.read",),
        identity_scopes={"admin@example.com": ("operator.admin",)},
    ),
    timezone="UTC",
)


def test_the_defaults_are_a_valid_config():
    settings, issues = validate(values_from_dict(default_settings_dict()))
    assert issues == []
    assert settings.network_mode == "proxy"


def test_every_form_field_has_a_value():
    values = {**values_from_dict({}), **secret_values({})}
    assert set(values) == set(FIELDS)


@pytest.mark.parametrize("variant", [*SETTINGS_VARIANTS, "trusted"])
def test_settings_survive_the_form(variant):
    settings = SETTINGS_VARIANTS.get(variant, TRUSTED_SETTINGS)
    values = values_from_dict(json.loads(json.dumps(settings_to_dict(settings))))
    assert validate(values) == (settings, [])


def test_the_example_config_opens_unchanged():
    data = json.loads(EXAMPLE.read_text())
    settings, issues = validate(values_from_dict(data))
    assert issues == []
    assert settings == load_settings(EXAMPLE)


def test_bridge_extra_network_toggle_follows_the_config():
    bridge = settings_to_dict(SETTINGS_VARIANTS["bridge"])  # has proxy_network
    assert values_from_dict(bridge)["bridge_extra_network"] is True
    del bridge["proxy_network"]
    assert values_from_dict(bridge)["bridge_extra_network"] is False
    # proxy mode always joins its network; the bridge toggle stays off
    assert values_from_dict(default_settings_dict())["bridge_extra_network"] is False


def test_hidden_fields_are_left_out_of_the_config():
    values = values_from_dict(default_settings_dict())
    values["port"] = "not-a-port"  # only used in bridge/host mode
    data, issues = dict_from_values(values)
    assert "port" not in data
    assert issues == []


def test_invalid_values_from_a_loaded_config_are_shown_for_fixing():
    values = values_from_dict(
        {
            "network_mode": "weird",
            "port": "abc",
            "packages": "openclaw",
            "skip_update": 1,
        }
    )
    assert values["network_mode"] == "weird"
    assert values["packages"] == "openclaw"
    assert values["skip_update"] is False
    settings, issues = validate(values)
    assert settings is None
    assert ("network_mode", "must be one of: proxy, bridge, host") in issues


@pytest.mark.parametrize(
    ("path", "text", "message"),
    [
        ("public_urls", '["https://oc.example.com"', "JSON list"),
        ("trusted_proxy.identity_scopes", "{not json", "JSON object"),
    ],
)
def test_unparsable_text_is_reported_once_on_its_field(path, text, message):
    values = values_from_dict(default_settings_dict())
    values["trusted_proxy.enabled"] = True
    values["trusted_proxies"] = "10.0.0.1"
    values[path] = text
    settings, issues = validate(values)
    assert settings is None
    on_field = [m for p, m in issues if p == path]
    assert len(on_field) == 1
    assert message in on_field[0]


def test_bad_port_is_reported_on_the_port_field():
    values = values_from_dict(settings_to_dict(SETTINGS_VARIANTS["host"]))
    values["port"] = "80a"
    _, issues = validate(values)
    assert issues == [("port", "Enter a port between 1 and 65535")]


def test_lists_accept_json_or_commas():
    values = values_from_dict(default_settings_dict())
    values["public_urls"] = '["https://a.example.com", "https://b.example.com"]'
    settings, _ = validate(values)
    assert settings.public_urls == ("https://a.example.com", "https://b.example.com")
    values["public_urls"] = "https://a.example.com, https://b.example.com"
    assert validate(values)[0] == settings


def test_load_warnings_list_dropped_keys_only():
    data = {**default_settings_dict(), "prot": 1, "gateway_token": "x", "port": "abc"}
    warnings = load_warnings(data)
    assert len(warnings) == 2
    assert warnings[0].startswith("prot: unknown setting")
    assert warnings[1].startswith("gateway_token: secrets don't belong")


@pytest.mark.parametrize(
    ("path", "section", "field"),
    [
        ("image", "image", "image"),
        ("image_source", "image", "image_source"),
        ("proxy_network.name", "network", "proxy_network.name"),
        ("proxy_network", "network", "proxy_network.name"),
        ("hosts", "network", "hosts"),
        ("trusted_proxies", "auth", "trusted_proxies"),
        ("trusted_proxy.user_header", "auth", "trusted_proxy.user_header"),
        (
            "trusted_proxy.identity_scopes.a@example.com",
            "auth",
            "trusted_proxy.identity_scopes",
        ),
        ("trusted_proxy", "auth", "trusted_proxy.enabled"),
        ("storage", "storage", "storage.kind"),
        ("storage.base_path", "storage", "storage.base_path"),
        ("packages", "environment", "packages"),
        ("secrets.token", "secrets", "secrets.token"),
        ("", "review", None),
    ],
)
def test_issues_map_to_their_section_and_field(path, section, field):
    assert section_of(path) == section
    assert field_for(path) == field


def test_field_ids_are_valid_and_unique():
    ids = [field_id(path) for path in FIELDS]
    assert len(set(ids)) == len(ids)
    assert all("." not in widget_id for widget_id in ids)


def test_secrets_need_a_token_without_trusted_proxy():
    values = secret_values({"ANTHROPIC_API_KEY": "sk"})
    assert secrets_from_values(values, trusted_proxy=False) == (
        None,
        [("secrets.token", "Enter or generate a gateway token")],
    )
    values["secrets.token"] = "tok"
    assert secrets_from_values(values, trusted_proxy=False) == (
        Secrets("tok", "sk", ""),
        [],
    )


def test_trusted_proxy_secrets_drop_the_token_and_keep_the_password():
    values = secret_values(
        {"OPENCLAW_GATEWAY_TOKEN": "tok", "OPENCLAW_GATEWAY_PASSWORD": "pw"}
    )
    assert secrets_from_values(values, trusted_proxy=True) == (
        Secrets("", "", "", "pw"),
        [],
    )


def test_notices_for_secrets_that_do_not_apply():
    assert "refuses to start" in ignored_secret_notice(
        {"OPENCLAW_GATEWAY_TOKEN": "t"}, trusted_proxy=True
    )
    assert "only used with trusted-proxy" in ignored_secret_notice(
        {"OPENCLAW_GATEWAY_PASSWORD": "p"}, trusted_proxy=False
    )
    assert ignored_secret_notice({"OPENCLAW_GATEWAY_TOKEN": "t"}, False) is None


def test_trusted_proxies_are_kept_with_token_auth():
    values = values_from_dict(default_settings_dict())
    values["trusted_proxies"] = '["10.0.0.1", "172.17.0.1"]'
    settings, issues = validate(values)
    assert issues == []
    assert settings.trusted_proxies == ("10.0.0.1", "172.17.0.1")
    assert settings.trusted_proxy is None


def test_trusted_proxy_auth_requires_trusted_proxies():
    values = values_from_dict(default_settings_dict())
    values["trusted_proxy.enabled"] = True
    _, issues = validate(values)
    assert [p for p, _ in issues] == ["trusted_proxies"]


def test_nested_proxies_from_older_configs_fill_the_field():
    data = {**default_settings_dict(), "trusted_proxy": {"proxies": ["10.0.0.1"]}}
    assert values_from_dict(data)["trusted_proxies"] == "10.0.0.1"
