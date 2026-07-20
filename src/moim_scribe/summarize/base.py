"""Summarizer backend interface.

Four ways to turn a diarized transcript into a structured summary:
  claude_cli     shell out to the Claude Code CLI (`claude -p`) — no API
                 key handling, uses your existing login
  anthropic_api  direct Anthropic Messages API call (ANTHROPIC_API_KEY)
  ollama         local model via the Ollama HTTP API — fully offline
  none           skip summarization; the pipeline stops at the transcript
"""
from __future__ import annotations

from typing import Protocol


class Summarizer(Protocol):
    def summarize(self, prompt: str) -> str:
        """Run the full prompt and return the summary markdown."""
        ...


DEFAULT_MODELS = {
    "claude_cli": "sonnet",
    "anthropic_api": "claude-sonnet-5",
    "ollama": "llama3.1",
}


def get_summarizer(name: str, model: str | None = None, timeout_sec: int = 900) -> Summarizer | None:
    """Instantiate a summarizer by name; None for the transcribe-only mode."""
    if name == "none":
        return None
    if name == "claude_cli":
        from .claude_cli import ClaudeCliSummarizer

        return ClaudeCliSummarizer(model or DEFAULT_MODELS[name], timeout_sec)
    if name == "anthropic_api":
        from .anthropic_api import AnthropicApiSummarizer

        return AnthropicApiSummarizer(model or DEFAULT_MODELS[name], timeout_sec)
    if name == "ollama":
        from .ollama import OllamaSummarizer

        return OllamaSummarizer(model or DEFAULT_MODELS[name], timeout_sec)
    raise ValueError(
        f"unknown summarizer: {name!r} (expected claude_cli, anthropic_api, ollama, or none)"
    )
