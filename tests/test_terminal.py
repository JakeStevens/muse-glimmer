import re
import sys
import unicodedata
from urllib.parse import unquote, urlsplit

import pytest
from muse_glimmer.terminal import print_path


@pytest.fixture
def model(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("VTE_VERSION", raising=False)
    path = tmp_path / ".cache/huggingface/hub/models--owner--model/snapshots" / ("a" * 40)
    path = path / "text-solo" / "模型 café #1.pte"
    path.parent.mkdir(parents=True)
    blob = tmp_path / "blob"
    blob.touch()
    path.symlink_to(blob)
    return path


def test_redirected_path_stays_complete_and_plain(model, monkeypatch, capsys):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    monkeypatch.setenv("COLUMNS", "30")
    print_path(model)
    assert capsys.readouterr().out == f"Location: {model}\n"


@pytest.mark.parametrize("columns", [30, 80, 120])
def test_terminal_link_fits_width_and_opens_containing_folder(model, monkeypatch, capsys, columns):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setenv("COLUMNS", str(columns))
    prefix = "      Location: "
    print_path(model, prefix)
    output = capsys.readouterr().out
    match = re.fullmatch(re.escape(prefix) + r"\x1b\]8;;([^\x1b]+)\x1b\\(.+)\x1b\]8;;\x1b\\\n", output)
    assert match is not None
    target, label = match.groups()
    assert urlsplit(target).scheme == "file"
    assert unquote(urlsplit(target).path) == str(model.parent)  # Snapshot folder, not the file or blob.
    assert label.startswith("~/") and "…" in label and label.endswith(".pte")
    visible = prefix + label
    width = sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in visible)
    assert width < columns


@pytest.mark.parametrize("plain", ["unsupported", "no-color", "dumb"])
def test_plain_terminals_have_no_escape_sequences(model, monkeypatch, capsys, plain):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setenv("COLUMNS", "80")
    if plain == "unsupported":
        monkeypatch.setenv("TERM_PROGRAM", "Apple_Terminal")
    elif plain == "no-color":
        monkeypatch.setenv("NO_COLOR", "")
    else:
        monkeypatch.setenv("TERM", "dumb")
    print_path(model)
    output = capsys.readouterr().out
    assert "\x1b" not in output
    if plain == "dumb":
        assert output == f"Location: {model}\n"
    else:
        assert output.startswith("Location: ~/") and "…" in output


def test_control_characters_cannot_escape_terminal_link(model, monkeypatch, capsys):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setenv("COLUMNS", "500")
    model = model.parent / "folder\x1b\x07\n" / "unsafe\x1b\x07\n.pte"
    model.parent.mkdir()
    model.touch()
    print_path(model)
    output = capsys.readouterr().out
    assert output.count("\x1b") == 4  # Only the hyperlink wrappers.
    assert output.count("\n") == 1 and "\x07" not in output
    assert "folder%1B%07%0A" in output
    assert "unsafe\\x1b\\x07\\n.pte" in output
