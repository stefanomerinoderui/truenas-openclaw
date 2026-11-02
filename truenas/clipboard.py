"""Detect a usable clipboard backend for the current OS/environment and copy text.

Detection is based on the platform and on environment variables only (no
subprocess probing), and each candidate is used only if its binary is on PATH.
When no tool is available, the OSC 52 terminal escape sequence is used as a
last resort, which also covers SSH sessions where the terminal runs locally.
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TextIO

COPY_TIMEOUT_SECONDS = 5


class ClipboardError(Exception):
    """Raised when the text could not be handed to the clipboard backend."""


@dataclass(frozen=True)
class Clipboard:
    name: str
    # Command that reads the text from stdin; None = OSC 52 terminal sequence.
    command: tuple[str, ...] | None = None


PBCOPY = Clipboard("pbcopy", ("pbcopy",))
CLIP = Clipboard("clip", ("clip",))
CLIP_EXE_WSL = Clipboard("clip.exe (WSL)", ("clip.exe",))
WL_COPY = Clipboard("wl-copy", ("wl-copy",))
XCLIP = Clipboard("xclip", ("xclip", "-selection", "clipboard", "-in"))
XSEL = Clipboard("xsel", ("xsel", "--clipboard", "--input"))
TERMUX = Clipboard("termux-clipboard-set", ("termux-clipboard-set",))
OSC52 = Clipboard("terminal, OSC 52")


def _candidates(env: Mapping[str, str], platform: str) -> list[Clipboard]:
    """Backends that make sense here, most specific first."""
    if platform == "darwin":
        return [PBCOPY]
    if platform == "win32":
        return [CLIP]
    candidates = []
    if env.get("WSL_DISTRO_NAME") or env.get("WSL_INTEROP"):
        candidates.append(CLIP_EXE_WSL)
    if env.get("WAYLAND_DISPLAY"):
        candidates.append(WL_COPY)
    if env.get("DISPLAY"):
        candidates += [XCLIP, XSEL]
    if env.get("TERMUX_VERSION"):
        candidates.append(TERMUX)
    return candidates


def detect_clipboard(
    *,
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
    stdout_is_tty: bool | None = None,
) -> Clipboard | None:
    """Return the first usable backend, or None if the text can't be copied."""
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    if stdout_is_tty is None:
        stdout_is_tty = sys.stdout.isatty()

    for candidate in _candidates(env, platform):
        if candidate.command and which(candidate.command[0]):
            return candidate
    return OSC52 if stdout_is_tty else None


def copy_to_clipboard(
    text: str, clipboard: Clipboard, *, stream: TextIO | None = None
) -> None:
    if clipboard.command is None:
        _copy_osc52(text, stream or sys.stdout)
        return
    try:
        # DEVNULL, not PIPE: xclip/wl-copy keep running in the background to
        # serve the selection and would block a run() waiting on open pipes.
        subprocess.run(
            clipboard.command,
            input=text.encode("utf-8"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=COPY_TIMEOUT_SECONDS,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as err:
        raise ClipboardError(f"{clipboard.name} failed: {err}") from err


def _copy_osc52(text: str, stream: TextIO) -> None:
    payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
    try:
        stream.write(f"\033]52;c;{payload}\a")
        stream.flush()
    except OSError as err:
        raise ClipboardError(f"cannot write to the terminal: {err}") from err
