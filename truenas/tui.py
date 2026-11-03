"""Full-screen editor for the generator settings (Textual).

The editor is a menu of sections on the left and the selected section's form on
the right. Every change re-validates the whole form through ``draft.validate``;
problems are shown under their field and as a mark on their section. Secrets
come from the env file or are typed in, and are never written to the config.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, ClassVar

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widget import Widget
from textual.widgets import (
    Button,
    ContentSwitcher,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    RadioButton,
    RadioSet,
    Static,
    Switch,
    TextArea,
)

from clipboard import ClipboardError, copy_to_clipboard, detect_clipboard
from compose_builder import AppConfig, ExtraNetwork, build_compose, dump_yaml
from draft import (
    FIELDS,
    LIST_HINT,
    SECTIONS,
    FormValues,
    dict_from_values,
    field_for,
    field_id,
    ignored_secret_notice,
    new_gateway_token,
    proxy_ip_hint,
    secret_values,
    secrets_from_values,
    section_of,
    summary_lines,
    validate,
    values_from_dict,
)
from envfile import SECRET_VARIABLES
from settings import Settings, build_app_config, format_issue, save_settings

DEFAULT_CONFIG = Path("openclaw-truenas.json")

CHOICES: dict[str, tuple[tuple[str, str], ...]] = {
    "image_source": (
        ("inline", "Build on TrueNAS from the embedded Dockerfile (recommended)"),
        ("prebuilt", "Pull a prebuilt image from a registry"),
    ),
    "network_mode": (
        ("proxy", "Reverse proxy: join its Docker network, no published ports"),
        ("bridge", "Bridge: publish the gateway port on TrueNAS"),
        ("host", "Host network: use the TrueNAS network stack"),
    ),
    "storage.kind": (
        ("host", "Host paths on a dataset (recommended, easy to back up)"),
        ("volumes", "Docker named volumes (managed by TrueNAS)"),
    ),
}


def write_private_file(text: str, path: Path) -> None:
    # The YAML contains the gateway token and API keys: owner-only permissions.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def row_id(path: str) -> str:
    return "row-" + field_id(path)


def error_id(path: str) -> str:
    return "err-" + field_id(path)


# --- form building blocks -----------------------------------------------------


def _field(path: str, label: str, control: Widget, hint: str = "") -> Vertical:
    children: list[Widget] = [Label(label, classes="field-label"), control]
    if hint:
        children.append(Static(hint, classes="hint", markup=False))
    children.append(Static("", id=error_id(path), classes="error", markup=False))
    return Vertical(*children, id=row_id(path), classes="field")


def text_field(
    path: str, label: str, values: FormValues, hint: str = "", password: bool = False
) -> Vertical:
    control = Input(value=values[path], id=field_id(path), password=password)
    return _field(path, label, control, hint)


def list_field(path: str, label: str, values: FormValues, hint: str = "") -> Vertical:
    return text_field(
        path, label, values, f"{hint} ({LIST_HINT})" if hint else LIST_HINT
    )


def choice_field(path: str, label: str, values: FormValues) -> Vertical:
    buttons = [
        RadioButton(text, value=values[path] == key, id=f"{field_id(path)}--opt--{key}")
        for key, text in CHOICES[path]
    ]
    return _field(path, label, RadioSet(*buttons, id=field_id(path)))


def switch_field(path: str, label: str, values: FormValues, hint: str = "") -> Vertical:
    children: list[Widget] = [
        Horizontal(
            Switch(values[path], id=field_id(path)),
            Label(label, classes="switch-label"),
            classes="switch-row",
        )
    ]
    if hint:
        children.append(Static(hint, classes="hint", markup=False))
    children.append(Static("", id=error_id(path), classes="error", markup=False))
    return Vertical(*children, id=row_id(path), classes="field")


def radio_value(radio_set: RadioSet) -> str:
    button = radio_set.pressed_button
    return button.id.rsplit("--opt--", 1)[1] if button and button.id else ""


# --- modal dialogs --------------------------------------------------------------


class ChoiceScreen(ModalScreen[str | None]):
    """A message with buttons; dismisses with the pressed button's id."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "dismiss(None)", "Cancel")
    ]

    def __init__(self, message: str, buttons: list[tuple[str, str, str]]) -> None:
        super().__init__()
        self.message = message
        self.buttons = buttons  # (result id, label, variant)

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(self.message, markup=False)
            with Horizontal(classes="buttons"):
                for result, label, variant in self.buttons:
                    yield Button(label, id=result, variant=variant)

    @on(Button.Pressed)
    def choose(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id)


