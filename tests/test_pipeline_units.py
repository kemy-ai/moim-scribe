"""Pure-logic pieces of the pipeline: filename hints, lock, prompt headers."""
import os
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


def test_strip_preamble_recovers_a_heading_fused_with_noise():
    """Observed shape: wrapper output lands on the heading's own line.

    Skipping that line would drop the H1 and leave the deliverable starting at
    an H2 — worse than the noise it was trying to remove.
    """
    fused = "Kim Wo# Minutes — topic\n\n## Overview\nbody"
    assert _strip_preamble(fused) == "# Minutes — topic\n\n## Overview\nbody"


def test_strip_preamble_leaves_prose_hash_alone():
    """"#" without a following space is not a heading marker — e.g. "see #1"."""
    prose = "see #1 for context\nmore text\n# Real title\nbody"
    assert _strip_preamble(prose) == "# Real title\nbody"


def test_strip_preamble_only_repairs_the_first_line():
    later = "banner line\nissue #2 mentioned here\n# Real title\nbody"
    assert _strip_preamble(later) == "# Real title\nbody"


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
        assert {
            "rec_title", "rec_info", "rec_line", "spk_stats", "spk_fixed", "transcript",
        } <= set(PROMPT_HEADERS[lang])


def test_rec_title_header_carries_the_title_but_warns_against_copying_it():
    """The filename outranks transcription for people's names — it is not the H1.

    Without the second half of that sentence in the prompt, the deliverable
    tends to adopt the filename as its own heading.
    """
    for lang in ("ko", "en"):
        block = PROMPT_HEADERS[lang]["rec_title"].format(title="wine chat - Jeong Yujin")
        assert "wine chat - Jeong Yujin" in block
        assert "{title}" not in block
        assert len(block.splitlines()) >= 3  # header, the value, the do-not-copy note


def test_lock_basic_cycle(tmp_path):
    lock = PipelineLock(tmp_path)
    assert lock.acquire() is True
    # held by another live process (our parent stands in for it): acquire fails.
    # Our own pid would not do: a lock naming it is stale (see test_pipeline_robustness).
    (tmp_path / ".moim-scribe.lock" / "pid").write_text(str(os.getppid()))
    assert PipelineLock(tmp_path).acquire() is False
    lock.release()
    assert PipelineLock(tmp_path).acquire() is True


def test_lock_stale_pid_reclaimed(tmp_path):
    lock_dir = tmp_path / ".moim-scribe.lock"
    lock_dir.mkdir()
    (lock_dir / "pid").write_text("999999999")  # far above any real pid space
    assert PipelineLock(tmp_path).acquire() is True
