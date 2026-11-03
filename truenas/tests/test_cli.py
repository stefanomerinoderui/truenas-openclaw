import json

import pytest

import generate_yaml
from generate_yaml import initial_config
from settings import ConfigError, default_settings_dict


def test_no_config_starts_from_the_defaults():
    assert initial_config(None) == (default_settings_dict(), [])


def test_a_missing_config_file_starts_a_new_config(tmp_path):
    assert initial_config(tmp_path / "new.json") == (default_settings_dict(), [])


def test_a_config_with_invalid_values_still_opens(tmp_path):
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps({"network_mode": "weird", "prot": 1}))
    data, warnings = initial_config(path)
    assert data == {"network_mode": "weird", "prot": 1}
    assert len(warnings) == 1
    assert warnings[0].startswith("prot: unknown setting")


@pytest.mark.parametrize(
    ("text", "message"),
    [('{"network_mode": ', "not valid JSON"), ("[1, 2]", "must contain a JSON object")],
)
def test_a_config_that_cannot_be_edited_is_rejected(tmp_path, text, message):
    path = tmp_path / "cfg.json"
    path.write_text(text)
    with pytest.raises(ConfigError, match=message):
        initial_config(path)


def test_main_needs_an_interactive_terminal(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no ./.env
    assert generate_yaml.main([]) == 1
    assert "needs an interactive terminal" in capsys.readouterr().err


def test_main_reports_a_missing_env_file(tmp_path, capsys):
    assert generate_yaml.main(["--env-file", str(tmp_path / "nope.env")]) == 1
    assert "env file not found" in capsys.readouterr().err


def test_main_reports_a_broken_config(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "cfg.json").write_text("{")
    assert generate_yaml.main(["--config", "cfg.json"]) == 1
    assert "Cannot open the config" in capsys.readouterr().err
