"""Pure-logic pieces of the pipeline: filename hints, lock, prompt headers."""
import unicodedata

from moim_scribe.pipeline import (
    PROMPT_HEADERS,
    PipelineLock,
    _strip_preamble,
    filename_speaker_hint,
)


def test_strip_preamble_removes_banner():
    noisy = "some wrapper banner\nanother line\n# Minutes — topic\nbody"
    assert _strip_preamble(noisy) == "# Minutes — topic\nbody"


def test_strip_preamble_clean_output_untouched():
    clean = "# Minutes\nbody"
    assert _strip_preamble(clean) == clean


def test_strip_preamble_no_heading_keeps_everything():
    prose = "no headings at all\njust text"
    assert _strip_preamble(prose) == prose


def test_ko_filename_hint_and_strip():
    n, title = filename_speaker_hint("북클럽 5명", "ko")
    assert n == 5 and title == "북클럽"
    n, title = filename_speaker_hint("전략회의 3인 final", "ko")
    assert n == 3 and title == "전략회의 final"


def test_ko_filename_hint_nfd():
    # phone filesystems deliver decomposed Hangul; the hint must still parse
    nfd = unicodedata.normalize("NFD", "모임 4명")
    n, title = filename_speaker_hint(nfd, "ko")
    assert n == 4 and title == "모임"


def test_en_filename_hint():
    n, title = filename_speaker_hint("standup 4p", "en")
    assert n == 4 and title == "standup"
    n, title = filename_speaker_hint("book club 5 people", "en")
    assert n == 5 and title == "book club"


def test_no_hint():
    n, title = filename_speaker_hint("just a recording", "ko")
    assert n is None and title == "just a recording"


def test_hint_only_filename_gets_placeholder_title():
    n, title = filename_speaker_hint("5명", "ko")
    assert n == 5 and title == "untitled"


def test_prompt_headers_have_both_languages():
    for lang in ("ko", "en"):
        assert {"rec_info", "rec_line", "spk_stats", "spk_fixed", "transcript"} <= set(
            PROMPT_HEADERS[lang]
        )


def test_lock_basic_cycle(tmp_path):
    lock = PipelineLock(tmp_path)
    assert lock.acquire() is True
    # second acquire from the same live pid fails (we are alive)
    assert PipelineLock(tmp_path).acquire() is False
    lock.release()
    assert PipelineLock(tmp_path).acquire() is True


def test_lock_stale_pid_reclaimed(tmp_path):
    lock_dir = tmp_path / ".moim-scribe.lock"
    lock_dir.mkdir()
    (lock_dir / "pid").write_text("999999999")  # far above any real pid space
    assert PipelineLock(tmp_path).acquire() is True
