"""Printing must survive a console that cannot show every character.

Found on a clean Korean Windows install (code page 949): `verify` printed its
verdict, then raised UnicodeEncodeError on a single dash in the next line.
"""
import ast
import io
import pathlib
import sys

import pytest

from moim_scribe import cli

SRC = pathlib.Path(cli.__file__).parent


@pytest.mark.parametrize("encoding,text", [
    ("cp949", "an em dash \u2014 here"),
    ("cp1252", "arrow \u2192 and Korean \ud55c\uae00"),
    ("ascii", "ellipsis\u2026"),
])
def test_hardened_streams_escape_what_the_console_cannot_show(monkeypatch, encoding, text):
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding=encoding)          # errors="strict", like a console
    with pytest.raises(UnicodeEncodeError):
        io.TextIOWrapper(io.BytesIO(), encoding=encoding).write(text)
    monkeypatch.setattr(sys, "stdout", stream)
    cli.harden_streams()
    print(text)
    stream.flush()
    out = raw.getvalue().decode(encoding)
    assert "\\u" in out                                         # escaped, not dropped, not raised


def test_text_the_console_can_show_is_left_alone(monkeypatch):
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp949")
    monkeypatch.setattr(sys, "stdout", stream)
    cli.harden_streams()
    print("\ud68c\uc758 3\uba85")
    stream.flush()
    assert raw.getvalue().decode("cp949").strip() == "\ud68c\uc758 3\uba85"


def test_main_hardens_the_streams_before_any_command_runs(monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr(cli, "harden_streams", lambda: called.append(True))
    cli.main(["speakers", str(tmp_path / "nope")])
    assert called == [True]


def test_a_stream_that_cannot_be_reconfigured_is_tolerated(monkeypatch):
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    cli.harden_streams()                                          # must not raise


CONSOLE_CALLS = {"print", "_err", "mark", "check"}
LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical"}
UNSAFE = {"\u2014", "\u2013", "\u2026", "\u2225", "\u2192"}


def test_console_messages_use_no_punctuation_that_code_pages_lack():
    """The escape above keeps the tool alive; this keeps its own messages
    readable. Prompt texts and docstrings are not console output and are not
    checked."""
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")
            is_log = isinstance(f, ast.Attribute) and f.attr in LOG_METHODS
            if not (is_log or name in CONSOLE_CALLS):
                continue
            for arg in list(node.args) + [k.value for k in node.keywords]:
                for sub in ast.walk(arg):
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and UNSAFE & set(sub.value):
                        offenders.append(f"{path.name}:{sub.lineno}")
    assert not offenders, offenders
