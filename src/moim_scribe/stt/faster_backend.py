"""Cross-platform STT backend using faster-whisper (CTranslate2).

Works on Windows and Linux (CUDA or CPU). Experimental:
the pipeline was originally tuned against the mlx backend on Apple
Silicon; decoding parameters are mapped 1:1 but field reports welcome.

Install with: pip install "moim-scribe[cross]"
"""
from __future__ import annotations

from .base import (
    COMPRESSION_RATIO_THRESHOLD,
    CONDITION_ON_PREVIOUS_TEXT,
    LOGPROB_THRESHOLD,
    NO_SPEECH_THRESHOLD,
    Segment,
)


class FasterWhisperBackend:
    def __init__(self, model: str, device: str = "auto", compute_type: str = "default"):
        self.model_name = model
        self.device = device
        self.compute_type = compute_type
        self._model = None

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            self._model = WhisperModel(
                self.model_name, device=self.device, compute_type=self.compute_type
            )
        return self._model

    def transcribe(
        self, audio_path: str, language: str, initial_prompt: str | None = None
    ) -> list[Segment]:
        model = self._load()
        segments, _info = model.transcribe(
            audio_path,
            language=language,
            condition_on_previous_text=CONDITION_ON_PREVIOUS_TEXT,
            no_speech_threshold=NO_SPEECH_THRESHOLD,
            log_prob_threshold=LOGPROB_THRESHOLD,
            compression_ratio_threshold=COMPRESSION_RATIO_THRESHOLD,
            initial_prompt=initial_prompt,
        )
        return [(s.start, s.end, s.text.strip()) for s in segments if s.text.strip()]
