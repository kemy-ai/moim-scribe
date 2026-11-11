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

WHISPER_SAMPLE_RATE = 16000


def load_waveform(audio_path: str):
    """The recording as a 16 kHz mono float32 array, or the path itself.

    faster-whisper decodes a file path with PyAV, and that pairing breaks on
    its own: faster-whisper 1.2.1 passes PyAV an argument that PyAV 19 no
    longer accepts, so a fresh install failed on the first recording with
    "open() got an unexpected keyword argument 'metadata_errors'" (found on a
    clean Windows install, reproduced on macOS). The pipeline has already
    turned the recording into a 16 kHz mono wav by the time it gets here, so
    it is read with soundfile and handed over as samples: PyAV is never
    asked to decode anything.

    Anything that is not such a wav (another sample rate, a format soundfile
    cannot read) is passed through as a path, which is the old behaviour.
    """
    try:
        import numpy as np
        import soundfile as sf

        x, sr = sf.read(audio_path, dtype="float32", always_2d=True)   # (time, channels)
    except Exception:  # noqa: BLE001 - any read problem means "let faster-whisper try"
        return audio_path
    if sr != WHISPER_SAMPLE_RATE or x.size == 0:
        return audio_path
    mono = x[:, 0] if x.shape[1] == 1 else x.mean(axis=1)
    return np.ascontiguousarray(mono, dtype="float32")


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
            load_waveform(audio_path),
            language=language,
            condition_on_previous_text=CONDITION_ON_PREVIOUS_TEXT,
            no_speech_threshold=NO_SPEECH_THRESHOLD,
            log_prob_threshold=LOGPROB_THRESHOLD,
            compression_ratio_threshold=COMPRESSION_RATIO_THRESHOLD,
            initial_prompt=initial_prompt,
        )
        return [(s.start, s.end, s.text.strip()) for s in segments if s.text.strip()]
