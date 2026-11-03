import asyncio
import json
import stat
from pathlib import Path

import pytest
import yaml
from textual.widgets import Button, ContentSwitcher, Input, Label, RadioButton, Switch

import tui
from clipboard import Clipboard
from draft import field_id
from generate_yaml import initial_config, read_sources
from settings import load_settings, settings_to_dict
from tui import ChoiceScreen, GeneratorApp, PathScreen

TOKEN = "a" * 64
EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.json"


def make_app(tmp_path, data=None, config_path=None, known=None):
    if data is None:
        data, _ = initial_config(None)
    return GeneratorApp(
        data=data,
        config_path=config_path,
        output=tmp_path / "out.yaml",
        known={"OPENCLAW_GATEWAY_TOKEN": TOKEN} if known is None else known,
        env_file=None,
        sources=read_sources(),
    )


def run(app, scenario):
    """Run a scenario against the app in a headless terminal."""

    async def go():
        async with app.run_test(size=(140, 60)) as pilot:
            await pilot.pause()
            await scenario(pilot)

    asyncio.run(go())


def widget(app, path, kind=Input):
    return app.editor.query_one("#" + field_id(path), kind)


def visible(app, path):
    return app.editor.query_one("#row-" + field_id(path)).display


def menu_label(app, key):
    return str(app.editor.query_one(f"#menu-label-{key}", Label).render())


async def set_value(pilot, app, path, value, kind=Input):
    widget(app, path, kind).value = value
    await pilot.pause()


def test_opens_a_config_with_its_values(tmp_path):
    data, _ = initial_config(EXAMPLE)
    app = make_app(tmp_path, data=data, config_path=EXAMPLE)

    async def scenario(pilot):
        assert (
            widget(app, "proxy_network.name").value == "ix-nginx-proxy-manager_default"
        )
        assert widget(app, "timezone").value == "Europe/Madrid"
        assert app.editor.sub_title == str(EXAMPLE)
        assert app.editor.config is not None
        assert not app.editor.query_one("#btn-generate", Button).disabled

    run(app, scenario)


def test_menu_navigation_and_escape_back(tmp_path):
    app = make_app(tmp_path)

    async def scenario(pilot):
        switcher = app.editor.query_one("#sections", ContentSwitcher)
        assert app.focused.id == "menu"
        await pilot.press("down")
        assert switcher.current == "section-network"
        await pilot.press("enter")
        assert app.focused.id == field_id("network_mode")
        await pilot.press("escape")
        assert app.focused.id == "menu"
        await pilot.press("down", "down", "down", "down", "down")
        assert switcher.current == "section-review"

    run(app, scenario)


def test_network_mode_shows_the_matching_fields(tmp_path):
    app = make_app(tmp_path)

    async def scenario(pilot):
        assert visible(app, "public_urls")
        assert not visible(app, "port")
        bridge = f"#{field_id('network_mode')}--opt--bridge"
        app.editor.query_one(bridge, RadioButton).value = True
        await pilot.pause()
        assert not visible(app, "public_urls")
        assert visible(app, "port")
        assert visible(app, "bind_addresses")
        assert not app.editor.query_one("#grp-proxy-network").display
        await set_value(pilot, app, "bridge_extra_network", True, kind=Switch)
        assert app.editor.query_one("#grp-proxy-network").display

    run(app, scenario)


def test_invalid_value_marks_its_field_and_section(tmp_path):
    app = make_app(tmp_path)

    async def scenario(pilot):
        await set_value(pilot, app, "public_urls", "not-a-url")
        error = app.editor.query_one("#err-" + field_id("public_urls"))
        assert "Not an origin URL" in str(error.render())
        assert error.display
        assert menu_label(app, "network") == "Network ⚠"
        assert app.editor.query_one("#btn-generate", Button).disabled
        assert "[Network] public_urls" in str(
            app.editor.query_one("#review-problems").render()
        )
        await set_value(pilot, app, "public_urls", "https://oc.example.com")
        assert menu_label(app, "network") == "Network"
        assert not app.editor.query_one("#btn-generate", Button).disabled

    run(app, scenario)


def test_trusted_proxy_switch_reveals_its_fields_and_drops_the_token(tmp_path):
    app = make_app(tmp_path)

    async def scenario(pilot):
        assert not app.editor.query_one("#grp-trusted").display
        await set_value(pilot, app, "trusted_proxy.enabled", True, kind=Switch)
        assert app.editor.query_one("#grp-trusted").display
        assert menu_label(app, "auth") == "Authentication ⚠"  # no proxy IP yet
        assert not visible(app, "secrets.token")
        assert visible(app, "secrets.password")
        await set_value(pilot, app, "trusted_proxies", '["10.0.0.1", "172.17.0.1"]')
        assert app.editor.config is not None
        assert app.editor.config.secrets.gateway_token == ""
        assert app.editor.config.trusted_proxies == ("10.0.0.1", "172.17.0.1")

    run(app, scenario)


def test_save_writes_the_config_and_clears_the_unsaved_mark(tmp_path):
    target = tmp_path / "cfg.json"
    target.write_text(json.dumps(settings_to_dict(load_settings(EXAMPLE))))
    data, _ = initial_config(target)
    app = make_app(tmp_path, data=data, config_path=target)

    async def scenario(pilot):
        await set_value(pilot, app, "timezone", "UTC")
        assert app.editor.sub_title.endswith(" *")
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert not app.editor.sub_title.endswith(" *")
        assert load_settings(target).timezone == "UTC"
        assert "OPENCLAW_GATEWAY_TOKEN" not in target.read_text()

    run(app, scenario)


