from pathlib import Path

import pytest

from envfile import EnvFileError, load_secret_values, resolve_env_file


def write_env(tmp_path: Path, text: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(text)
    return path


def test_reads_only_the_secret_variables(tmp_path):
    env = write_env(
        tmp_path,
        "OPENCLAW_GATEWAY_TOKEN=tok\nANTHROPIC_API_KEY=sk-1\nSKIP_UPDATE=1\nOTHER=x\n",
    )
    assert load_secret_values(env, {}) == {
        "OPENCLAW_GATEWAY_TOKEN": "tok",
        "ANTHROPIC_API_KEY": "sk-1",
    }


def test_empty_values_count_as_unset(tmp_path):
    env = write_env(tmp_path, "OPENCLAW_GATEWAY_TOKEN=\nANTIGRAVITY_API_KEY=''\n")
    assert load_secret_values(env, {}) == {}


def test_supports_dotenv_syntax(tmp_path):
    env = write_env(
        tmp_path,
        '# comment\nexport OPENCLAW_GATEWAY_TOKEN="quoted token"  # trailing\n'
        "ANTHROPIC_API_KEY='sk-$notexpanded'\n",
    )
    assert load_secret_values(env, {}) == {
        "OPENCLAW_GATEWAY_TOKEN": "quoted token",
        "ANTHROPIC_API_KEY": "sk-$notexpanded",
    }


def test_real_environment_variables_win_over_the_file(tmp_path):
    env = write_env(tmp_path, "OPENCLAW_GATEWAY_TOKEN=from-file\n")
    environ = {"OPENCLAW_GATEWAY_TOKEN": "from-env", "ANTHROPIC_API_KEY": "sk-env"}
    assert load_secret_values(env, environ) == {
        "OPENCLAW_GATEWAY_TOKEN": "from-env",
        "ANTHROPIC_API_KEY": "sk-env",
    }


def test_works_without_an_env_file():
    assert load_secret_values(None, {"ANTIGRAVITY_API_KEY": "k"}) == {
        "ANTIGRAVITY_API_KEY": "k"
    }


def test_missing_env_file_is_an_error(tmp_path):
    with pytest.raises(EnvFileError, match="not found"):
        load_secret_values(tmp_path / "nope.env", {})


def test_default_env_file_is_used_only_when_present(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert resolve_env_file(None) is None
    (tmp_path / ".env").write_text("X=1\n")
    assert resolve_env_file(None) == Path(".env")


def test_explicit_env_file_is_returned_as_is(tmp_path):
    assert resolve_env_file(tmp_path / "custom.env") == tmp_path / "custom.env"
