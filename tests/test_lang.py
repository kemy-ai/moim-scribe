"""Spoken speaker-count detection (ko/en) and the language dispatcher."""
from moim_scribe.lang import detect_num_speakers
from moim_scribe.lang.en import extract_count as en_count
from moim_scribe.lang.ko import extract_count as ko_count


def test_ko_arabic():
    assert ko_count("안녕하세요 참석자 5명입니다") == 5
    assert ko_count("참석자는 12명이에요") == 12


def test_ko_native_numerals():
    assert ko_count("오늘 참석자 다섯 명입니다") == 5
    assert ko_count("참석자가 두 명이야") == 2


def test_ko_no_cue():
    assert ko_count("오늘 책 이야기를 해봅시다") is None


def test_en_forms():
    assert en_count("we have 4 participants today") == 4
    assert en_count("five people are here") == 5
    assert en_count("Three speakers today.") == 3


def test_en_no_cue():
    assert en_count("let's get started") is None


def test_dispatcher_uses_head_window():
    segs = [(0.0, 3.0, "참석자 세 명입니다"), (200.0, 202.0, "참석자 9명")]
    assert detect_num_speakers("ko", segs, window_sec=90) == 3


def test_dispatcher_unknown_language():
    segs = [(0.0, 3.0, "cinq participants")]
    assert detect_num_speakers("fr", segs) is None
