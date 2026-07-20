"""Korean spoken speaker-count detection ("참석자 N명 / 다섯 명")."""
from __future__ import annotations

import re

_KO_NUM = {
    "한": 1, "하나": 1, "두": 2, "둘": 2, "세": 3, "셋": 3, "네": 4, "넷": 4,
    "다섯": 5, "여섯": 6, "일곱": 7, "여덟": 8, "아홉": 9, "열": 10,
}

_ARABIC_RE = re.compile(r"참석자?[은는이가\s]*(\d+)\s*명")
_NATIVE_RE = re.compile(r"참석자?[은는이가\s]*(한|하나|두|둘|세|셋|네|넷|다섯|여섯|일곱|여덟|아홉|열)\s*명")


def extract_count(head_text: str) -> int | None:
    m = _ARABIC_RE.search(head_text)
    if m:
        return int(m.group(1))
    m = _NATIVE_RE.search(head_text)
    if m:
        return _KO_NUM[m.group(1)]
    return None
