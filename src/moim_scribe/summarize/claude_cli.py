"""Summarize by shelling out to the Claude Code CLI (`claude -p`).

Uses whatever login the CLI already has — no key management. The prompt
goes in via stdin; a hard timeout guards against very long recordings
hanging the pipeline.
"""
from __future__ import annotations

import shutil
import subprocess


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
        r = subprocess.run(
            [exe, "-p", "--model", self.model],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=self.timeout_sec,
        )
        if r.returncode != 0:
            raise RuntimeError(f"claude CLI exited {r.returncode}: {r.stderr.strip()[:500]}")
        out = r.stdout.strip()
        if not out:
            raise RuntimeError("claude CLI returned empty output")
        return out
