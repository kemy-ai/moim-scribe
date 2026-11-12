"""One bad file, or one failed run, must never stop the pipeline or lose a recording.

These come from a hands-on red team. Each scenario used to do one of: kill
every later pass (an over-long filename), leave a full-length decoded copy of
the recording in _processing and then quarantine it as if it were a recording
(the engine's temp wav), or skip a file forever with a misleading INFO line.
The speaker-count hint has its own file, test_filename_hint.py.
"""
import json
import os
import sys
from pathlib import Path

import pytest

from moim_scribe import pipeline
from moim_scribe.config import ensure_workspace_dirs, load_config
from moim_scribe.pipeline import (
    PipelineLock,
    is_stable_audio,
    quarantine_leftovers,
    scan_inbox,
)

# A stem whose name fits on every common filesystem, but whose work products
# ("<stem>_transcript_diarized.jsonl") do not: 251 + 4 = 255.
LONG_STEM = "x" * 251

# On Windows the full path, not the name, hits the limit first (MAX_PATH 260
# without long-path support), so these files cannot even be set up there.
long_names = pytest.mark.skipif(
    sys.platform == "win32", reason="Windows MAX_PATH limits the path before the name"
)


class _Summ:
    def summarize(self, prompt):
        return "# T\n\nbody\n"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    c = load_config(tmp_path)
    ensure_workspace_dirs(c)
    monkeypatch.setattr(pipeline, "is_stable_audio", lambda p: True)
    return c


def _stub_engine(cfg, monkeypatch, fail_on=None, temp_wav=False):
    """Stand in for diarize.run_diarization, writing what the real one writes.

    Like the real engine, it writes its work products next to the audio, so an
    over-long stem raises ENAMETOOLONG here as it would there. With temp_wav it
    first leaves the full-length wav the real engine creates (and deletes only
    on success) before failing.
    """
    def fake_run(audio, **kw):
        folder = Path(audio).parent
        if temp_wav:
            (folder / f"_diarize_full_{os.getpid()}.wav").write_bytes(b"W" * 50_000)
        if fail_on and fail_on in audio:
            raise RuntimeError("engine boom")
        stem = os.path.splitext(os.path.basename(audio))[0]
        base = folder / stem
        txt = Path(f"{base}_transcript_diarized.txt")
        txt.write_text("[  0.0s] SPEAKER_00: hello\n", encoding="utf-8")
        jsonl = Path(f"{base}_transcript_diarized.jsonl")
        jsonl.write_text("{}\n", encoding="utf-8")
        sp = Path(f"{base}_speakers.json")
        sp.write_text(json.dumps({"n_speakers": 1, "speakers": []}), encoding="utf-8")
        return {"txt_path": str(txt), "jsonl_path": str(jsonl), "stats_path": str(sp)}

    monkeypatch.setattr(pipeline, "get_hf_token", lambda *a, **k: "tok")
    monkeypatch.setattr(pipeline, "get_backend", lambda *a, **k: object())
    monkeypatch.setattr(pipeline, "run_diarization", fake_run)
    monkeypatch.setattr(pipeline, "get_summarizer", lambda *a, **k: _Summ())
    monkeypatch.setattr(pipeline, "_recording_time_block", lambda *a, **k: "")


def _drop(folder, name, body=b"A"):
    (folder / name).write_bytes(body * 20_000)


def _names(folder):
    return sorted(p.name for p in folder.iterdir())


# --- A3: an over-long filename ----------------------------------------------

@long_names
def test_over_long_name_goes_to_error_and_every_pass_keeps_running(cfg, monkeypatch, caplog):
    _stub_engine(cfg, monkeypatch)
    _drop(cfg.inbox_dir, f"{LONG_STEM}.m4a")
    _drop(cfg.inbox_dir, "innocent meeting.m4a")

    with caplog.at_level("INFO"):
        assert scan_inbox(cfg) == {"processed": 1, "failed": 1, "skipped": 0}
    assert "filename too long: shorten it and move it back to inbox/" in caplog.text
    assert f"{LONG_STEM}.m4a" in _names(cfg.error_dir)
    assert "innocent meeting.m4a" in _names(cfg.done_dir)
    assert _names(cfg.processing_dir) == []

    _drop(cfg.inbox_dir, "second pass.m4a")
    assert scan_inbox(cfg) == {"processed": 1, "failed": 0, "skipped": 0}
    _drop(cfg.inbox_dir, "third pass.m4a")
    assert scan_inbox(cfg) == {"processed": 1, "failed": 0, "skipped": 0}
    assert {"second pass.m4a", "third pass.m4a"} <= set(_names(cfg.done_dir))
    assert f"{LONG_STEM}.m4a" in _names(cfg.error_dir)  # the original, untouched


