"""Summarize by shelling out to the Claude Code CLI (`claude -p`).

Uses whatever login the CLI already has — no key management. The prompt
goes in via stdin; a hard timeout guards against very long recordings
hanging the pipeline.

The CLI is an agent, and here it is used as a plain text-in, text-out model, so
it is started with everything else switched off (ISOLATION_FLAGS):

  - no tools. The prompt is a transcript of whatever was said in the room. An
    agent that can act should never be handed that as its instructions.
  - none of the user's own customizations (global instructions, hooks, MCP
    servers, skills, settings). Otherwise the summary depends on the machine
    it ran on.

Safe mode alone does not cover settings. Measured on a machine whose user
settings ask for Korean replies: with safe mode only, an English lens and an
English transcript came back in Korean 3 times out of 3; with user settings
also excluded (`--setting-sources project`) they came back in English 4 out of
4, and a Korean lens and transcript still came back in Korean. "project" is the
narrowest source the CLI offers, so the call also runs from an empty temporary
directory: there is then no project to take settings from either.

Older CLI versions do not know these flags. That case fails closed: a clear
error tells the user to update Claude Code or pick another summarizer. There
is deliberately no retry without the flags, because that would hand a
transcript to an agent with tools and the user's own settings.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile

logger = logging.getLogger("moim_scribe.summarize.claude_cli")

ISOLATION_FLAGS = ("--safe-mode", "--strict-mcp-config", "--tools=", "--setting-sources", "project")


class ClaudeCliSummarizer:
    def __init__(self, model: str, timeout_sec: int = 900, executable: str | None = None):
        self.model = model
        self.timeout_sec = timeout_sec
        self.executable = executable

    def summarize(self, prompt: str) -> str:
        exe = self.executable or shutil.which("claude")
        if not exe:
            raise RuntimeError(
                "claude CLI not found on PATH. Install Claude Code, or switch "
                "summarizer to anthropic_api / ollama / none in config.yaml"
            )
        base = [exe, "-p", "--model", self.model]
        r = self._run([*base, *ISOLATION_FLAGS], prompt)
        if r.returncode != 0 and "unknown option" in (r.stderr or "").lower():
            raise RuntimeError(
                "this claude CLI does not accept the isolation flags "
                f"({' '.join(ISOLATION_FLAGS)}), and running without them would "
                "give the summary step tools and your own Claude Code settings. "
                "Update Claude Code, or set summarizer to ollama / none in config.yaml"
            )
        if r.returncode != 0:
            raise RuntimeError(f"claude CLI exited {r.returncode}: {r.stderr.strip()[:500]}")
        out = r.stdout.strip()
        if not out:
            raise RuntimeError("claude CLI returned empty output")
        return out

    def _run(self, argv: list[str], prompt: str) -> subprocess.CompletedProcess:
        # An empty directory of its own: wherever the user ran moim-scribe from
        # may hold a .claude/ folder or a CLAUDE.md, and that is "project".
        with tempfile.TemporaryDirectory(prefix="moim-scribe-claude-") as empty:
            return subprocess.run(
                argv,
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=self.timeout_sec,
                check=False,
                cwd=empty,
            )
