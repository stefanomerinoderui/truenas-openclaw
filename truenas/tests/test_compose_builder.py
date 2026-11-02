from pathlib import Path

import pytest
import yaml

from compose_builder import (
    ENTRYPOINT_TARGET,
    LOCAL_IMAGE,
    MOUNTS,
    AppConfig,
    Secrets,
    Storage,
    build_compose,
    dump_yaml,
    escape_interpolation,
    inline_dockerfile,
)
from generate_yaml import (
    build_origins,
    validate_dataset_path,
    validate_hosts,
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
    gateway = roundtrip(make_config(host_port=8080, bind_address="192.168.1.5"))[
        "services"
    ]["openclaw"]
    assert gateway["ports"] == ["192.168.1.5:8080:18789"]
    assert gateway["command"][-1] == "18789"


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
        (validate_dataset_path, "/mnt/tank/apps", True),
        (validate_dataset_path, "/home/me", False),
    ],
)
def test_validators(validator, value, ok):
    assert (validator(value) is True) is ok
