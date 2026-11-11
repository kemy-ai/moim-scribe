"""Summarize via a local Ollama server — the fully-offline option.

Requires Ollama running (default http://localhost:11434) with the chosen
model pulled, e.g. `ollama pull llama3.1`. Quality depends heavily on the
local model; for long transcripts prefer models with large context windows.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

DEFAULT_HOST = "http://localhost:11434"


class OllamaSummarizer:
    def __init__(self, model: str, timeout_sec: int = 900, host: str | None = None):
        self.model = model
        self.timeout_sec = timeout_sec
        raw = (host or os.environ.get("OLLAMA_HOST") or DEFAULT_HOST).strip()
        # Ollama's own documented form has no scheme (`0.0.0.0`, `host:11434`).
        if "://" not in raw:
            raw = f"http://{raw}"
        self.host = raw.rstrip("/")

    def summarize(self, prompt: str) -> str:
        body = json.dumps(
            {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{self.host}/api/chat",
            data=body,
            method="POST",
            headers={"content-type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError) as e:
            raise RuntimeError(
                f"Ollama request failed ({self.host}). Is Ollama running and "
                f"'{self.model}' pulled? — {e}"
            ) from e
        out = (data.get("message") or {}).get("content", "").strip()
        if not out:
            raise RuntimeError("Ollama returned empty content")
        return out
