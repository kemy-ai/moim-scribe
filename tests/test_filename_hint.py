"""The speaker count read from a filename is forced on the speaker separation.

So a false hit is worse than none. The red team found "1103 인터뷰" read as
1103 speakers (title "터뷰"), "2025 명절 계획" as 2025, "3인칭 소설 합평회" as 3,
"budget for 2000 people" as 2000.
"""
from pathlib import Path

import pytest

from moim_scribe import pipeline
from moim_scribe.config import ensure_workspace_dirs, load_config
from moim_scribe.pipeline import filename_speaker_hint, scan_inbox


@pytest.mark.parametrize("title,language,count,cleaned", [
    # over-matches the red team found: no count, title untouched
    ("1인 가구 인터뷰", "ko", None, "1인 가구 인터뷰"),
    ("3인칭 소설 합평회", "ko", None, "3인칭 소설 합평회"),
    ("2025 명절 계획", "ko", None, "2025 명절 계획"),
    ("1103 인터뷰", "ko", None, "1103 인터뷰"),
    ("2026 인사평가", "ko", None, "2026 인사평가"),
    ("budget for 2000 people", "en", None, "budget for 2000 people"),
    ("top 40 people skills", "en", None, "top 40 people skills"),
    # the tail of a longer number is not a count, nor is a padded one
    ("참가 1225명 행사", "ko", None, "참가 1225명 행사"),
    ("회의 003명", "ko", None, "회의 003명"),
    # 명 opening a word is not the counter
    ("10 명언 모음", "ko", None, "10 명언 모음"),
    # what must keep working
    ("book club 5명", "ko", 5, "book club"),
    ("독서모임 5명", "ko", 5, "독서모임"),
    ("standup 4p", "en", 4, "standup"),
    ("회의 3인", "ko", 3, "회의"),
    ("5명", "ko", 5, "untitled"),
    ("전략회의 3인 final", "ko", 3, "전략회의 final"),
    ("5명 회의", "ko", 5, "회의"),
    ("book club 5 people", "en", 5, "book club"),
    # range edges
    ("all hands 30p", "en", 30, "all hands"),
    ("all hands 31p", "en", None, "all hands 31p"),
    ("zero 0명", "ko", None, "zero 0명"),
])
def test_filename_speaker_hint_table(title, language, count, cleaned):
    assert filename_speaker_hint(title, language) == (count, cleaned)


def test_hint_red_team_titles_reach_pyannote_as_no_count(tmp_path, monkeypatch):
    """End to end: what the engine receives, not just what the parser returns."""
    cfg = load_config(tmp_path)
    ensure_workspace_dirs(cfg)
    cfg.language = "ko"
    monkeypatch.setattr(pipeline, "is_stable_audio", lambda p: True)
    seen = {}

    def fake_run(audio, **kw):
        seen[Path(audio).name] = kw.get("num_speakers")
        raise RuntimeError("stop after capturing the speaker count")

    monkeypatch.setattr(pipeline, "get_hf_token", lambda *a, **k: "tok")
    monkeypatch.setattr(pipeline, "get_backend", lambda *a, **k: object())
    monkeypatch.setattr(pipeline, "run_diarization", fake_run)
    for name in ("1인 가구 인터뷰.m4a", "3인칭 소설 합평회.m4a", "2025 명절 계획.m4a", "독서모임 5명.m4a"):
        (cfg.inbox_dir / name).write_bytes(b"A" * 20_000)
    scan_inbox(cfg)
    assert seen == {
        "1인 가구 인터뷰.m4a": None,
        "3인칭 소설 합평회.m4a": None,
        "2025 명절 계획.m4a": None,
        "독서모임 5명.m4a": 5,
    }
