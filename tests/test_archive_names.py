"""_done and _error keep originals: a repeated filename must not replace one.

A recurring group saves every session under the same name ("book club 5p.m4a"
each week, which the README itself suggests). A plain move onto that name in
_done replaced last week's original and its transcript, with nothing logged.
"""
import pytest

from moim_scribe import pipeline
from moim_scribe.config import ensure_workspace_dirs, load_config
from moim_scribe.pipeline import archive_stem, quarantine_leftovers, scan_inbox


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    c = load_config(tmp_path)
    ensure_workspace_dirs(c)
    monkeypatch.setattr(pipeline, "is_stable_audio", lambda p: True)
    return c


def _drop(cfg, name, body):
    (cfg.inbox_dir / name).write_bytes(body * 5000)


def _ok(cfg_, claimed):
    stem = claimed.stem
    (cfg_.processing_dir / f"{stem}_transcript_diarized.txt").write_text(
        claimed.read_bytes()[:5].decode(), encoding="utf-8")
    (cfg_.processing_dir / f"{stem}_speakers.json").write_text("{}", encoding="utf-8")
    return claimed


def test_same_name_next_week_keeps_both_originals_and_their_transcripts(cfg, monkeypatch):
    monkeypatch.setattr(pipeline, "process_recording", _ok)
    _drop(cfg, "book club 5p.m4a", b"WEEK1")
    scan_inbox(cfg)
    _drop(cfg, "book club 5p.m4a", b"WEEK2")
    scan_inbox(cfg)
    d = cfg.done_dir
    assert (d / "book club 5p.m4a").read_bytes()[:5] == b"WEEK1"
    assert (d / "book club 5p (2).m4a").read_bytes()[:5] == b"WEEK2"
    # the sidecars follow their recording's new stem, so they still pair up
    assert (d / "book club 5p_transcript_diarized.txt").read_text() == "WEEK1"
    assert (d / "book club 5p (2)_transcript_diarized.txt").read_text() == "WEEK2"
    assert (d / "book club 5p (2)_speakers.json").exists()


def test_same_name_in_two_subfolders_in_one_pass(cfg, monkeypatch):
    monkeypatch.setattr(pipeline, "process_recording", _ok)
    for sub, body in (("a", b"AAAAA"), ("b", b"BBBBB")):
        (cfg.inbox_dir / sub).mkdir()
        (cfg.inbox_dir / sub / "standup.m4a").write_bytes(body * 5000)
    assert scan_inbox(cfg)["processed"] == 2
    got = {p.read_bytes()[:5] for p in cfg.done_dir.glob("standup*.m4a")}
    assert got == {b"AAAAA", b"BBBBB"}


def test_failures_with_the_same_name_are_both_kept_in_error(cfg, monkeypatch):
    def boom(cfg_, claimed):
        (cfg_.processing_dir / f"{claimed.stem}_transcript_diarized.txt").write_text("t")
        raise RuntimeError("x")

    monkeypatch.setattr(pipeline, "process_recording", boom)
    _drop(cfg, "rec.m4a", b"FIRST")
    scan_inbox(cfg)
    _drop(cfg, "rec.m4a", b"AGAIN")
    scan_inbox(cfg)
    assert {p.read_bytes()[:5] for p in cfg.error_dir.glob("rec*.m4a")} == {b"FIRST", b"AGAIN"}
    assert (cfg.error_dir / "rec (2)_transcript_diarized.txt").exists()


def test_stranded_file_does_not_replace_one_already_in_error(cfg):
    (cfg.error_dir / "rec.m4a").write_bytes(b"OLD__" * 5000)
    (cfg.processing_dir / "rec.m4a").write_bytes(b"NEW__" * 5000)
    quarantine_leftovers(cfg)
    assert (cfg.error_dir / "rec.m4a").read_bytes()[:5] == b"OLD__"
    assert (cfg.error_dir / "rec (2).m4a").read_bytes()[:5] == b"NEW__"


def test_a_sidecar_alone_also_reserves_the_name(tmp_path):
    (tmp_path / "rec_transcript_diarized.txt").write_text("t")
    assert archive_stem(tmp_path, "rec", ".m4a") == "rec (2)"
    assert archive_stem(tmp_path, "other", ".m4a") == "other"
