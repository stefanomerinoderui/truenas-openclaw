import base64
import io
import sys

import pytest

import generate_yaml
from clipboard import (
    CLIP,
    CLIP_EXE_WSL,
    OSC52,
    PBCOPY,
    TERMUX,
    WL_COPY,
    XCLIP,
    XSEL,
    Clipboard,
    ClipboardError,
    copy_to_clipboard,
    detect_clipboard,
)


def detect(env=None, platform="linux", installed=(), tty=False):
    return detect_clipboard(
        env=env or {},
        platform=platform,
        which=lambda name: f"/usr/bin/{name}" if name in installed else None,
        stdout_is_tty=tty,
    )


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"platform": "darwin", "installed": ["pbcopy"]}, PBCOPY),
        ({"platform": "win32", "installed": ["clip"]}, CLIP),
        (
            {"env": {"WAYLAND_DISPLAY": "wayland-1"}, "installed": ["wl-copy"]},
            WL_COPY,
        ),
        ({"env": {"DISPLAY": ":0"}, "installed": ["xclip", "xsel"]}, XCLIP),
        ({"env": {"DISPLAY": ":0"}, "installed": ["xsel"]}, XSEL),
        (
            {"env": {"WSL_DISTRO_NAME": "Ubuntu"}, "installed": ["clip.exe"]},
            CLIP_EXE_WSL,
        ),
        (
            {"env": {"TERMUX_VERSION": "0.118"}, "installed": ["termux-clipboard-set"]},
            TERMUX,
        ),
    ],
)
def test_detects_backend_for_environment(kwargs, expected):
    assert detect(**kwargs) == expected


def test_wayland_with_x11_falls_back_when_wl_copy_is_missing():
    env = {"WAYLAND_DISPLAY": "wayland-1", "DISPLAY": ":0"}
    assert detect(env=env, installed=["xclip"]) == XCLIP


def test_wayland_prefers_wl_copy_over_xclip():
    env = {"WAYLAND_DISPLAY": "wayland-1", "DISPLAY": ":0"}
    assert detect(env=env, installed=["xclip", "wl-copy"]) == WL_COPY


def test_tool_not_on_path_is_skipped():
    assert detect(platform="darwin", installed=[], tty=False) is None


def test_display_without_tools_uses_osc52_on_a_terminal():
    assert detect(env={"DISPLAY": ":0"}, tty=True) == OSC52


def test_headless_ssh_session_uses_osc52():
    env = {"SSH_CONNECTION": "1.2.3.4 5 6.7.8.9 22"}
    assert detect(env=env, tty=True) == OSC52


def test_nothing_available_without_a_terminal():
    assert detect(tty=False) is None


def test_command_backend_receives_text_on_stdin(tmp_path):
    target = tmp_path / "clip.txt"
    script = f"import sys; open({str(target)!r}, 'wb').write(sys.stdin.buffer.read())"
    clipboard = Clipboard("fake", (sys.executable, "-c", script))
    copy_to_clipboard("token: tok$en\n", clipboard)
    assert target.read_text() == "token: tok$en\n"


def test_failing_command_raises_clipboard_error():
    clipboard = Clipboard("fake", (sys.executable, "-c", "raise SystemExit(3)"))
    with pytest.raises(ClipboardError, match="fake failed"):
        copy_to_clipboard("x", clipboard)


def test_missing_binary_raises_clipboard_error():
    with pytest.raises(ClipboardError):
        copy_to_clipboard("x", Clipboard("ghost", ("definitely-not-installed",)))


def test_osc52_writes_base64_escape_sequence():
    stream = io.StringIO()
    copy_to_clipboard("hello", OSC52, stream=stream)
    payload = base64.b64encode(b"hello").decode()
    assert stream.getvalue() == f"\033]52;c;{payload}\a"


def test_token_question_skipped_when_token_was_typed_in(prompts):
    generate_yaml.offer_token("my-own-token", generated=False)
    assert prompts.asked == []


def test_token_question_asked_for_generated_token(prompts):
    generate_yaml.offer_token("generated", generated=True)
    assert prompts.asked == ["Show the gateway token now?"]


def test_clipboard_offer_names_backend_and_copies(prompts, monkeypatch):
    copied = []
    monkeypatch.setattr(generate_yaml, "detect_clipboard", lambda: WL_COPY)
    monkeypatch.setattr(
        generate_yaml,
        "copy_to_clipboard",
        lambda text, board: copied.append((text, board)),
    )
    generate_yaml.offer_clipboard_copy("yaml-text")
    assert prompts.asked == ["Copy the YAML to your clipboard (wl-copy)?"]
    assert copied == [("yaml-text", WL_COPY)]


def test_clipboard_offer_respects_no(prompts, monkeypatch):
    prompts.confirm_answers.append(False)
    copied = []
    monkeypatch.setattr(generate_yaml, "detect_clipboard", lambda: WL_COPY)
    monkeypatch.setattr(
        generate_yaml, "copy_to_clipboard", lambda text, board: copied.append(text)
    )
    generate_yaml.offer_clipboard_copy("yaml-text")
    assert copied == []


def test_clipboard_offer_not_asked_without_backend(prompts, monkeypatch):
    monkeypatch.setattr(generate_yaml, "detect_clipboard", lambda: None)
    generate_yaml.offer_clipboard_copy("yaml-text")
    assert prompts.asked == []


def test_clipboard_failure_is_reported_not_raised(prompts, monkeypatch):
    def fail(text, board):
        raise ClipboardError("boom")

    monkeypatch.setattr(generate_yaml, "detect_clipboard", lambda: WL_COPY)
    monkeypatch.setattr(generate_yaml, "copy_to_clipboard", fail)
    generate_yaml.offer_clipboard_copy("yaml-text")  # must not raise
