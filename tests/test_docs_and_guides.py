"""The words shipped around the code in 0.2.1: the Claude guide file, the two
READMEs, the Slack guide and its example script.

A document is a claim about the program. These tests hold the claims that can
be held by a machine: the guide names the real commands and the real folders,
the two READMEs stay the same shape, the links point at files that exist, and
the example script sends what the guide says it sends.
"""
import http.server
import json
import re
import shutil
import subprocess
import threading
import tomllib
from pathlib import Path

import pytest

import moim_scribe
from moim_scribe.headcount import HEADCOUNT_DIR_NAME

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = Path(moim_scribe.__file__).parent / "templates"
WORKSPACE_TEMPLATES = TEMPLATES / "workspace"
READMES = (ROOT / "README.md", ROOT / "README.ko.md")
DOCS = ROOT / "docs"
SCRIPT = DOCS / "examples" / "notify-slack.sh"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _outside_fences(text: str) -> list[str]:
    lines, fenced = [], False
    for ln in text.splitlines():
        if ln.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            lines.append(ln)
    return lines


# --- the Claude guide file ----------------------------------------------------

def test_guide_file_ships_in_both_languages():
    for lang in ("en", "ko"):
        assert (WORKSPACE_TEMPLATES / f"CLAUDE.{lang}.md").is_file()


@pytest.mark.parametrize("lang", ["en", "ko"])
def test_guide_names_both_commands(lang):
    body = _text(WORKSPACE_TEMPLATES / f"CLAUDE.{lang}.md")
    assert "moim-scribe waiting" in body
    assert "moim-scribe count" in body


def test_guide_forbids_opening_the_users_data_unless_asked():
    en = _text(WORKSPACE_TEMPLATES / "CLAUDE.en.md")
    ko = _text(WORKSPACE_TEMPLATES / "CLAUDE.ko.md")
    assert "Do not open the user's data unless they ask" in en
    assert "unless the user asks you to" in en
    assert "시키기 전에는" in ko


@pytest.mark.parametrize("lang", ["en", "ko"])
def test_guide_lists_the_folders_the_workspace_really_has(lang):
    body = _text(WORKSPACE_TEMPLATES / f"CLAUDE.{lang}.md")
    for name in ("inbox/", "results/", "speakers/", "packs/", "models/", f"{HEADCOUNT_DIR_NAME}/",
                 "glossary.txt", ".hf_token", ".soniox_key"):
        assert f"`{name}`" in body, f"the guide does not name {name}"


@pytest.mark.parametrize("lang", ["en", "ko"])
def test_guide_fits_on_one_screen(lang):
    assert len(_text(WORKSPACE_TEMPLATES / f"CLAUDE.{lang}.md").splitlines()) <= 30


# --- versions and install lines ---------------------------------------------------

def test_version_is_the_same_everywhere():
    meta = tomllib.loads(_text(ROOT / "pyproject.toml"))
    assert meta["project"]["version"] == moim_scribe.__version__ == "0.2.1"


@pytest.mark.parametrize("readme", READMES, ids=lambda p: p.name)
def test_install_lines_pin_this_version(readme):
    body = _text(readme)
    assert body.count("@v0.2.1") == 3
    assert "@v0.2.0" not in body


# --- the two READMEs keep one shape ---------------------------------------------

def _headings(path: Path) -> list[str]:
    return [ln for ln in _outside_fences(_text(path)) if re.match(r"#{1,6} ", ln)]


def _tables(path: Path) -> list[int]:
    """Column count of every table (a table = a header row over a |---| row)."""
    lines = _outside_fences(_text(path))
    return [
        lines[i - 1].count("|") - 1
        for i, ln in enumerate(lines)
        if i and re.fullmatch(r"\|(\s*:?-+:?\s*\|)+", ln.strip())
    ]


def test_readmes_have_the_same_headings_and_tables():
    en, ko = READMES
    assert len(_headings(en)) == len(_headings(ko))
    assert [h.split(" ")[0] for h in _headings(en)] == [h.split(" ")[0] for h in _headings(ko)]
    assert _tables(en) == _tables(ko)
    assert len(_tables(en)) >= 6


def test_readmes_have_the_same_number_of_code_blocks():
    en, ko = READMES
    assert _text(en).count("```") == _text(ko).count("```")


@pytest.mark.parametrize("readme", READMES, ids=lambda p: p.name)
def test_readme_tells_the_wait_and_its_off_switch(readme):
    body = _text(readme)
    for needle in ("moim-scribe count", "moim-scribe waiting", "speaker_count_wait_sec",
                   "--no-wait", "CLAUDE.md", ".moim-scribe-headcount"):
        assert needle in body, f"{readme.name} lacks {needle}"


# --- links ------------------------------------------------------------------------

LINK = re.compile(r"\]\((?!https?://|#|mailto:)([^)\s]+)\)")


@pytest.mark.parametrize("doc", [*READMES, *sorted(DOCS.glob("*.md"))], ids=lambda p: p.name)
def test_relative_links_point_at_files(doc):
    for target in LINK.findall(_text(doc)):
        path = (doc.parent / target.split("#")[0]).resolve()
        assert path.exists(), f"{doc.name}: {target} does not exist"


