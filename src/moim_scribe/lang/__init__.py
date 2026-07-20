"""Language-specific helpers (spoken speaker-count detection)."""
from __future__ import annotations

from types import ModuleType

from . import en, ko

_MODULES: dict[str, ModuleType] = {"ko": ko, "en": en}


def detect_num_speakers(language: str, segments, window_sec: float = 90.0) -> int | None:
    """Detect a spoken attendee-count cue near the start of the recording.

    Saying the count out loud at the start ("참석자 다섯 명입니다" /
    "five participants today") pins the speaker count without guessing.
    Unknown languages simply return None (auto-estimation takes over).
    """
    mod = _MODULES.get(language)
    if mod is None:
        return None
    head = " ".join(t for s, e, t in segments if s <= window_sec)
    return mod.extract_count(head)
