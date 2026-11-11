"""Summarize by shelling out to the Claude Code CLI (`claude -p`).

Uses whatever login the CLI already has — no key management. The prompt
goes in via stdin; a hard timeout guards against very long recordings
hanging the pipeline.

The CLI is an agent, and here it is used as a plain text-in, text-out model, so
it is started with everything else switched off (ISOLATION_FLAGS):

  - no tools. The prompt is a transcript of whatever was said in the room. An
    agent that can act should never be handed that as its instructions.
  - none of the user's own customizations (global instructions, hooks, MCP
    servers, skills). Otherwise the summary depends on the machine it ran on:
    in an end-to-end run, an English workspace came back summarized in Korean
    because the machine's global instructions asked for Korean.

Older CLI versions do not know these flags. Rather than fail every summary on
them, one refusal that names an unknown option is retried without the flags,
with a warning saying what was lost.
"""
from __future__ import annotations

import logging
import shutil
import subprocess

logger = logging.getLogger("moim_scribe.summarize.claude_cli")

ISOLATION_FLAGS = ("--safe-mode", "--strict-mcp-config", "--tools=")


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
            logger.warning(
                "this claude CLI does not accept the isolation flags "
                f"({' '.join(ISOLATION_FLAGS)}); retrying without them. The summary "
                "now runs with your own Claude Code settings and with tools "
                "available. Update Claude Code to close this."
            )
            r = self._run(base, prompt)
        if r.returncode != 0:
            raise RuntimeError(f"claude CLI exited {r.returncode}: {r.stderr.strip()[:500]}")
        out = r.stdout.strip()
        if not out:
            raise RuntimeError("claude CLI returned empty output")
        return out

    def _run(self, argv: list[str], prompt: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            argv,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=self.timeout_sec,
            check=False,
        )
