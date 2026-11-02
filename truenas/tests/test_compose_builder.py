import json
from pathlib import Path

import pytest
import yaml

from compose_builder import (
    ENTRYPOINT_TARGET,
    LOCAL_IMAGE,
    MOUNTS,
    AppConfig,
    ExtraNetwork,
    Secrets,
    Storage,
    TrustedProxy,
    build_compose,
    dump_yaml,
    escape_interpolation,
    format_host,
    inline_dockerfile,
)
from settings import build_origins
from validators import (
    parse_bind_addresses,
    validate_bind_addresses,
    validate_dataset_path,
    validate_header_name,
    validate_header_names,
    validate_hostname,
    validate_hosts,
    validate_identities,
    validate_network_name,
    validate_required_urls,
    validate_trusted_proxies,
    validate_urls,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = (REPO_ROOT / "Dockerfile").read_text()
ENTRYPOINT = (REPO_ROOT / "docker-entrypoint.sh").read_text()


def make_config(**overrides) -> AppConfig:
    values = {
        "image_source": "inline",
        "network_mode": "bridge",
        "host_port": 18789,
        "storage": Storage("volumes"),
        "secrets": Secrets("tok$en"),
        "control_ui_origins": ("http://truenas.local:18789",),
    }
    values.update(overrides)
    return AppConfig(**values)


def roundtrip(config: AppConfig) -> dict:
    return yaml.safe_load(dump_yaml(build_compose(config, DOCKERFILE, ENTRYPOINT)))


def test_inline_dockerfile_removes_entrypoint_copy():
    result = inline_dockerfile(DOCKERFILE)
    assert "docker-entrypoint.sh /usr/local/bin" not in result
    assert result.startswith("# OpenClaw")


def test_inline_dockerfile_requires_copy_line():
    with pytest.raises(ValueError):
        inline_dockerfile("FROM node:24-alpine\n")


def test_escape_interpolation():
    assert escape_interpolation("$PNPM_HOME ${X}") == "$$PNPM_HOME $${X}"


def test_inline_mode_stages_and_build():
    compose = roundtrip(make_config())
    services = compose["services"]
    assert list(services) == ["update", "doctor", "openclaw"]
    assert services["update"]["pull_policy"] == "build"
    assert "$$PNPM_HOME" in services["update"]["build"]["dockerfile_inline"]
    assert services["doctor"]["pull_policy"] == "never"
    assert services["doctor"]["depends_on"] == {
        "update": {"condition": "service_completed_successfully"}
    }
    assert services["openclaw"]["entrypoint"][-1] == ENTRYPOINT_TARGET
    assert "$$" in compose["configs"]["openclaw-entrypoint"]["content"]
    assert set(compose["volumes"]) == set(MOUNTS)


def test_environment_is_escaped_and_gateway_skips_setup():
    env = roundtrip(make_config())["services"]["openclaw"]["environment"]
    assert env["OPENCLAW_GATEWAY_TOKEN"] == "tok$$en"
    assert env["RUN_SETUP"] == "0"
    assert env["OPENCLAW_CONTROL_UI_ORIGINS"] == '["http://truenas.local:18789"]'
    assert "ANTHROPIC_API_KEY" not in env


def test_bridge_publishes_port_to_container_port():
    gateway = roundtrip(make_config(host_port=8080, bind_addresses=("192.168.1.5",)))[
        "services"
    ]["openclaw"]
    assert gateway["ports"] == ["192.168.1.5:8080:18789"]
    assert gateway["command"][-1] == "18789"


def test_bridge_publishes_one_port_per_bind_address():
    config = make_config(bind_addresses=("192.168.1.5", "10.0.0.2", "fd00::1"))
    gateway = roundtrip(config)["services"]["openclaw"]
    assert gateway["ports"] == [
        "192.168.1.5:18789:18789",
        "10.0.0.2:18789:18789",
        "[fd00::1]:18789:18789",
    ]


def test_extra_network_joins_only_the_gateway_with_alias():
    extra = ExtraNetwork("ix-nginx-proxy-manager_default", "oc")
    compose = roundtrip(make_config(extra_network=extra))
    gateway = compose["services"]["openclaw"]
    assert gateway["hostname"] == "oc"
    assert gateway["networks"] == {
        "ix-nginx-proxy-manager_default": {"aliases": ["oc"]},
    }
    assert compose["networks"] == {"ix-nginx-proxy-manager_default": {"external": True}}
    for name in ("update", "doctor"):
        assert "networks" not in compose["services"][name]
        assert "hostname" not in compose["services"][name]


def test_proxy_mode_publishes_nothing_and_only_joins_the_proxy_network():
    extra = ExtraNetwork("ix-nginx-proxy-manager_default", "openclaw")
    compose = roundtrip(make_config(network_mode="proxy", extra_network=extra))
    gateway = compose["services"]["openclaw"]
    assert "ports" not in gateway
    assert "network_mode" not in gateway
    assert gateway["networks"] == {
        "ix-nginx-proxy-manager_default": {"aliases": ["openclaw"]}
    }
    assert gateway["command"][-1] == "18789"
    assert gateway["healthcheck"]["test"][-1] == "http://127.0.0.1:18789/healthz"
    # `external` is only valid in the top-level networks block.
    assert compose["networks"] == {"ix-nginx-proxy-manager_default": {"external": True}}


def test_proxy_mode_requires_a_network():
    with pytest.raises(ValueError, match="proxy network"):
        make_config(network_mode="proxy")


def test_no_extra_network_by_default():
    compose = roundtrip(make_config())
    assert "networks" not in compose
    assert "networks" not in compose["services"]["openclaw"]


def test_extra_network_is_rejected_in_host_mode():
    with pytest.raises(ValueError, match="host network"):
        make_config(network_mode="host", extra_network=ExtraNetwork("net", "oc"))


def test_bridge_mode_requires_a_bind_address():
    with pytest.raises(ValueError, match="bind address"):
        make_config(bind_addresses=())


def test_host_network_uses_host_port():
    gateway = roundtrip(make_config(network_mode="host", host_port=8080))["services"][
        "openclaw"
    ]
    assert "ports" not in gateway
    assert gateway["network_mode"] == "host"
    assert gateway["command"][-1] == "8080"
    assert gateway["healthcheck"]["test"][-1] == "http://127.0.0.1:8080/healthz"


def test_host_storage_adds_permissions_stage_that_builds():
    compose = roundtrip(
        make_config(storage=Storage("host", "/mnt/tank/apps/openclaw/"))
    )
    services = compose["services"]
    assert list(services) == ["permissions", "update", "doctor", "openclaw"]
    assert "build" in services["permissions"]
    assert "build" not in services["update"]
    assert services["update"]["pull_policy"] == "never"
    assert services["permissions"]["user"] == "0:0"
    assert (
        "/mnt/tank/apps/openclaw/tools:/home/node/.local"
        in services["openclaw"]["volumes"]
    )
    assert "volumes" not in compose


def test_prebuilt_mode_has_no_build_or_configs():
    compose = roundtrip(
        make_config(image_source="prebuilt", prebuilt_image="ghcr.io/me/oc:1")
    )
    assert "configs" not in compose
    for service in compose["services"].values():
        assert service["image"] == "ghcr.io/me/oc:1"
        assert "build" not in service
    assert compose["services"]["update"]["pull_policy"] == "always"
    assert LOCAL_IMAGE not in dump_yaml(compose)


def test_format_host_brackets_only_ipv6():
    assert format_host("10.0.0.2") == "10.0.0.2"
    assert format_host("fd00::1") == "[fd00::1]"
    assert format_host("[fd00::1]") == "[fd00::1]"
    assert format_host("truenas.local") == "truenas.local"


def test_parse_bind_addresses_normalises_and_deduplicates():
    parsed = parse_bind_addresses("192.168.1.5, [::1], 0:0:0:0:0:0:0:1, 192.168.1.5")
    assert parsed == ("192.168.1.5", "::1")


def test_build_origins_deduplicates_and_formats_ipv6():
    origins = build_origins(
        ["truenas.local", "fd00::1", "localhost"], 18789, ["https://oc.example.com/"]
    )
    assert origins == (
        "http://truenas.local:18789",
        "http://[fd00::1]:18789",
        "http://localhost:18789",
        "http://127.0.0.1:18789",
        "https://oc.example.com",
    )


@pytest.mark.parametrize(
    ("validator", "value", "ok"),
    [
        (validate_hosts, "truenas.local, 10.0.0.2", True),
        (validate_hosts, "http://truenas.local", False),
        (validate_hosts, "truenas.local:80", False),
        (validate_hosts, "", False),
        (validate_urls, "", True),
        (validate_urls, "https://oc.example.com", True),
        (validate_urls, "oc.example.com", False),
        (validate_required_urls, "", False),
        (validate_required_urls, "oc.example.com", False),
        (validate_required_urls, "https://oc.example.com", True),
        (validate_dataset_path, "/mnt/tank/apps", True),
        (validate_dataset_path, "/home/me", False),
        (validate_bind_addresses, "0.0.0.0", True),
        (validate_bind_addresses, "192.168.1.5, 10.0.0.2", True),
        (validate_bind_addresses, "0.0.0.0, ::1", True),
        (validate_bind_addresses, "0.0.0.0, 0.0.0.0", True),
        (validate_bind_addresses, "0.0.0.0, 192.168.1.5", False),
        (validate_bind_addresses, "::, fd00::1", False),
        (validate_bind_addresses, "truenas.local", False),
        (validate_bind_addresses, "", False),
        (validate_network_name, "ix-nginx-proxy-manager_default", True),
        (validate_network_name, "-bad", False),
        (validate_network_name, "has space", False),
        (validate_network_name, "", False),
        (validate_hostname, "openclaw", True),
        (validate_hostname, "oc.internal", True),
        (validate_hostname, "-oc", False),
        (validate_hostname, "oc_1", False),
        (validate_hostname, "", False),
        (validate_trusted_proxies, "172.18.0.0/16", True),
        (validate_trusted_proxies, "10.0.0.1, fd00::/8, 192.168.1.5/24", True),
        (validate_trusted_proxies, "", False),
        (validate_trusted_proxies, "proxy.local", False),
        (validate_trusted_proxies, "0.0.0.0/0", False),
        (validate_trusted_proxies, "::/0", False),
        (validate_header_name, "x-forwarded-user", True),
        (validate_header_name, "X_Auth_Email", True),
        (validate_header_name, "bad header", False),
        (validate_header_name, "", False),
        (validate_header_names, "", True),
        (validate_header_names, "x-a, x-b", True),
        (validate_header_names, "x-a, bad header", False),
        (validate_identities, "", True),
        (validate_identities, "a@example.com, b@example.com", True),
        (validate_identities, "first last", False),
    ],
)
def test_validators(validator, value, ok):
    assert (validator(value) is True) is ok


TRUSTED = TrustedProxy(
    proxies=("172.18.0.0/16",),
    user_header="x-auth-request-email",
    required_headers=("x-forwarded-proto", "x-forwarded-host"),
    allow_users=("me@example.com",),
)


def trusted_config(**overrides) -> AppConfig:
    values = {
        "network_mode": "proxy",
        "extra_network": ExtraNetwork("ix-nginx-proxy-manager_default", "openclaw"),
        "trusted_proxy": TRUSTED,
        "secrets": Secrets(""),
    }
    values.update(overrides)
    return make_config(**values)


def overrides_of(compose: dict, service: str = "openclaw") -> list[dict]:
    env = compose["services"][service]["environment"]
    return json.loads(env["OPENCLAW_CONFIG_OVERRIDES"])


def test_token_mode_resets_the_auth_mode_on_every_deploy():
    compose = roundtrip(make_config())
    assert overrides_of(compose) == [{"path": "gateway.auth.mode", "value": "token"}]
    assert compose["services"]["openclaw"]["environment"]["OPENCLAW_GATEWAY_TOKEN"]


def test_trusted_proxy_sets_the_documented_gateway_keys():
    overrides = {
        o["path"]: o["value"] for o in overrides_of(roundtrip(trusted_config()))
    }
    prefix = "gateway.auth.trustedProxy"
    assert overrides == {
        "gateway.auth.mode": "trusted-proxy",
        "gateway.trustedProxies": ["172.18.0.0/16"],
        f"{prefix}.userHeader": "x-auth-request-email",
        f"{prefix}.requiredHeaders": ["x-forwarded-proto", "x-forwarded-host"],
        f"{prefix}.allowUsers": ["me@example.com"],
        f"{prefix}.allowLoopback": False,
        f"{prefix}.deviceAutoApprove.enabled": False,
        "gateway.auth.identityScopes": {},
    }


def test_trusted_proxy_advanced_options_are_passed_through():
    proxy = TrustedProxy(
        proxies=("10.0.0.1",),
        user_header="x-forwarded-user",
        allow_loopback=True,
        device_auto_approve=True,
        device_scopes=("operator.read",),
        identity_scopes={"admin@example.com": ("operator.admin",)},
    )
    overrides = {
        o["path"]: o["value"]
        for o in overrides_of(roundtrip(trusted_config(trusted_proxy=proxy)))
    }
    prefix = "gateway.auth.trustedProxy"
    assert overrides[f"{prefix}.allowLoopback"] is True
    assert overrides[f"{prefix}.deviceAutoApprove.enabled"] is True
    assert overrides[f"{prefix}.deviceAutoApprove.scopes"] == ["operator.read"]
    assert overrides["gateway.auth.identityScopes"] == {
        "admin@example.com": ["operator.admin"]
    }


def test_trusted_proxy_never_sets_a_gateway_token_in_any_stage():
    compose = roundtrip(trusted_config())
    for name, service in compose["services"].items():
        assert "OPENCLAW_GATEWAY_TOKEN" not in service["environment"], name
        assert overrides_of(compose, name)[0]["value"] == "trusted-proxy"


def test_gateway_password_is_only_passed_with_trusted_proxy():
    with_proxy = roundtrip(trusted_config(secrets=Secrets("", gateway_password="pw$1")))
    assert (
        with_proxy["services"]["openclaw"]["environment"]["OPENCLAW_GATEWAY_PASSWORD"]
        == "pw$$1"
    )
    plain = roundtrip(make_config(secrets=Secrets("tok", gateway_password="pw")))
    assert (
        "OPENCLAW_GATEWAY_PASSWORD" not in plain["services"]["openclaw"]["environment"]
    )


def test_trusted_proxy_and_gateway_token_are_mutually_exclusive():
    with pytest.raises(ValueError, match="refuses to start"):
        trusted_config(secrets=Secrets("tok"))
    with pytest.raises(ValueError, match="gateway token is required"):
        make_config(secrets=Secrets(""))


def test_trusted_proxy_needs_a_proxy_and_a_header():
    with pytest.raises(ValueError, match="at least one trusted proxy"):
        TrustedProxy(proxies=(), user_header="x-user")
    with pytest.raises(ValueError, match="user header"):
        TrustedProxy(proxies=("10.0.0.1",), user_header="")
