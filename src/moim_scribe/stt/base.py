"""STT backend interface and shared anti-hallucination decoding parameters.

Two backends ship with moim-scribe:
  - "mlx"    mlx-whisper, Apple Silicon only (fastest on M-series; the
             configuration this project was originally tuned on)
  - "faster" faster-whisper (CTranslate2), cross-platform: Windows, Linux,
             Intel Mac. Marked experimental until field-verified.

Both receive the same decoding parameters, chosen to suppress whisper's
repetition-loop pathology on long real-world recordings:
  - condition_on_previous_text=False is the main lever. When True, whisper
    feeds its own previous window back as a prompt, which is exactly the
    self-reinforcing path that turns one bad decode into dozens of
    identical segments. The upstream docstring itself notes disabling it
    makes the model "less prone to getting stuck in a failure loop".
  - The remaining thresholds are whisper defaults, pinned as explicit
    constants so future tuning has a single place to happen.
"""
from __future__ import annotations

from typing import Protocol

# (start_sec, end_sec, text)
Segment = tuple[float, float, str]

CONDITION_ON_PREVIOUS_TEXT = False
NO_SPEECH_THRESHOLD = 0.6
LOGPROB_THRESHOLD = -1.0
COMPRESSION_RATIO_THRESHOLD = 2.4

DEFAULT_MODELS = {
    "mlx": "mlx-community/whisper-large-v3-turbo",
    "faster": "large-v3-turbo",
}


class STTBackend(Protocol):
    def transcribe(
        self, audio_path: str, language: str, initial_prompt: str | None = None
    ) -> list[Segment]:
        """Transcribe a full audio file into (start, end, text) segments."""
        ...


def get_backend(name: str, model: str | None = None) -> STTBackend:
    """Instantiate an STT backend by name ("mlx" or "faster")."""
    if name == "mlx":
        from .mlx_backend import MlxWhisperBackend

        return MlxWhisperBackend(model or DEFAULT_MODELS["mlx"])
    if name == "faster":
        from .faster_backend import FasterWhisperBackend

        return FasterWhisperBackend(model or DEFAULT_MODELS["faster"])
    raise ValueError(f"unknown STT backend: {name!r} (expected 'mlx' or 'faster')")