@long_names
def test_failure_cleanup_survives_names_the_filesystem_rejects(cfg, monkeypatch):
    """Even past the claim check, a failing cleanup must not end the pass."""
    _stub_engine(cfg, monkeypatch)
    monkeypatch.setattr(pipeline, "name_fits", lambda folder, stem: True)
    _drop(cfg.inbox_dir, f"{LONG_STEM}.m4a")
    _drop(cfg.inbox_dir, "innocent meeting.m4a")
    assert scan_inbox(cfg) == {"processed": 1, "failed": 1, "skipped": 0}
    assert f"{LONG_STEM}.m4a" in _names(cfg.error_dir)
    _drop(cfg.inbox_dir, "next.m4a")
    assert scan_inbox(cfg)["processed"] == 1


@long_names
def test_quarantine_survives_an_over_long_stranded_name(cfg, monkeypatch):
    _drop(cfg.processing_dir, f"{LONG_STEM}.m4a")
    _drop(cfg.processing_dir, "other stranded.m4a")
    quarantine_leftovers(cfg)
    assert _names(cfg.error_dir) == sorted([f"{LONG_STEM}.m4a", "other stranded.m4a"])
    assert _names(cfg.processing_dir) == []


def test_quarantine_keeps_going_when_one_move_fails(cfg, monkeypatch, caplog):
    _drop(cfg.processing_dir, "a stuck.m4a")
    _drop(cfg.processing_dir, "b fine.m4a")
    real_move = pipeline.shutil.move

    def move(src, dst):
        if "a stuck" in str(src):
            raise PermissionError(13, "denied")
        return real_move(src, dst)

    monkeypatch.setattr(pipeline.shutil, "move", move)
    with caplog.at_level("INFO"):
        quarantine_leftovers(cfg)
    assert _names(cfg.error_dir) == ["b fine.m4a"]
    assert "could not move a stuck.m4a" in caplog.text


@long_names
def test_name_fits_is_decided_by_the_filesystem(tmp_path):
    assert pipeline.name_fits(tmp_path, "short name") is True
    assert pipeline.name_fits(tmp_path, LONG_STEM) is False
    # 40 Hangul syllables fit everywhere when composed (120 bytes). Decomposed
    # they are 80 UTF-16 units, which APFS still holds, but 240 bytes, which
    # with the work-file suffix is past the 255 bytes Linux filesystems allow.
    import unicodedata
    title = "가나다라마바사아자차" * 4
    assert pipeline.name_fits(tmp_path, unicodedata.normalize("NFC", title)) is True
    decomposed = unicodedata.normalize("NFD", title)
    if sys.platform == "darwin":
        assert pipeline.name_fits(tmp_path, decomposed) is True
    elif sys.platform == "linux":
        assert pipeline.name_fits(tmp_path, decomposed) is False
    assert list(tmp_path.iterdir()) == []  # the probe leaves nothing behind


def test_a_stranded_file_is_never_overwritten_by_a_same_named_claim(cfg, monkeypatch):
    _stub_engine(cfg, monkeypatch)
    _drop(cfg.processing_dir, "rec.m4a", b"OLD")
    monkeypatch.setattr(pipeline, "quarantine_leftovers", lambda c: None)  # its move failed
    _drop(cfg.inbox_dir, "rec.m4a", b"NEW")
    assert scan_inbox(cfg)["skipped"] == 1
    assert (cfg.processing_dir / "rec.m4a").read_bytes()[:3] == b"OLD"
    assert (cfg.inbox_dir / "rec.m4a").read_bytes()[:3] == b"NEW"


# --- A1 (pipeline half): the engine's full-length temp wav -------------------

def test_failed_run_leaves_no_temp_wav_in_processing(cfg, monkeypatch):
    _stub_engine(cfg, monkeypatch, fail_on="bad", temp_wav=True)
    _drop(cfg.inbox_dir, "bad.m4a")
    assert scan_inbox(cfg)["failed"] == 1
    assert _names(cfg.processing_dir) == []
    assert _names(cfg.error_dir) == ["bad.m4a"]


def test_quarantine_deletes_a_dead_runs_temp_wav_and_never_files_it(cfg):
    dead = cfg.processing_dir / "_diarize_full_999999999.wav"
    dead.write_bytes(b"W" * 50_000)
    own = cfg.processing_dir / f"_diarize_full_{os.getpid()}.wav"
    own.write_bytes(b"W" * 50_000)
    head = cfg.processing_dir / "_diarize_head_999999999.wav"
    head.write_bytes(b"W" * 50_000)
    _drop(cfg.processing_dir, "real stranded.m4a")
    quarantine_leftovers(cfg)
    assert _names(cfg.error_dir) == ["real stranded.m4a"]
    assert _names(cfg.processing_dir) == []


