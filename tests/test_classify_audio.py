"""classify_audio: what counts as a recording worth processing.

The failure this replaces is not a wrong skip, it is silence. Filtering the
folder listing by extension means a file whose extension was stripped never
becomes a candidate — no SKIP line, no error, nothing to notice. So the scan
lists everything and this gate decides.

ffprobe is stubbed throughout: it is a system binary, not a Python dependency,
and CI installs neither.
"""
from types import SimpleNamespace

from moim_scribe.config import ensure_workspace_dirs, load_config
from moim_scribe.pipeline import classify_audio, scan_inbox


def _probe(monkeypatch, *, returncode=0, stdout="audio\n", calls=None):
    def fake_run(*args, **kwargs):
        if calls is not None:
            calls.append(args)
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    monkeypatch.setattr("moim_scribe.pipeline.subprocess.run", fake_run)


def test_known_extension_passes_without_probing(tmp_path, monkeypatch):
    """The normal path must stay free — no per-file subprocess on every scan."""
    calls = []
    _probe(monkeypatch, calls=calls)
    f = tmp_path / "rec.m4a"
    f.write_bytes(b"x" * 100)
    assert classify_audio(f) is True
    assert calls == []


def test_known_extension_is_case_insensitive(tmp_path, monkeypatch):
    calls = []
    _probe(monkeypatch, calls=calls)
    f = tmp_path / "REC.M4A"
    f.write_bytes(b"x" * 100)
    assert classify_audio(f) is True
    assert calls == []


def test_extensionless_audio_is_rescued(tmp_path, monkeypatch, caplog):
    f = tmp_path / "phone call"
    f.write_bytes(b"x" * 100)
    _probe(monkeypatch, returncode=0, stdout="audio\n")
    with caplog.at_level("INFO"):
        assert classify_audio(f) is True
    assert "no usable extension" in caplog.text


def test_non_audio_drops_quietly(tmp_path, monkeypatch, caplog):
    """Our own outputs and stray notes must not become candidates."""
    f = tmp_path / "notes.txt"
    f.write_text("some text", encoding="utf-8")
    _probe(monkeypatch, returncode=1, stdout="")
    with caplog.at_level("INFO"):
        assert classify_audio(f) is False
    assert caplog.text == ""


def test_probe_answering_something_else_is_rejected(tmp_path, monkeypatch):
    f = tmp_path / "clip"
    f.write_bytes(b"x" * 100)
    _probe(monkeypatch, returncode=0, stdout="video\n")
    assert classify_audio(f) is False


def test_empty_file_is_out_before_probing(tmp_path, monkeypatch):
    calls = []
    _probe(monkeypatch, calls=calls)
    f = tmp_path / "placeholder"
    f.touch()
    assert classify_audio(f) is False
    assert calls == []


def test_missing_ffprobe_is_not_fatal(tmp_path, monkeypatch):
    """No ffprobe on PATH must degrade to "not audio", never crash the scan."""
    f = tmp_path / "clip"
    f.write_bytes(b"x" * 100)

    def boom(*args, **kwargs):
        raise FileNotFoundError("ffprobe")

    monkeypatch.setattr("moim_scribe.pipeline.subprocess.run", boom)
    assert classify_audio(f) is False


def test_scan_picks_up_an_extensionless_recording(tmp_path, monkeypatch):
    """End to end: the 4-hour silence, reproduced and then not silent."""
    cfg = load_config(tmp_path)
    ensure_workspace_dirs(cfg)
    (cfg.inbox_dir / "phone call").write_bytes(b"x" * 20000)

    _probe(monkeypatch, returncode=0, stdout="audio\n")
    monkeypatch.setattr("moim_scribe.pipeline.is_stable_audio", lambda p: True)
    seen = []
    monkeypatch.setattr(
        "moim_scribe.pipeline.process_recording",
        lambda cfg_, claimed: seen.append(claimed.name) or claimed,
    )

    counts = scan_inbox(cfg)
    assert seen == ["phone call"]
    assert counts["processed"] == 1


def test_scan_ignores_a_stray_text_file(tmp_path, monkeypatch):
    cfg = load_config(tmp_path)
    ensure_workspace_dirs(cfg)
    (cfg.inbox_dir / "shopping list.txt").write_text("milk", encoding="utf-8")

    _probe(monkeypatch, returncode=1, stdout="")
    monkeypatch.setattr("moim_scribe.pipeline.is_stable_audio", lambda p: True)
    monkeypatch.setattr(
        "moim_scribe.pipeline.process_recording",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not be processed")),
    )

    assert scan_inbox(cfg) == {"processed": 0, "failed": 0, "skipped": 0}
