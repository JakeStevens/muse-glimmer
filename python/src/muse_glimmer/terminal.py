"""Compact file locations for interactive terminals."""

from __future__ import annotations

import os
import shutil
import sys
import unicodedata
from pathlib import Path


def _width(text: str) -> int:
    return sum(
        0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in "WF" else 1
        for char in text
    )


def _clip(text: str, columns: int, *, tail: bool = False) -> str:
    result = []
    for char in reversed(text) if tail else text:
        columns -= _width(char)
        if columns < 0:
            break
        result.append(char)
    return "".join(reversed(result) if tail else result)


def print_path(path: Path, prefix: str = "Location: ") -> None:
    path = path.expanduser().absolute()
    if not sys.stdout.isatty() or os.environ.get("TERM") == "dumb":
        print(prefix + str(path))
        return

    home = Path.home()
    label = "~/" + str(path.relative_to(home)) if path.is_relative_to(home) else str(path)
    # File names must not inject terminal controls into the visible label.
    label = "".join(char if char.isprintable() else char.encode("unicode_escape").decode() for char in label)
    columns = max(1, shutil.get_terminal_size().columns - _width(prefix) - 1)
    if _width(label) > columns:
        # Prefer keeping the filename; shorten its middle too on narrow terminals.
        tail = min(_width("/" + label.rsplit("/", 1)[-1]), max(0, columns - 4))
        head = _clip(label, columns - tail - 1)
        if "/" in head:
            head = head.rsplit("/", 1)[0] + "/"
        label = head + "…" + _clip(label, tail, tail=True)

    program = os.environ.get("TERM_PROGRAM", "")
    term = os.environ.get("TERM", "")
    vte = os.environ.get("VTE_VERSION", "")
    links = (
        program in {"iTerm.app", "WezTerm", "vscode", "ghostty"}
        or term in {"xterm-kitty", "xterm-ghostty", "foot", "foot-extra"}
        or (vte.isdigit() and int(vte) >= 5000)
    )
    if links and "NO_COLOR" not in os.environ and path.parent.is_dir():
        label = f"\033]8;;{path.parent.as_uri()}\033\\{label}\033]8;;\033\\"
    print(prefix + label)