def test_quarantine_leaves_a_live_runs_temp_wav_alone(cfg, monkeypatch):
    live = cfg.processing_dir / "_diarize_full_4242.wav"
    live.write_bytes(b"W" * 50_000)
    monkeypatch.setattr(PipelineLock, "_pid_alive", staticmethod(lambda pid: True))
    quarantine_leftovers(cfg)
    assert live.exists()
    assert _names(cfg.error_dir) == []


def test_a_recording_named_like_a_temp_file_is_not_claimed(cfg, monkeypatch, caplog):
    _stub_engine(cfg, monkeypatch)
    _drop(cfg.inbox_dir, "_diarize_full_123.wav")
    with caplog.at_level("WARNING"):
        assert scan_inbox(cfg) == {"processed": 0, "failed": 0, "skipped": 1}
    assert (cfg.inbox_dir / "_diarize_full_123.wav").exists()
    assert "reserved" in caplog.text


# --- A4: files that can never decode -----------------------------------------

def _stable_env(monkeypatch, returncode):
    from types import SimpleNamespace
    monkeypatch.setattr(pipeline, "_prefetch", lambda p: None)
    monkeypatch.setattr(pipeline.time, "sleep", lambda s: None)
    monkeypatch.setattr(
        pipeline.subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=returncode, stdout="", stderr=""),
    )


def test_undecodable_file_warns_with_both_readings(tmp_path, monkeypatch, caplog):
    f = tmp_path / "not audio.m4a"
    f.write_bytes(b"x" * 20000)
    _stable_env(monkeypatch, 1)
    with caplog.at_level("INFO"):
        assert is_stable_audio(f) is False
    (rec,) = [r for r in caplog.records if "not audio.m4a" in r.getMessage()]
    assert rec.levelname == "WARNING"
    msg = rec.getMessage()
    assert "still downloading, or not a valid audio file" in msg
    assert "remove it from inbox/" in msg


def test_too_small_file_warns_with_its_size(tmp_path, monkeypatch, caplog):
    f = tmp_path / "note.m4a"
    f.write_bytes(b"x" * 21)
    _stable_env(monkeypatch, 0)
    with caplog.at_level("INFO"):
        assert is_stable_audio(f) is False
    (rec,) = [r for r in caplog.records if "note.m4a" in r.getMessage()]
    assert rec.levelname == "WARNING"
    assert "21 bytes" in rec.getMessage()
    assert "too small to be a recording" in rec.getMessage()


def test_a_growing_file_is_skipped_quietly(tmp_path, monkeypatch, caplog):
    f = tmp_path / "copying.m4a"
    f.write_bytes(b"x" * 20000)
    monkeypatch.setattr(pipeline, "_prefetch", lambda p: None)
    monkeypatch.setattr(pipeline.time, "sleep", lambda s: f.write_bytes(b"x" * 30000))
    with caplog.at_level("INFO"):
        assert is_stable_audio(f) is False
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


# --- A6: the lock --------------------------------------------------------------

def test_lock_holding_our_own_pid_is_stale(tmp_path):
    lock_dir = tmp_path / ".moim-scribe.lock"
    lock_dir.mkdir()
    (lock_dir / "pid").write_text(str(os.getpid()))
    assert PipelineLock(tmp_path).acquire() is True


def test_lock_held_by_another_live_process_is_respected(tmp_path):
    lock_dir = tmp_path / ".moim-scribe.lock"
    lock_dir.mkdir()
    (lock_dir / "pid").write_text(str(os.getppid()))
    lock = PipelineLock(tmp_path)
    assert lock.acquire() is False
    assert lock.error is None


def test_unwritable_workspace_is_reported_not_called_a_busy_lock(cfg, monkeypatch, caplog):
    def no_mkdir(self, *a, **k):
        raise PermissionError(13, "Read-only file system")

    monkeypatch.setattr(Path, "mkdir", no_mkdir)
    _drop(cfg.inbox_dir, "rec.m4a")
    with caplog.at_level("INFO"):
        result = scan_inbox(cfg)
    # A5: the pass must read as failed (scan-inbox exits non-zero), not as an
    # empty inbox. The three counts are still there and still zero.
    assert {k: result[k] for k in ("processed", "failed", "skipped")} == {
        "processed": 0, "failed": 0, "skipped": 0}
    assert len(result["setup_problems"]) == 1
    assert "cannot create the lock" in result["setup_problems"][0]
    assert "another run is active" not in caplog.text
    assert "cannot create the lock" in caplog.text
    assert "Read-only file system" in caplog.text
    assert (cfg.inbox_dir / "rec.m4a").exists()


def test_quarantine_message_says_how_to_retry(cfg, caplog):
    _drop(cfg.processing_dir, "stranded.m4a")
    with caplog.at_level("WARNING"):
        quarantine_leftovers(cfg)
    assert "move it back to inbox/" in caplog.text