def test_the_slack_guide_is_reachable_from_both_readmes():
    assert "docs/notify-slack.md" in _text(READMES[0])
    assert "docs/notify-slack.ko.md" in _text(READMES[1])


# --- the Slack guide and its script ---------------------------------------------

@pytest.mark.parametrize("name", ["notify-slack.md", "notify-slack.ko.md"])
def test_slack_guides_embed_the_script_as_shipped(name):
    body = _text(DOCS / name)
    assert _text(SCRIPT).rstrip("\n") in body
    assert "speaker_count_wait_sec" in body
    assert "moim-scribe waiting" in body and "moim-scribe count" in body


def test_slack_guides_hold_no_real_webhook():
    for path in (DOCS / "notify-slack.md", DOCS / "notify-slack.ko.md", SCRIPT):
        for url in re.findall(r"https://hooks\.slack\.com/[A-Za-z0-9_./-]*", _text(path)):
            assert url.rstrip(".") in ("https://hooks.slack.com/", "https://hooks.slack.com/..."), url


needs_sh = pytest.mark.skipif(
    shutil.which("sh") is None or shutil.which("curl") is None or shutil.which("cksum") is None,
    reason="the example script needs a POSIX shell and curl",
)


class _Sink:
    """A local stand-in for a Slack webhook that keeps what it is sent."""

    def __init__(self):
        self.bodies: list[bytes] = []
        sink = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                sink.bodies.append(self.rfile.read(int(self.headers["Content-Length"])))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.url = f"http://127.0.0.1:{self.server.server_port}/hook"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


@needs_sh
def test_example_script_sends_each_waiting_recording_once_and_keeps_the_json_valid(tmp_path):
    fake = tmp_path / "bin" / "moim-scribe"
    fake.parent.mkdir()
    fake.write_text('#!/bin/sh\n[ "$1" = waiting ] || exit 9\ncat "$FAKE_WAITING"\n', encoding="utf-8")
    fake.chmod(0o755)
    waiting = tmp_path / "waiting.txt"
    state = tmp_path / "state"

    def run(url: str) -> subprocess.CompletedProcess:
        import os

        env = dict(os.environ, SLACK_WEBHOOK_URL=url, FAKE_WAITING=str(waiting), NOTIFIED_FILE=str(state),
                   PATH=f"{fake.parent}{os.pathsep}{os.environ['PATH']}")
        return subprocess.run(["sh", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60, check=False)

    nasty = '독서 모임 "5월" \\초안.m4a'
    with _Sink() as sink:
        waiting.write_text(f"{nasty}\t3:00 left\nteam sync.m4a\t2:59 left\n", encoding="utf-8")
        assert run(sink.url).returncode == 0
        assert len(sink.bodies) == 1
        text = json.loads(sink.bodies[0])["text"]
        assert nasty in text and "team sync.m4a" in text

        waiting.write_text(f"{nasty}\t1:10 left\nteam sync.m4a\t1:09 left\n", encoding="utf-8")
        assert run(sink.url).returncode == 0
        assert len(sink.bodies) == 1  # same recordings, only the countdown moved

        waiting.write_text(f"{nasty}\t0:30 left\nnew one.m4a\t3:00 left\n", encoding="utf-8")
        assert run(sink.url).returncode == 0
        assert len(sink.bodies) == 2
        second = json.loads(sink.bodies[1])["text"]
        assert "new one.m4a" in second and "team sync.m4a" not in second

        waiting.write_text("", encoding="utf-8")
        assert run(sink.url).returncode == 0
        assert len(sink.bodies) == 2  # nothing waits: nothing sent


@needs_sh
def test_example_script_retries_after_a_failed_send(tmp_path):
    import os

    fake = tmp_path / "bin" / "moim-scribe"
    fake.parent.mkdir()
    fake.write_text('#!/bin/sh\nprintf "a.m4a\\t3:00 left\\n"\n', encoding="utf-8")
    fake.chmod(0o755)
    env = dict(os.environ, NOTIFIED_FILE=str(tmp_path / "state"),
               PATH=f"{fake.parent}{os.pathsep}{os.environ['PATH']}")
    down = subprocess.run(["sh", str(SCRIPT)], env=dict(env, SLACK_WEBHOOK_URL="http://127.0.0.1:1/hook"),
                          capture_output=True, text=True, timeout=60, check=False)
    assert down.returncode != 0
    with _Sink() as sink:
        up = subprocess.run(["sh", str(SCRIPT)], env=dict(env, SLACK_WEBHOOK_URL=sink.url),
                            capture_output=True, text=True, timeout=60, check=False)
        assert up.returncode == 0 and len(sink.bodies) == 1


def test_example_script_refuses_to_run_without_an_address():
    if shutil.which("sh") is None:
        pytest.skip("no POSIX shell")
    import os

    env = {k: v for k, v in os.environ.items() if k != "SLACK_WEBHOOK_URL"}
    done = subprocess.run(["sh", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode != 0 and "SLACK_WEBHOOK_URL" in done.stderr
