import json
import re
import stat

import pytest
import yaml

import generate_yaml
from compose_builder import Secrets, TrustedProxy
from settings import load_settings, parse_settings, settings_to_dict

TOKEN = "t" * 64
CONFIG = {
    "network_mode": "proxy",
    "public_urls": ["https://oc.example.com"],
    "storage": {"kind": "volumes"},
    "timezone": "UTC",
}


@pytest.fixture
def no_prompts(monkeypatch):
    """Config mode must never prompt; fail loudly if it tries."""

    def boom(*_args, **_kwargs):
        raise AssertionError("config mode must not prompt")

    monkeypatch.setattr(generate_yaml, "ask", boom)


def run(tmp_path, capsys, config=CONFIG, env_text=None, extra_args=()):
    config_path = tmp_path / "cfg.json"
    config_path.write_text(json.dumps(config))
    output = tmp_path / "out.yaml"
    args = ["--config", str(config_path), "-o", str(output), *extra_args]
    if env_text is not None:
        env_path = tmp_path / "secrets.env"
        env_path.write_text(env_text)
        args += ["--env-file", str(env_path)]
    code = generate_yaml.main(args)
    captured = capsys.readouterr()
    return code, output, captured.out + captured.err


def gateway_env(output) -> dict:
    return yaml.safe_load(output.read_text())["services"]["openclaw"]["environment"]


