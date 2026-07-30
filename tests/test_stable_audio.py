"""is_stable_audio and the cloud-placeholder prefetch that has to precede it.

An online-only file from Google Drive / iCloud / Dropbox reports its final size
while holding no content. Comparing sizes therefore says "stable" on every pass
while the decode probe keeps failing — so the recording is skipped forever, and
the log only ever says "not decodable yet". Reading the bytes is what makes the
provider fetch them, which is why the order below is a contract, not a detail.
"""
from types import SimpleNamespace

from moim_scribe.pipeline import PREFETCH_CHUNK_BYTES, _prefetch, is_stable_audio


def test_prefetch_reads_a_multi_chunk_file_without_raising(tmp_path):
    f = tmp_path / "rec.m4a"
    f.write_bytes(b"x" * (2 * PREFETCH_CHUNK_BYTES + 7))
    _prefetch(f)


def test_prefetch_survives_an_unreadable_path(tmp_path, caplog):
    """A stub that cannot be opened yet must not break the pass."""
    with caplog.at_level("INFO"):
        _prefetch(tmp_path / "not-there.m4a")
    assert "prefetch skipped" in caplog.text


def test_prefetch_logs_when_it_held_the_pipeline(tmp_path, monkeypatch, caplog):
    f = tmp_path / "rec.m4a"
    f.write_bytes(b"x" * 128)
    ticks = iter([0.0, 99.0])
    monkeypatch.setattr("moim_scribe.pipeline.time.time", lambda: next(ticks))
    with caplog.at_level("INFO"):
        _prefetch(f)
    assert "the pipeline was held" in caplog.text


def test_prefetch_is_quiet_when_fast(tmp_path, monkeypatch, caplog):
    f = tmp_path / "rec.m4a"
    f.write_bytes(b"x" * 128)
    ticks = iter([0.0, 0.2])
    monkeypatch.setattr("moim_scribe.pipeline.time.time", lambda: next(ticks))
    with caplog.at_level("INFO"):
        _prefetch(f)
    assert caplog.text == ""


def test_prefetch_runs_before_the_size_comparison(tmp_path, monkeypatch):
    """Order is the whole point: prefetch, then measure, then probe."""
    order = []
    f = tmp_path / "rec.m4a"
    f.write_bytes(b"x" * 20000)
    monkeypatch.setattr("moim_scribe.pipeline._prefetch", lambda p: order.append("prefetch"))
    monkeypatch.setattr("moim_scribe.pipeline.time.sleep", lambda s: order.append("wait"))
    monkeypatch.setattr(
        "moim_scribe.pipeline.subprocess.run",
        lambda *a, **k: order.append("probe") or SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    assert is_stable_audio(f) is True
    assert order == ["prefetch", "wait", "probe"]


def test_undecodable_file_is_skipped_not_failed(tmp_path, monkeypatch, caplog):
    f = tmp_path / "rec.m4a"
    f.write_bytes(b"x" * 20000)
    monkeypatch.setattr("moim_scribe.pipeline._prefetch", lambda p: None)
    monkeypatch.setattr("moim_scribe.pipeline.time.sleep", lambda s: None)
    monkeypatch.setattr(
        "moim_scribe.pipeline.subprocess.run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="moov atom not found"),
    )
    with caplog.at_level("INFO"):
        assert is_stable_audio(f) is False
    assert "not decodable yet" in caplog.text


def test_too_small_file_is_skipped(tmp_path, monkeypatch):
    f = tmp_path / "rec.m4a"
    f.write_bytes(b"x" * 10)
    monkeypatch.setattr("moim_scribe.pipeline._prefetch", lambda p: None)
    monkeypatch.setattr("moim_scribe.pipeline.time.sleep", lambda s: None)
    assert is_stable_audio(f) is False
