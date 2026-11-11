"""How the Claude CLI is started for a summary.

The CLI is an agent. It is given a transcript as its prompt, so it must start
with no tools and none of the machine's own customizations; and an older CLI
that does not know those flags must fail closed, never run unisolated.
"""
import subprocess

import pytest

from moim_scribe.summarize import claude_cli
from moim_scribe.summarize.claude_cli import ISOLATION_FLAGS, ClaudeCliSummarizer


def _fake_run(monkeypatch, results):
    """results: one (returncode, stdout, stderr) per expected call."""
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        rc, out, err = results[len(calls) - 1]
        return subprocess.CompletedProcess(argv, rc, out, err)

    monkeypatch.setattr(claude_cli.subprocess, "run", run)
    return calls


def test_the_cli_is_started_without_tools_or_customizations(monkeypatch):
    calls = _fake_run(monkeypatch, [(0, "# Summary\n", "")])
    out = ClaudeCliSummarizer("sonnet", executable="/bin/claude").summarize("transcript")
    assert out == "# Summary"
    (argv,) = calls
    assert argv[:4] == ["/bin/claude", "-p", "--model", "sonnet"]
    for flag in ("--safe-mode", "--strict-mcp-config", "--tools="):
        assert flag in argv
    assert ISOLATION_FLAGS == (
        "--safe-mode", "--strict-mcp-config", "--tools=", "--setting-sources", "project",
        "--no-session-persistence",
    )


def test_no_session_is_kept_on_disk(monkeypatch):
    """Without --no-session-persistence the CLI saves the prompt (transcript,
    title, speaker stats, pack roster) in its session store, outside the
    workspace."""
    calls = _fake_run(monkeypatch, [(0, "# Summary\n", "")])
    ClaudeCliSummarizer("sonnet", executable="/bin/claude").summarize("transcript")
    assert "--no-session-persistence" in calls[0]


def test_an_older_cli_without_the_session_flag_fails_closed(monkeypatch):
    calls = _fake_run(monkeypatch, [
        (1, "", "error: unknown option '--no-session-persistence'"),
        (0, "# Summary\n", ""),
    ])
    with pytest.raises(RuntimeError, match="Update Claude Code"):
        ClaudeCliSummarizer("sonnet", executable="/bin/claude").summarize("t")
    assert len(calls) == 1


def test_user_settings_are_excluded_and_the_call_runs_from_an_empty_directory(monkeypatch, tmp_path):
    """Safe mode does not cover settings: on a machine whose user settings ask
    for another language, summaries of an English recording came back in that
    language. So user settings are excluded by flag, and because "project" is
    the narrowest source there is, the working directory must be one that
    holds no project."""
    import os

    seen = {}

    def run(argv, **kw):
        seen["argv"] = argv
        seen["cwd"] = kw.get("cwd")
        seen["cwd_entries"] = os.listdir(kw["cwd"]) if kw.get("cwd") else None
        return subprocess.CompletedProcess(argv, 0, "# Summary\n", "")

    monkeypatch.setattr(claude_cli.subprocess, "run", run)
    (tmp_path / "CLAUDE.md").write_text("reply in French", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    ClaudeCliSummarizer("sonnet", executable="/bin/claude").summarize("transcript")
    i = seen["argv"].index("--setting-sources")
    assert seen["argv"][i + 1] == "project"
    assert seen["cwd"] and os.path.realpath(seen["cwd"]) != os.path.realpath(tmp_path)
    assert seen["cwd_entries"] == []
    assert not os.path.exists(seen["cwd"])        # and it is cleaned up afterwards


def test_an_older_cli_fails_closed_without_a_retry(monkeypatch):
    calls = _fake_run(monkeypatch, [
        (1, "", "error: unknown option '--safe-mode'"),
        (0, "# Summary\n", ""),
    ])
    with pytest.raises(RuntimeError, match="Update Claude Code"):
        ClaudeCliSummarizer("sonnet", executable="/bin/claude").summarize("t")
    assert len(calls) == 1


def test_subprocess_is_started_with_utf8_encoding(monkeypatch):
    seen = {}

    def run(argv, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setattr(claude_cli.subprocess, "run", run)
    ClaudeCliSummarizer("sonnet", executable="/bin/claude").summarize("t")
    assert seen["encoding"] == "utf-8"


def test_any_other_failure_is_not_retried(monkeypatch):
    """A retry without the flags is a downgrade. It must not be reachable by an
    ordinary failure such as a login or rate-limit error."""
    calls = _fake_run(monkeypatch, [(1, "", "Not logged in")])
    with pytest.raises(RuntimeError, match="Not logged in"):
        ClaudeCliSummarizer("sonnet", executable="/bin/claude").summarize("t")
    assert len(calls) == 1