def test_config_mode_builds_the_yaml_with_secrets_from_the_env_file(
    tmp_path, capsys, no_prompts, monkeypatch
):
    monkeypatch.delenv("OPENCLAW_GATEWAY_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    env_text = f"OPENCLAW_GATEWAY_TOKEN={TOKEN}\nANTHROPIC_API_KEY=sk-test\n"
    code, output, text = run(tmp_path, capsys, env_text=env_text)
    assert code == 0
    env = gateway_env(output)
    assert env["OPENCLAW_GATEWAY_TOKEN"] == TOKEN
    assert env["ANTHROPIC_API_KEY"] == "sk-test"
    assert "ANTIGRAVITY_API_KEY" not in env
    assert "https://oc.example.com" in env["OPENCLAW_CONTROL_UI_ORIGINS"]
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert TOKEN not in text  # secrets are never echoed


def test_config_mode_generates_a_token_when_none_is_provided(
    tmp_path, capsys, no_prompts, monkeypatch
):
    monkeypatch.delenv("OPENCLAW_GATEWAY_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)  # no ./.env here
    code, output, text = run(tmp_path, capsys)
    assert code == 0
    token = gateway_env(output)["OPENCLAW_GATEWAY_TOKEN"]
    assert re.fullmatch(r"[0-9a-f]{64}", token)
    assert token not in text
    assert "OPENCLAW_GATEWAY_TOKEN not found" in text


def test_config_mode_reads_default_dot_env(tmp_path, capsys, no_prompts, monkeypatch):
    monkeypatch.delenv("OPENCLAW_GATEWAY_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(f"OPENCLAW_GATEWAY_TOKEN={TOKEN}\n")
    code, output, _ = run(tmp_path, capsys)
    assert code == 0
    assert gateway_env(output)["OPENCLAW_GATEWAY_TOKEN"] == TOKEN


def test_environment_variable_overrides_the_env_file(
    tmp_path, capsys, no_prompts, monkeypatch
):
    monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", "from-environment")
    code, output, _ = run(
        tmp_path, capsys, env_text=f"OPENCLAW_GATEWAY_TOKEN={TOKEN}\n"
    )
    assert code == 0
    assert gateway_env(output)["OPENCLAW_GATEWAY_TOKEN"] == "from-environment"


def test_invalid_config_lists_every_problem_and_writes_nothing(
    tmp_path, capsys, no_prompts
):
    bad = {"network_mode": "proxy", "gateway_token": "x", "skip_update": "no"}
    code, output, text = run(tmp_path, capsys, config=bad)
    assert code == 1
    assert "Invalid config" in text
    assert "gateway_token: secrets don't belong" in text
    assert "skip_update: must be true or false" in text
    assert "public_urls: required" in text
    assert not output.exists()


def test_missing_config_file_is_an_error(tmp_path, capsys, no_prompts):
    code = generate_yaml.main(["--config", str(tmp_path / "nope.json")])
    assert code == 1
    assert "cannot read" in capsys.readouterr().err


def test_missing_explicit_env_file_is_an_error(tmp_path, capsys, no_prompts):
    code, output, text = run(
        tmp_path, capsys, extra_args=["--env-file", str(tmp_path / "nope.env")]
    )
    assert code == 1
    assert "env file not found" in text
    assert not output.exists()


@pytest.fixture
def settings():
    return parse_settings(CONFIG)


def test_config_save_is_offered_and_written_without_secrets(
    prompts, settings, tmp_path
):
    target = tmp_path / "saved.json"
    prompts.text_answers.append(str(target))
    generate_yaml.offer_config_save(settings)
    assert prompts.asked[0].startswith("Save these settings as a JSON config")
    assert json.loads(target.read_text()) == settings_to_dict(settings)
    assert load_settings(target) == settings


def test_config_save_can_be_declined(prompts, settings, tmp_path):
    prompts.confirm_answers.append(False)
    generate_yaml.offer_config_save(settings)
    assert len(prompts.asked) == 1  # no file name was asked


def test_config_save_does_not_overwrite_without_confirmation(
    prompts, settings, tmp_path
):
    target = tmp_path / "saved.json"
    target.write_text("keep me")
    prompts.confirm_answers += [True, False]  # save? yes, overwrite? no
    prompts.text_answers.append(str(target))
    generate_yaml.offer_config_save(settings)
    assert target.read_text() == "keep me"


def test_config_save_overwrites_when_confirmed(prompts, settings, tmp_path):
    target = tmp_path / "saved.json"
    target.write_text("old")
    prompts.confirm_answers += [True, True]
    prompts.text_answers.append(str(target))
    generate_yaml.offer_config_save(settings)
    assert load_settings(target) == settings


def test_config_save_reports_write_errors(prompts, settings, tmp_path):
    prompts.text_answers.append(str(tmp_path / "missing-dir" / "saved.json"))
    generate_yaml.offer_config_save(settings)  # must not raise


TRUSTED_CONFIG = {
    **CONFIG,
    "trusted_proxy": {
        "proxies": ["172.18.0.0/16"],
        "user_header": "x-auth-request-email",
        "allow_users": ["me@example.com"],
    },
}


def overrides(output) -> dict:
    env = gateway_env(output)
    return {o["path"]: o["value"] for o in json.loads(env["OPENCLAW_CONFIG_OVERRIDES"])}


def test_trusted_proxy_config_mode_sets_no_token_and_ignores_one_in_the_env_file(
    tmp_path, capsys, no_prompts, monkeypatch
):
    monkeypatch.delenv("OPENCLAW_GATEWAY_TOKEN", raising=False)
    env_text = (
        f"OPENCLAW_GATEWAY_TOKEN={TOKEN}\nOPENCLAW_GATEWAY_PASSWORD=pw-internal\n"
    )
    code, output, text = run(tmp_path, capsys, TRUSTED_CONFIG, env_text=env_text)
    assert code == 0
    env = gateway_env(output)
    assert "OPENCLAW_GATEWAY_TOKEN" not in env
    assert env["OPENCLAW_GATEWAY_PASSWORD"] == "pw-internal"
    assert overrides(output)["gateway.auth.mode"] == "trusted-proxy"
    assert "OPENCLAW_GATEWAY_TOKEN ignored" in text
    assert "generated a random token" not in text
    assert "trusted proxy (x-auth-request-email header" in text
    assert TOKEN not in text
    assert "pw-internal" not in text


def test_password_is_ignored_without_trusted_proxy_auth(
    tmp_path, capsys, no_prompts, monkeypatch
):
    monkeypatch.delenv("OPENCLAW_GATEWAY_PASSWORD", raising=False)
    env_text = f"OPENCLAW_GATEWAY_TOKEN={TOKEN}\nOPENCLAW_GATEWAY_PASSWORD=pw\n"
    code, output, text = run(tmp_path, capsys, env_text=env_text)
    assert code == 0
    assert "OPENCLAW_GATEWAY_PASSWORD" not in gateway_env(output)
    assert overrides(output) == {"gateway.auth.mode": "token"}
    assert "OPENCLAW_GATEWAY_PASSWORD ignored" in text


def test_invalid_trusted_proxy_config_writes_nothing(tmp_path, capsys, no_prompts):
    bad = {**CONFIG, "trusted_proxy": {"proxies": ["0.0.0.0/0"]}}
    code, output, text = run(tmp_path, capsys, bad)
    assert code == 1
    assert "trusted_proxy.proxies: 0.0.0.0/0 would trust every source" in text
    assert "trusted_proxy.user_header: required" in text
    assert not output.exists()


def test_trusted_proxy_prompts_collect_the_answers(prompts):
    prompts.confirm_answers.append(True)
    prompts.text_answers += [
        "172.18.0.0/16, 172.18.0.0/16",
        "X-Auth-Request-Email",
        "X-Forwarded-Proto, x-forwarded-host",
        "me@example.com",
    ]
    proxy = generate_yaml.prompt_trusted_proxy()
    assert proxy == TrustedProxy(
        proxies=("172.18.0.0/16",),
        user_header="x-auth-request-email",
        required_headers=("x-forwarded-proto", "x-forwarded-host"),
        allow_users=("me@example.com",),
    )


def test_trusted_proxy_is_opt_in(prompts):
    prompts.confirm_answers.append(False)
    assert generate_yaml.prompt_trusted_proxy() is None
    assert len(prompts.asked) == 1


def test_trusted_proxy_secrets_skip_the_token_prompts(prompts, capsys):
    prompts.password_answers += ["pw", "", ""]  # password, anthropic, antigravity
    known = {"OPENCLAW_GATEWAY_TOKEN": TOKEN}
    secrets_, generated = generate_yaml.prompt_secrets(known, trusted_proxy=True)
    assert secrets_ == Secrets("", "", "", "pw")
    assert generated is False
    assert "Generate a random gateway token?" not in prompts.asked
    assert "OPENCLAW_GATEWAY_TOKEN ignored" in capsys.readouterr().out


def test_token_secrets_are_still_prompted_without_trusted_proxy(prompts):
    prompts.confirm_answers.append(True)  # generate a token
    prompts.password_answers += ["", ""]  # anthropic, antigravity
    secrets_, generated = generate_yaml.prompt_secrets({}, trusted_proxy=False)
    assert re.fullmatch(r"[0-9a-f]{64}", secrets_.gateway_token)
    assert generated is True
    assert secrets_.gateway_password == ""
