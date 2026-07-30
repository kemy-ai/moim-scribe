"""strip_intra_repeat: repetition loops *inside* a single segment.

A separate axis from dedupe_repeated_segments, which only sees consecutive
identical segments. A character-level loop within one segment passes that guard
untouched — and one such segment is enough to skew a speaker's talk share and
everything computed from it.
"""
from moim_scribe.diarize import (
    INTRA_REPEAT_DROP_LEN,
    INTRA_REPEAT_MIN,
    strip_intra_repeat,
    strip_intra_repeats,
)


def test_plain_text_untouched():
    text = "오늘은 세 가지 안건이 있습니다"
    assert strip_intra_repeat(text) == text


def test_long_run_is_dropped_whole():
    """The observed failure shape: one fragment repeated hundreds of times."""
    assert strip_intra_repeat("lin" * 223) == ""


def test_run_at_the_drop_threshold_is_dropped():
    unit = "x"
    assert strip_intra_repeat(unit * INTRA_REPEAT_DROP_LEN) == ""


def test_short_run_collapses_to_one_unit():
    """Laughter and interjections are real speech — collapse, don't delete."""
    assert strip_intra_repeat("ㅋ" * 10) == "ㅋ"


def test_multi_character_unit_collapses():
    assert strip_intra_repeat("ab" * 8) == "ab"


def test_below_min_repeats_is_left_alone():
    text = "네" * (INTRA_REPEAT_MIN - 1)
    assert strip_intra_repeat(text) == text


def test_run_embedded_in_real_speech_keeps_the_speech():
    out = strip_intra_repeat("그래서 " + "lin" * 223 + " 결론은 이렇습니다")
    assert "그래서" in out and "결론은 이렇습니다" in out
    assert "linlin" not in out


def test_segments_emptied_by_cleaning_are_dropped():
    segs = [
        (0.0, 1.0, "정상 문장"),
        (1.0, 12.0, "lin" * 223),
        (12.0, 13.0, "다음 문장"),
    ]
    out, dropped = strip_intra_repeats(segs)
    assert dropped == 1
    assert [s[2] for s in out] == ["정상 문장", "다음 문장"]


def test_segment_timings_are_preserved():
    segs = [(3.5, 9.25, "ㅋ" * 10)]
    out, dropped = strip_intra_repeats(segs)
    assert dropped == 0
    assert out == [(3.5, 9.25, "ㅋ")]


def test_input_not_mutated():
    segs = [(0.0, 1.0, "lin" * 223), (1.0, 2.0, "ok")]
    before = list(segs)
    strip_intra_repeats(segs)
    assert segs == before


def test_empty_input():
    assert strip_intra_repeats([]) == ([], 0)
