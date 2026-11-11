"""How the Claude CLI is started for a summary.

The CLI is an agent. It is given a transcript as its prompt, so it must start
with no tools and none of the machine's own customizations; and an older CLI
that does not know those flags must still produce a summary.
"""
import logging
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
    assert set(ISOLATION_FLAGS) == {"--safe-mode", "--strict-mcp-config", "--tools="}


def test_an_older_cli_gets_one_retry_without_the_flags(monkeypatch, caplog):
    calls = _fake_run(monkeypatch, [
        (1, "", "error: unknown option '--safe-mode'"),
        (0, "# Summary\n", ""),
    ])
    with caplog.at_level(logging.WARNING, logger="moim_scribe.summarize.claude_cli"):
        out = ClaudeCliSummarizer("sonnet", executable="/bin/claude").summarize("t")
    assert out == "# Summary"
    assert len(calls) == 2
    assert not any(f in calls[1] for f in ISOLATION_FLAGS)
    assert any("tools" in r.message and "Update Claude Code" in r.message for r in caplog.records)


def test_any_other_failure_is_not_retried(monkeypatch):
    """A retry without the flags is a downgrade. It must not be reachable by an
    ordinary failure such as a login or rate-limit error."""
    calls = _fake_run(monkeypatch, [(1, "", "Not logged in")])
    with pytest.raises(RuntimeError, match="Not logged in"):
        ClaudeCliSummarizer("sonnet", executable="/bin/claude").summarize("t")
    assert len(calls) == 1


def test_a_failed_retry_surfaces_the_second_error(monkeypatch):
    _fake_run(monkeypatch, [
        (1, "", "error: unknown option '--safe-mode'"),
        (1, "", "Not logged in"),
    ])
    with pytest.raises(RuntimeError, match="Not logged in"):
        ClaudeCliSummarizer("sonnet", executable="/bin/claude").summarize("t")
