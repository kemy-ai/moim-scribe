"""Apple Silicon STT backend using mlx-whisper.

Install with: pip install "moim-scribe[mlx]"
"""
from __future__ import annotations

from .base import (
    COMPRESSION_RATIO_THRESHOLD,
    CONDITION_ON_PREVIOUS_TEXT,
    LOGPROB_THRESHOLD,
    NO_SPEECH_THRESHOLD,
    Segment,
)


class MlxWhisperBackend:
    def __init__(self, model: str):
        self.model = model

    def transcribe(
        self, audio_path: str, language: str, initial_prompt: str | None = None
    ) -> list[Segment]:
        import mlx_whisper

        r = mlx_whisper.transcribe(
            audio_path,
            path_or_hf_repo=self.model,
            language=language,
            condition_on_previous_text=CONDITION_ON_PREVIOUS_TEXT,
            no_speech_threshold=NO_SPEECH_THRESHOLD,
            logprob_threshold=LOGPROB_THRESHOLD,
            compression_ratio_threshold=COMPRESSION_RATIO_THRESHOLD,
            initial_prompt=initial_prompt,
        )
        return [
            (s["start"], s["end"], s["text"].strip())
            for s in r.get("segments", [])
            if s["text"].strip()
        ]
