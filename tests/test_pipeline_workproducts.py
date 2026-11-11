"""Where each work product ends up after a successful pass.

This is a real contract, not bookkeeping: `enroll` can only read the jsonl
(segment start/end exist nowhere else), and scan_inbox deletes the working
jsonl on success. It is safe purely because process_recording copied it into
results/ first. The README used to point enroll at inbox/_done/ for that file
— a path that never exists — so the layout is pinned here.
"""
from moim_scribe.config import ensure_workspace_dirs, load_config
from moim_scribe.pipeline import scan_inbox

SUFFIXES = ("_transcript_diarized.jsonl", "_transcript_diarized.txt", "_speakers.json")
RESULT_JSONL = "260730_rec_diarized.jsonl"


def _fake_process(cfg, claimed):
    """Stand in for process_recording: same side effects, no models."""
    for suffix in SUFFIXES:
        (cfg.processing_dir / f"{claimed.stem}{suffix}").write_text("x", encoding="utf-8")
    cfg.results_dir.mkdir(parents=True, exist_ok=True)
    (cfg.results_dir / RESULT_JSONL).write_text("x", encoding="utf-8")
    out = cfg.results_dir / "260730_rec_summary.md"
    out.write_text("# notes\n", encoding="utf-8")
    return out


def _run(tmp_path, monkeypatch, process=_fake_process):
    cfg = load_config(tmp_path)
    ensure_workspace_dirs(cfg)
    (cfg.inbox_dir / "rec.m4a").write_bytes(b"x" * 20000)
    monkeypatch.setattr("moim_scribe.pipeline.is_stable_audio", lambda p: True)
    monkeypatch.setattr("moim_scribe.pipeline.process_recording", process)
    return cfg, scan_inbox(cfg)


def test_success_moves_audio_txt_and_stats_to_done(tmp_path, monkeypatch):
    cfg, counts = _run(tmp_path, monkeypatch)
    assert counts == {"processed": 1, "failed": 0, "skipped": 0}
    done = {p.name for p in cfg.done_dir.iterdir()}
    assert done == {"rec.m4a", "rec_transcript_diarized.txt", "rec_speakers.json"}


def test_jsonl_survives_only_in_results(tmp_path, monkeypatch):
    """enroll's one input. If this ever ends up nowhere, enrolment is impossible."""
    cfg, _ = _run(tmp_path, monkeypatch)
    assert not list(cfg.done_dir.glob("*.jsonl"))
    assert not list(cfg.processing_dir.glob("*.jsonl"))
    assert (cfg.results_dir / RESULT_JSONL).is_file()


def test_processing_dir_is_left_clean(tmp_path, monkeypatch):
    cfg, _ = _run(tmp_path, monkeypatch)
    assert list(cfg.processing_dir.iterdir()) == []


def test_failure_keeps_the_txt_for_diagnosis_and_quarantines_the_audio(tmp_path, monkeypatch):
    def failing(cfg, claimed):
        _fake_process(cfg, claimed)
        raise RuntimeError("boom")

    cfg, counts = _run(tmp_path, monkeypatch, process=failing)
    assert counts["failed"] == 1
    err = {p.name for p in cfg.error_dir.iterdir()}
    assert err == {"rec.m4a", "rec_transcript_diarized.txt"}
    assert list(cfg.processing_dir.iterdir()) == []


def test_readme_enroll_examples_read_the_jsonl_from_results(tmp_path):
    """Drift guard for the docs, in both languages.

    The jsonl only exists under results/ after a successful pass, so an enroll
    example naming inbox/_done/ for it sends the user to a missing file.
    """
    from pathlib import Path

    import moim_scribe

    repo = Path(moim_scribe.__file__).parent.parent.parent
    for name in ("README.md", "README.ko.md"):
        lines = (repo / name).read_text(encoding="utf-8").splitlines()
        enroll = [ln for ln in lines if "moim-scribe enroll" in ln]
        assert enroll, f"{name}: no enroll example found"
        assert "results/" in enroll[0], f"{name}: enroll must read the jsonl from results/"
        assert "_done/" not in enroll[0].split("results/")[0].split(".m4a")[-1], name
