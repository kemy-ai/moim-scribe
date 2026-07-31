"""Summarize via the Anthropic Messages API (no SDK dependency — stdlib urllib).

Requires the ANTHROPIC_API_KEY environment variable.
"""
from __future__ import annotations

import contextlib
import json
import os
import time
import urllib.error
import urllib.request

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
MAX_TOKENS = 8192
MAX_RETRIES = 2


class AnthropicApiSummarizer:
    def __init__(self, model: str, timeout_sec: int = 900):
        self.model = model
        self.timeout_sec = timeout_sec

    def summarize(self, prompt: str) -> str:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY not set. Export it, or switch summarizer to "
                "claude_cli / ollama / none in config.yaml"
            )
        body = json.dumps(
            {
                "model": self.model,
                "max_tokens": MAX_TOKENS,
                "messages": [{"role": "user", "content": prompt}],
            }
        ).encode("utf-8")

        last: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            req = urllib.request.Request(
                API_URL,
                data=body,
                method="POST",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": API_VERSION,
                    "content-type": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
                out = "".join(parts).strip()
                if not out:
                    raise RuntimeError("Anthropic API returned empty content")
                return out
            except urllib.error.HTTPError as e:
                retryable = e.code == 429 or 500 <= e.code < 600
                if not retryable or attempt >= MAX_RETRIES:
                    detail = ""
                    with contextlib.suppress(Exception):
                        detail = e.read().decode("utf-8")[:300]
                    raise RuntimeError(f"Anthropic API HTTP {e.code}: {detail}") from e
                time.sleep(min(2**attempt * 2, 60))
                last = e
            except (urllib.error.URLError, OSError) as e:
                if attempt >= MAX_RETRIES:
                    raise RuntimeError(f"Anthropic API request failed: {e}") from e
                time.sleep(min(2**attempt * 2, 60))
                last = e
        raise RuntimeError(f"Anthropic API retries exhausted: {last}")
