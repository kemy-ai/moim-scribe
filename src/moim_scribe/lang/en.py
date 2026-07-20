"""English spoken speaker-count detection ("five participants" / "5 people")."""
from __future__ import annotations

import re

_EN_NUM = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}

_ARABIC_RE = re.compile(r"\b(\d+)\s+(?:participants?|people|speakers?|attendees?)\b", re.IGNORECASE)
_WORD_RE = re.compile(
    r"\b(one|two|three|four|five|six|seven|eight|nine|ten)\s+"
    r"(?:participants?|people|speakers?|attendees?)\b",
    re.IGNORECASE,
)


def extract_count(head_text: str) -> int | None:
    m = _ARABIC_RE.search(head_text)
    if m:
        return int(m.group(1))
    m = _WORD_RE.search(head_text)
    if m:
        return _EN_NUM[m.group(1).lower()]
    return None