class PathScreen(ModalScreen[Path | None]):
    """Ask for a file path."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "dismiss(None)", "Cancel")
    ]

    def __init__(self, title: str, default: Path) -> None:
        super().__init__()
        self.title_text = title
        self.default = default

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(self.title_text, markup=False)
            yield Input(value=str(self.default), id="path")
            with Horizontal(classes="buttons"):
                yield Button("Save", id="ok", variant="primary")
                yield Button("Cancel", id="cancel")

    def _submit(self) -> None:
        text = self.query_one("#path", Input).value.strip()
        self.dismiss(Path(text) if text else None)

    @on(Input.Submitted)
    def submitted(self) -> None:
        self._submit()

    @on(Button.Pressed, "#ok")
    def ok(self) -> None:
        self._submit()

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:
        self.dismiss(None)


# --- the editor -------------------------------------------------------------------


class EditorScreen(Screen[None]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+s", "save", "Save config"),
        Binding("ctrl+g", "generate", "Generate YAML"),
        Binding("escape", "back", "Menu"),
    ]

    def __init__(self, values: FormValues, known: Mapping[str, str]) -> None:
        super().__init__()
        self.initial_values = values
        self.known = known
        self.config: AppConfig | None = None
        self.settings: Settings | None = None
        self.saved_snapshot: dict[str, Any] | None = None

    # -- layout --

    def compose(self) -> ComposeResult:
        v = self.initial_values
        yield Header()
        with Horizontal():
            yield ListView(
                *[
                    ListItem(Label(title, id=f"menu-label-{key}"), id=f"menu-{key}")
                    for key, title in SECTIONS
                ],
                id="menu",
            )
            with ContentSwitcher(initial="section-image", id="sections"):
                with VerticalScroll(id="section-image"):
                    yield Static("Image", classes="section-title")
                    yield choice_field(
                        "image_source", "Where TrueNAS gets the image", v
                    )
                    yield text_field("image", "Image reference", v)
                with VerticalScroll(id="section-network"):
                    yield from self._network_section(v)
                with VerticalScroll(id="section-auth"):
                    yield from self._auth_section(v)
                with VerticalScroll(id="section-storage"):
                    yield Static("Storage", classes="section-title")
                    yield choice_field(
                        "storage.kind", "Packages, config and credentials", v
                    )
                    yield text_field(
                        "storage.base_path",
                        "Base dataset path",
                        v,
                        "Subfolders tools/openclaw/claude/gemini are created and given to uid 1000.",
                    )
                with VerticalScroll(id="section-environment"):
                    yield Static("Environment", classes="section-title")
                    yield text_field(
                        "timezone", "Timezone", v, "IANA name, e.g. Europe/Madrid"
                    )
                    yield list_field("packages", "Global pnpm packages", v)
                    yield switch_field(
                        "skip_update",
                        "Skip package updates on start (only install missing ones)",
                        v,
                    )
                with VerticalScroll(id="section-secrets"):
                    yield from self._secrets_section(v)
                with VerticalScroll(id="section-review"):
                    yield from self._review_section()
        yield Footer()

    def _network_section(self, v: FormValues) -> ComposeResult:
        yield Static("Network", classes="section-title")
        yield choice_field("network_mode", "Network mode", v)
        yield switch_field(
            "bridge_extra_network",
            "Also join an existing Docker network (e.g. a reverse proxy's)",
            v,
        )
        yield Vertical(
            text_field(
                "proxy_network.name",
                "Existing network name",
                v,
                "TrueNAS names app networks ix-<app name>_default.",
            ),
            text_field(
                "proxy_network.hostname", "Hostname of the openclaw container on it", v
            ),
            id="grp-proxy-network",
            classes="group",
        )
        yield list_field("public_urls", "Public URL(s) served by the proxy", v)
        yield text_field("port", "Gateway port on TrueNAS", v)
        yield list_field(
            "bind_addresses", "Bind addresses", v, "0.0.0.0 = all interfaces"
        )
        yield list_field(
            "hosts", "TrueNAS hostnames/IPs used to open the Control UI", v
        )
        yield list_field("extra_origins", "Extra allowed origins (optional)", v)

    def _auth_section(self, v: FormValues) -> ComposeResult:
        yield Static("Authentication", classes="section-title")
        yield list_field(
            "trusted_proxies",
            "Trusted proxies (gateway.trustedProxies)",
            v,
            "IPs (or CIDRs) of your reverse proxies, one or more, e.g. "
            "10.0.0.1, 172.17.0.1. Optional with token auth, where it lets the "
            "gateway trust their X-Forwarded-* headers; required for trusted-proxy "
            "auth, where each one can assert any user identity",
        )
        yield Static("", id="proxy-ip-hint", classes="hint", markup=False)
        yield switch_field(
            "trusted_proxy.enabled",
            "Delegate authentication to the reverse proxy (trusted-proxy auth)",
            v,
            "Off: the Control UI is protected by the gateway token. On: an "
            "identity-aware proxy (oauth2-proxy, Pomerium...) authenticates users and "
            "forwards their identity in a header; it must overwrite that header and "
            "X-Forwarded-* instead of appending to them.",
        )
        yield Vertical(
            text_field(
                "trusted_proxy.user_header",
                "Header with the authenticated user",
                v,
                "x-auth-request-email for oauth2-proxy, x-pomerium-claim-email for Pomerium",
            ),
            list_field(
                "trusted_proxy.required_headers",
                "Headers the proxy must always send (optional)",
                v,
            ),
            list_field(
                "trusted_proxy.allow_users",
                "Allowed users (empty = every user the proxy authenticates)",
                v,
            ),
            switch_field(
                "trusted_proxy.allow_loopback",
                "Allow a same-host loopback proxy",
                v,
            ),
            switch_field(
                "trusted_proxy.device_auto_approve.enabled",
                "Auto-approve new UI operator devices",
                v,
            ),
            list_field(
                "trusted_proxy.device_auto_approve.scopes",
                "Scopes for auto-approved devices (empty = OpenClaw defaults)",
                v,
                "e.g. operator.read, operator.write",
            ),
            _field(
                "trusted_proxy.identity_scopes",
                "Per-identity operator scopes (JSON object, optional)",
                TextArea(
                    v["trusted_proxy.identity_scopes"],
                    id=field_id("trusted_proxy.identity_scopes"),
                    soft_wrap=True,
                ),
                '{"admin@example.com": ["operator.admin"]}',
            ),
            id="grp-trusted",
            classes="group",
        )

    def _secrets_section(self, v: FormValues) -> ComposeResult:
        yield Static("Secrets", classes="section-title")
        yield Static(
            "Never saved to the JSON config. Values found in the env file or the "
            "environment are pre-filled.",
            classes="hint",
        )
        yield Static("", id="secrets-loaded", classes="hint", markup=False)
        yield Static("", id="secret-notice", classes="warning", markup=False)
        yield Vertical(
            Label("Gateway token", classes="field-label"),
            Horizontal(
                Input(v["secrets.token"], id=field_id("secrets.token"), password=True),
                Button("Generate", id="btn-new-token"),
                classes="input-with-button",
            ),
            Static("", id=error_id("secrets.token"), classes="error", markup=False),
            id=row_id("secrets.token"),
            classes="field",
        )
        yield text_field(
            "secrets.password",
            "OPENCLAW_GATEWAY_PASSWORD (optional, lets internal clients bypass the proxy)",
            v,
            password=True,
        )
        yield text_field(
            "secrets.anthropic",
            "ANTHROPIC_API_KEY (optional, empty = log in with `claude` later)",
            v,
            password=True,
        )
        yield text_field(
            "secrets.antigravity",
            "ANTIGRAVITY_API_KEY (optional, empty = log in with `agy` later)",
            v,
            password=True,
        )
        yield Horizontal(
            Switch(False, id="show-secrets"),
            Label("Show values", classes="switch-label"),
            classes="switch-row",
        )

    def _review_section(self) -> ComposeResult:
        yield Static("Review & generate", classes="section-title")
        yield Static("", id="review-problems", classes="error", markup=False)
        yield Static("", id="review-summary", markup=False)
        yield Label("Output file", classes="field-label")
        yield Input(str(self.app.output), id="output-path")
        with Horizontal(classes="buttons"):
            yield Button("Generate YAML", id="btn-generate", variant="primary")
            yield Button("Copy YAML to clipboard", id="btn-copy")
            yield Button("Save config", id="btn-save")
            yield Button("Save config as...", id="btn-save-as")

    # -- lifecycle --

    def on_mount(self) -> None:
        self.saved_snapshot = self.config_dict()
        self.refresh_state()
        self.query_one("#menu", ListView).focus()
        loaded = ", ".join(name for name in SECRET_VARIABLES if name in self.known)
        source = self.app.env_file or "the environment"
        lines = [f"Loaded from {source}: {loaded}"] if loaded else []
        if self.app.token_generated:
            lines.append(
                "No gateway token found, so a random one was generated. Put it in your "
                "env file as OPENCLAW_GATEWAY_TOKEN to keep it when you regenerate."
            )
        self.query_one("#secrets-loaded", Static).update("\n".join(lines))
        if self.app.warnings:
            self.notify(
                "\n".join(self.app.warnings),
                title="Ignored settings in the config",
                severity="warning",
                timeout=15,
            )

    # -- reading the form --

    def collect(self) -> FormValues:
        values: FormValues = {}
        for path, kind in FIELDS.items():
            widget = self.query_one("#" + field_id(path))
            if kind == "choice":
                values[path] = radio_value(widget)  # type: ignore[arg-type]
            elif kind == "json":
                values[path] = widget.text  # type: ignore[attr-defined]
            else:
                values[path] = widget.value  # type: ignore[attr-defined]
        return values

    def config_dict(self) -> dict[str, Any]:
        return dict_from_values(self.collect())[0]

    def is_dirty(self) -> bool:
        return self.config_dict() != self.saved_snapshot

    # -- reacting to changes --

    @on(Input.Changed)
    @on(Switch.Changed)
    @on(RadioSet.Changed)
    @on(TextArea.Changed)
    def changed(self, event: Any) -> None:
        control = getattr(event, "control", None)
        if control is not None and control.id == "show-secrets":
            for path in (
                "secrets.token",
                "secrets.password",
                "secrets.anthropic",
                "secrets.antigravity",
            ):
                self.query_one("#" + field_id(path), Input).password = not event.value
            return
        if control is not None and control.id == "output-path":
            return
        self.refresh_state()

    def refresh_state(self) -> None:
        values = self.collect()
        self.update_visibility(values)
        settings, issues = validate(values)
        trusted = values["trusted_proxy.enabled"] is True
        secrets_, secret_issues = secrets_from_values(values, trusted)
        issues = issues + secret_issues

        config = None
        if settings and secrets_:
            try:
                config = build_app_config(settings, secrets_)
            except ValueError as err:
                issues.append(("", str(err)))
        self.settings, self.config = settings, config

        self.show_issues(issues)
        self.update_review(config, issues)
        self.update_hints(values, trusted)
        self.update_title()

    def update_visibility(self, v: FormValues) -> None:
        mode = v["network_mode"]
        trusted = v["trusted_proxy.enabled"] is True
        shown = {
            row_id("image"): v["image_source"] == "prebuilt",
            row_id("bridge_extra_network"): mode == "bridge",
            "grp-proxy-network": mode == "proxy"
            or (mode == "bridge" and v["bridge_extra_network"] is True),
            row_id("public_urls"): mode == "proxy",
            row_id("port"): mode in ("bridge", "host"),
            row_id("bind_addresses"): mode == "bridge",
            row_id("hosts"): mode in ("bridge", "host"),
            row_id("extra_origins"): mode in ("bridge", "host"),
            "grp-trusted": trusted,
            row_id("trusted_proxy.device_auto_approve.scopes"): v[
                "trusted_proxy.device_auto_approve.enabled"
            ]
            is True,
            row_id("storage.base_path"): v["storage.kind"] == "host",
            row_id("secrets.token"): not trusted,
            row_id("secrets.password"): trusted,
        }
        for widget_id, visible in shown.items():
            self.query_one("#" + widget_id).display = visible

    def show_issues(self, issues: list[tuple[str, str]]) -> None:
        messages: dict[str, list[str]] = {}
        sections_with_issues: set[str] = set()
        for path, message in issues:
            sections_with_issues.add(section_of(path))
            field = field_for(path) if path else None
            if field:
                messages.setdefault(field, []).append(message)
        for path in FIELDS:
            error = self.query_one("#" + error_id(path), Static)
            text = "\n".join(messages.get(path, []))
            error.update(text)
            error.display = bool(text)
        for key, title in SECTIONS:
            mark = " ⚠" if key in sections_with_issues else ""
            self.query_one(f"#menu-label-{key}", Label).update(title + mark)

    def update_review(
        self, config: AppConfig | None, issues: list[tuple[str, str]]
    ) -> None:
        problems = self.query_one("#review-problems", Static)
        if issues:
            titles = dict(SECTIONS)
            problems.update(
                "Fix these before saving or generating:\n"
                + "\n".join(
                    f"  [{titles[section_of(path)]}] {format_issue((path, message))}"
                    for path, message in issues
                )
            )
        problems.display = bool(issues)
        summary = self.query_one("#review-summary", Static)
        summary.update("\n".join(summary_lines(config)) if config else "")
        for button_id in ("#btn-generate", "#btn-copy"):
            self.query_one(button_id, Button).disabled = config is None
        for button_id in ("#btn-save", "#btn-save-as"):
            self.query_one(button_id, Button).disabled = self.settings is None

    def update_hints(self, v: FormValues, trusted: bool) -> None:
        mode = v["network_mode"]
        joins = mode == "proxy" or (
            mode == "bridge" and v["bridge_extra_network"] is True
        )
        network = (
            ExtraNetwork(str(v["proxy_network.name"]), str(v["proxy_network.hostname"]))
            if joins
            else None
        )
        self.query_one("#proxy-ip-hint", Static).update(proxy_ip_hint(network))
        notice = self.query_one("#secret-notice", Static)
        text = ignored_secret_notice(self.known, trusted) or ""
        notice.update(text)
        notice.display = bool(text)

    def update_title(self) -> None:
        path = self.app.config_path
        name = str(path) if path else "new config (not saved)"
        self.sub_title = name + (" *" if self.is_dirty() else "")

    # -- navigation --

    @on(ListView.Highlighted, "#menu")
    def show_section(self, event: ListView.Highlighted) -> None:
        if event.item is not None and event.item.id:
            key = event.item.id.removeprefix("menu-")
            self.query_one("#sections", ContentSwitcher).current = f"section-{key}"

    @on(ListView.Selected, "#menu")
    def enter_section(self, event: ListView.Selected) -> None:
        if event.item.id:
            self.focus_section(event.item.id.removeprefix("menu-"))

    def focus_section(self, key: str) -> None:
        self.query_one("#sections", ContentSwitcher).current = f"section-{key}"
        self.query_one("#menu", ListView).index = [k for k, _ in SECTIONS].index(key)
        section = self.query_one(f"#section-{key}")
        for widget in section.query("Input, Switch, RadioSet, TextArea, Button"):
            if widget.display and widget.focusable and _visible(widget):
                widget.focus()
                return

    def action_back(self) -> None:
        self.query_one("#menu", ListView).focus()

    # -- actions --

    @on(Button.Pressed, "#btn-new-token")
    def new_token(self) -> None:
        self.query_one(
            "#" + field_id("secrets.token"), Input
        ).value = new_gateway_token()
        self.notify("Generated a new gateway token. Turn on 'Show values' to see it.")

    @on(Button.Pressed, "#btn-generate")
    def action_generate(self) -> None:
        text = self.yaml_text()
        if text is None:
            return
        output = Path(
            self.query_one("#output-path", Input).value.strip()
            or "openclaw-truenas.yaml"
        )
        try:
            write_private_file(text, output)
        except OSError as err:
            self.notify(f"Could not write {output}: {err}", severity="error")
            return
        self.notify(f"Wrote {output.resolve()} (mode 600, contains secrets).")

    @on(Button.Pressed, "#btn-copy")
    def copy_yaml(self) -> None:
        text = self.yaml_text()
        if text is None:
            return
        clipboard = detect_clipboard(stdout_is_tty=True)
        if clipboard is None or clipboard.command is None:
            # Textual owns the terminal, so let it send the OSC 52 sequence.
            self.app.copy_to_clipboard(text)
            self.notify(
                "Sent to the terminal clipboard (OSC 52); paste to check it worked. "
                "Clear the clipboard after pasting: it holds secrets."
            )
            return
        try:
            copy_to_clipboard(text, clipboard)
        except ClipboardError as err:
            self.notify(f"Could not copy: {err}", severity="error")
            return
        self.notify(
            f"Copied with {clipboard.name}. The clipboard holds the token and API keys: "
            "clear it after pasting."
        )

    def yaml_text(self) -> str | None:
        if self.config is None:
            self.notify("Fix the problems listed under Review first.", severity="error")
            self.focus_section("review")
            return None
        return dump_yaml(build_compose(self.config, *self.app.sources))

    @on(Button.Pressed, "#btn-save")
    def action_save(self) -> None:
        self.request_save()

    @on(Button.Pressed, "#btn-save-as")
    def save_as(self) -> None:
        self.request_save(ask_path=True)

    def request_save(
        self, ask_path: bool = False, then: Callable[[], None] | None = None
    ) -> None:
        """Save the config, asking for a path when needed; run ``then`` on success."""
        settings = self.settings
        if settings is None:
            self.notify(
                "The config has problems; fix them before saving.", severity="error"
            )
            self.focus_section("review")
            return
        current = self.app.config_path
        if current is not None and not ask_path:
            self.save_to(current, settings, then)
            return

        def chosen(path: Path | None) -> None:
            if path is None:
                return
            if path.exists() and path != current:

                def confirmed(answer: str | None) -> None:
                    if answer == "yes":
                        self.save_to(path, settings, then)

                self.app.push_screen(
                    ChoiceScreen(
                        f"{path} already exists. Overwrite it?",
                        [("yes", "Overwrite", "warning"), ("no", "Cancel", "default")],
                    ),
                    confirmed,
                )
            else:
                self.save_to(path, settings, then)

        self.app.push_screen(
            PathScreen("Save the config (no secrets) as:", current or DEFAULT_CONFIG),
            chosen,
        )

    def save_to(
        self, path: Path, settings: Settings, then: Callable[[], None] | None
    ) -> None:
        try:
            save_settings(settings, path)
        except OSError as err:
            self.notify(f"Could not save {path}: {err}", severity="error")
            return
        self.app.config_path = path
        self.saved_snapshot = self.config_dict()
        self.update_title()
        self.notify(f"Saved {path}.")
        if then:
            then()


def _visible(widget: Widget) -> bool:
    """True when the widget and all its ancestors are displayed."""
    node: Any = widget
    while node is not None:
        if getattr(node, "display", True) is False:
            return False
        node = node.parent
    return True


class GeneratorApp(App[None]):
    TITLE = "OpenClaw TrueNAS generator"
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+q", "quit", "Quit", priority=True)
    ]
    CSS = """
    #menu { width: 30; border-right: solid $primary-darken-2; }
    #sections { padding: 0 2; }
    .section-title { text-style: bold; margin: 1 0; }
    .field { height: auto; margin-bottom: 1; }
    .field-label { text-style: bold; }
    .hint { color: $text-muted; }
    .error { color: $error; }
    .warning { color: $warning; }
    .switch-row { height: auto; }
    .switch-label { padding: 1 1; }
    .group { height: auto; border-left: tall $primary; padding-left: 1; margin-bottom: 1; }
    .input-with-button { height: auto; }
    .input-with-button Input { width: 1fr; }
    TextArea { height: 6; }
    #review-summary { margin-bottom: 1; }
    #proxy-ip-hint { margin-bottom: 1; }
    .buttons { height: auto; margin-top: 1; }
    .buttons Button { margin-right: 1; }
    ChoiceScreen, PathScreen { align: center middle; }
    .dialog {
        width: 76; height: auto; padding: 1 2;
        border: thick $primary; background: $surface;
    }
    """

    def __init__(
        self,
        *,
        data: Mapping[str, Any],
        config_path: Path | None,
        output: Path,
        known: Mapping[str, str],
        env_file: Path | None,
        sources: tuple[str, str],
        warnings: list[str] | None = None,
    ) -> None:
        super().__init__()
        self.config_path = config_path
        self.output = output
        self.env_file = env_file
        self.sources = sources
        self.warnings = warnings or []
        secrets_ = secret_values(known)
        self.token_generated = not secrets_["secrets.token"]
        if self.token_generated:
            secrets_["secrets.token"] = new_gateway_token()
        self.editor = EditorScreen({**values_from_dict(data), **secrets_}, known)

    def get_default_screen(self) -> Screen[Any]:
        return self.editor

    async def action_quit(self) -> None:
        if not self.editor.is_dirty():
            self.exit()
            return

        def answered(answer: str | None) -> None:
            if answer == "save":
                self.editor.request_save(then=self.exit)
            elif answer == "discard":
                self.exit()

        self.push_screen(
            ChoiceScreen(
                "The config has unsaved changes.",
                [
                    ("save", "Save and quit", "primary"),
                    ("discard", "Quit without saving", "error"),
                    ("cancel", "Cancel", "default"),
                ],
            ),
            answered,
        )