def test_save_of_a_new_config_asks_for_a_path(tmp_path):
    app = make_app(tmp_path)
    target = tmp_path / "new.json"

    async def scenario(pilot):
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, PathScreen)
        app.screen.query_one("#path", Input).value = str(target)
        await pilot.press("enter")
        await pilot.pause()
        assert target.exists()
        assert app.config_path == target
        assert app.editor.sub_title == str(target)

    run(app, scenario)


def test_save_as_asks_before_overwriting(tmp_path):
    target = tmp_path / "existing.json"
    target.write_text("keep me")
    app = make_app(tmp_path)

    async def scenario(pilot):
        app.editor.save_as()
        await pilot.pause()
        app.screen.query_one("#path", Input).value = str(target)
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ChoiceScreen)
        await pilot.click("#no")
        await pilot.pause()
        assert target.read_text() == "keep me"

    run(app, scenario)


def test_generate_writes_a_private_yaml_with_the_token(tmp_path):
    app = make_app(tmp_path)
    output = tmp_path / "out.yaml"

    async def scenario(pilot):
        await pilot.press("ctrl+g")
        await pilot.pause()
        env = yaml.safe_load(output.read_text())["services"]["openclaw"]["environment"]
        assert env["OPENCLAW_GATEWAY_TOKEN"] == TOKEN
        assert stat.S_IMODE(output.stat().st_mode) == 0o600

    run(app, scenario)


def test_generate_refuses_an_invalid_config(tmp_path):
    app = make_app(tmp_path)

    async def scenario(pilot):
        await set_value(pilot, app, "timezone", "")
        await pilot.press("ctrl+g")
        await pilot.pause()
        assert not (tmp_path / "out.yaml").exists()
        assert (
            app.editor.query_one("#sections", ContentSwitcher).current
            == "section-review"
        )

    run(app, scenario)


def test_a_token_is_generated_when_none_is_known(tmp_path):
    app = make_app(tmp_path, known={})

    async def scenario(pilot):
        token = widget(app, "secrets.token").value
        assert len(token) == 64
        assert app.editor.config is not None
        app.editor.focus_section("secrets")
        await pilot.pause()
        await pilot.click("#btn-new-token")
        await pilot.pause()
        assert widget(app, "secrets.token").value != token

    run(app, scenario)


def test_show_values_unmasks_the_secrets(tmp_path):
    app = make_app(tmp_path)

    async def scenario(pilot):
        assert widget(app, "secrets.token").password
        app.editor.query_one("#show-secrets", Switch).value = True
        await pilot.pause()
        assert not widget(app, "secrets.token").password

    run(app, scenario)


def test_copy_uses_the_detected_clipboard(tmp_path, monkeypatch):
    copied = []
    backend = Clipboard("fake", ("fake",))
    monkeypatch.setattr(tui, "detect_clipboard", lambda **_: backend)
    monkeypatch.setattr(
        tui, "copy_to_clipboard", lambda text, board: copied.append(board)
    )
    app = make_app(tmp_path)

    async def scenario(pilot):
        app.editor.copy_yaml()
        await pilot.pause()
        assert copied == [backend]

    run(app, scenario)


def test_copy_falls_back_to_the_terminal_clipboard(tmp_path, monkeypatch):
    monkeypatch.setattr(tui, "detect_clipboard", lambda **_: None)
    app = make_app(tmp_path)
    sent = []
    monkeypatch.setattr(app, "copy_to_clipboard", sent.append)

    async def scenario(pilot):
        app.editor.copy_yaml()
        await pilot.pause()
        assert len(sent) == 1
        assert TOKEN in sent[0]

    run(app, scenario)


@pytest.mark.parametrize(("answer", "exits"), [("discard", True), ("cancel", False)])
def test_quit_with_unsaved_changes_asks_first(tmp_path, answer, exits):
    app = make_app(tmp_path)

    async def scenario(pilot):
        await set_value(pilot, app, "timezone", "UTC")
        await pilot.press("ctrl+q")
        await pilot.pause()
        assert isinstance(app.screen, ChoiceScreen)
        await pilot.click(f"#{answer}")
        await pilot.pause()
        assert (app.return_code is not None) is exits

    run(app, scenario)


def test_quit_without_changes_exits_directly(tmp_path):
    app = make_app(tmp_path)

    async def scenario(pilot):
        await pilot.press("ctrl+q")
        await pilot.pause()
        assert app.return_code is not None

    run(app, scenario)


def test_trusted_proxies_field_is_shown_without_trusted_proxy_auth(tmp_path):
    app = make_app(tmp_path)

    async def scenario(pilot):
        assert not widget(app, "trusted_proxy.enabled", Switch).value
        assert visible(app, "trusted_proxies")
        await set_value(pilot, app, "trusted_proxies", "10.0.0.1, 172.17.0.1")
        assert app.editor.config.trusted_proxies == ("10.0.0.1", "172.17.0.1")
        assert app.editor.config.trusted_proxy is None
        assert menu_label(app, "auth") == "Authentication"

    run(app, scenario)
