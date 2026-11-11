"""One inbox, one route: what 0.2 changed about how a recording gets in.

Before 0.2 a recording had to be sorted into inbox/<type>/ and the type picked
the lens. Now every recording takes the same route, so these tests pin the two
things that could go quietly wrong: a file that is no longer looked at, and a
summary that is no longer written (or written when it should not be).
"""
import argparse
import logging
import os
import types
from pathlib import Path

import pytest

from moim_scribe import cli, pipeline
from moim_scribe.config import ensure_workspace_dirs, load_config
from moim_scribe.pipeline import inbox_folders, on_disk_ratio, scan_inbox


@pytest.fixture
def ws(tmp_path, monkeypatch):
    cfg = load_config(tmp_path)
    ensure_workspace_dirs(cfg)
    monkeypatch.setattr("moim_scribe.pipeline.is_stable_audio", lambda p: True)
    seen = []
    monkeypatch.setattr(
        "moim_scribe.pipeline.process_recording",
        lambda cfg_, claimed: seen.append(claimed.name) or claimed,
    )
    return cfg, seen


def _audio(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * 20000)


# --- where the scan looks -------------------------------------------------

def test_a_recording_dropped_straight_into_inbox_is_processed(ws):
    cfg, seen = ws
    _audio(cfg.inbox_dir / "standup.m4a")
    assert scan_inbox(cfg)["processed"] == 1
    assert seen == ["standup.m4a"]


def test_recordings_in_subfolders_are_processed_too(ws):
    """🔴 Pre-0.2 workspaces have inbox/meeting/ and friends, and people sort
    recordings into folders of their own. Not looking there would leave files
    sitting unprocessed with nothing in the log."""
    cfg, seen = ws
    _audio(cfg.inbox_dir / "meeting" / "a.m4a")
    _audio(cfg.inbox_dir / "my own folder" / "b.m4a")
    _audio(cfg.inbox_dir / "c.m4a")
    assert scan_inbox(cfg)["processed"] == 3
    assert sorted(seen) == ["a.m4a", "b.m4a", "c.m4a"]


def test_the_pipelines_own_folders_are_never_input(ws):
    """_done holds every finished recording. Scanning it would reprocess the
    whole archive on every pass."""
    cfg, seen = ws
    _audio(cfg.done_dir / "old.m4a")
    _audio(cfg.error_dir / "broken.m4a")
    _audio(cfg.inbox_dir / "_mine" / "private.m4a")
    _audio(cfg.inbox_dir / ".hidden" / "x.m4a")
    assert scan_inbox(cfg) == {"processed": 0, "failed": 0, "skipped": 0}
    assert seen == []
    assert (cfg.done_dir / "old.m4a").exists()


def test_only_one_level_of_subfolders_is_scanned(ws):
    cfg, seen = ws
    _audio(cfg.inbox_dir / "a" / "b" / "deep.m4a")
    assert scan_inbox(cfg)["processed"] == 0
    assert seen == []


def test_hidden_files_are_left_alone(ws):
    """Sync clients park partial downloads under dot-names."""
    cfg, seen = ws
    _audio(cfg.inbox_dir / ".partial.m4a")
    assert scan_inbox(cfg)["processed"] == 0
    assert seen == []


def test_inbox_folders_lists_root_first_then_subfolders_sorted(ws):
    cfg, _ = ws
    (cfg.inbox_dir / "b").mkdir()
    (cfg.inbox_dir / "a").mkdir()
    assert [d.name for d in inbox_folders(cfg)] == ["inbox", "a", "b"]


def test_inbox_folders_survives_a_missing_inbox(tmp_path):
    cfg = load_config(tmp_path)
    assert inbox_folders(cfg) == []


# --- what a recording turns into -------------------------------------------

class _Summarizer:
    def __init__(self):
        self.prompts = []

    def summarize(self, prompt):
        self.prompts.append(prompt)
        return "# Title - today\n\n## Decisions\n- ship it\n"


def _stub_engine(cfg, monkeypatch, summarizer):
    """Stand in for diarization: write the three work products, no models."""
    def fake_run(audio, **kw):
        stem = os.path.splitext(os.path.basename(audio))[0]
        base = cfg.processing_dir / stem
        txt = Path(f"{base}_transcript_diarized.txt")
        jsonl = Path(f"{base}_transcript_diarized.jsonl")
        txt.write_text("[  0.0s] SPEAKER_00: hello\n", encoding="utf-8")
        jsonl.write_text("{}\n", encoding="utf-8")
        return {"txt_path": str(txt), "jsonl_path": str(jsonl),
                "stats_path": f"{base}_speakers.json"}

    monkeypatch.setattr(pipeline, "get_hf_token", lambda *a, **k: "tok")
    monkeypatch.setattr(pipeline, "get_backend", lambda *a, **k: object())
    monkeypatch.setattr(pipeline, "run_diarization", fake_run)
    monkeypatch.setattr(pipeline, "get_summarizer", lambda *a, **k: summarizer)
    monkeypatch.setattr(pipeline, "_recording_time_block", lambda *a, **k: "")


def _claimed(cfg, name="weekly sync.m4a"):
    p = cfg.processing_dir / name
    p.write_bytes(b"x" * 100)
    return p


def _workspace(tmp_path, language="en"):
    args = argparse.Namespace(workspace=str(tmp_path), language=language, force=False)
    assert cli.cmd_init(args) == 0
    return load_config(tmp_path)


def test_every_recording_gets_the_one_summary_lens(tmp_path, monkeypatch):
    cfg = _workspace(tmp_path)
    s = _Summarizer()
    _stub_engine(cfg, monkeypatch, s)
    out = pipeline.process_recording(cfg, _claimed(cfg))
    assert out.name.endswith("_weekly sync_summary.md")
    assert out.read_text(encoding="utf-8").startswith("# Title")
    (prompt,) = s.prompts
    assert "Deliverable: recording summary" in prompt      # the lens
    assert "Output the finished deliverable only" in prompt  # the common rules
    assert "weekly sync" in prompt                          # the title block
    assert "SPEAKER_00: hello" in prompt                    # the transcript


def test_korean_workspace_gets_the_korean_lens(tmp_path, monkeypatch):
    cfg = _workspace(tmp_path, language="ko")
    s = _Summarizer()
    _stub_engine(cfg, monkeypatch, s)
    pipeline.process_recording(cfg, _claimed(cfg))
    assert "산출물: 녹음 요약" in s.prompts[0]
    assert "Deliverable: recording summary" not in s.prompts[0]


def test_summarizer_none_stops_at_the_transcript_and_needs_no_lens(tmp_path, monkeypatch):
    """`summarizer: none` is the way to get transcripts only, now that there is
    no transcribe-only folder. It must work in a workspace with no lens files:
    loading the lens first would turn a deliberate setting into a crash."""
    cfg = load_config(tmp_path)
    ensure_workspace_dirs(cfg)            # no init: packs/lenses/ is empty
    _stub_engine(cfg, monkeypatch, None)
    out = pipeline.process_recording(cfg, _claimed(cfg))
    assert out.name.endswith("_weekly sync_transcript.txt")
    assert not list(cfg.results_dir.glob("*_summary.md"))
    assert list(cfg.results_dir.glob("*_diarized.jsonl"))   # enroll's input survives


def test_a_workspace_without_the_summary_lens_uses_the_shipped_one(tmp_path, monkeypatch, caplog):
    """🔴 Every workspace created before 0.2 is in this state: it has the old
    lens files and no summary lens. Raising here would send the first
    recording after an upgrade to _error."""
    cfg = load_config(tmp_path)
    ensure_workspace_dirs(cfg)            # no init: packs/lenses/ is empty
    cfg.language = "en"
    s = _Summarizer()
    _stub_engine(cfg, monkeypatch, s)
    with caplog.at_level(logging.INFO, logger="moim_scribe.pipeline"):
        out = pipeline.process_recording(cfg, _claimed(cfg))
    assert out.name.endswith("_summary.md")
    assert "Deliverable: recording summary" in s.prompts[0]
    assert "Output the finished deliverable only" in s.prompts[0]   # common rules too
    assert any("built-in" in r.message for r in caplog.records)


def test_the_workspace_copy_of_the_lens_wins_over_the_shipped_one(tmp_path, monkeypatch):
    cfg = _workspace(tmp_path)
    (cfg.packs_dir / "lenses" / "summary.en.md").write_text("# MY OWN LENS", encoding="utf-8")
    s = _Summarizer()
    _stub_engine(cfg, monkeypatch, s)
    pipeline.process_recording(cfg, _claimed(cfg))
    assert "# MY OWN LENS" in s.prompts[0]
    assert "Deliverable: recording summary" not in s.prompts[0]


def test_a_language_without_a_translation_falls_back_to_english(tmp_path, monkeypatch):
    cfg = load_config(tmp_path)
    ensure_workspace_dirs(cfg)
    cfg.language = "ja"
    s = _Summarizer()
    _stub_engine(cfg, monkeypatch, s)
    pipeline.process_recording(cfg, _claimed(cfg))
    assert "Deliverable: recording summary" in s.prompts[0]


def test_an_unknown_lens_still_raises(tmp_path):
    cfg = load_config(tmp_path)
    ensure_workspace_dirs(cfg)
    with pytest.raises(FileNotFoundError):
        pipeline._load_lens(cfg, "no-such-lens")


def test_a_matching_pack_adds_background_and_collects_a_card(tmp_path, monkeypatch):
    cfg = _workspace(tmp_path)
    pack = cfg.packs_dir / "sync"
    pack.mkdir()
    (pack / "match.json").write_text('{"title_contains": ["weekly sync"]}', encoding="utf-8")
    (pack / "pack.md").write_text("ROSTER: Ada, Grace", encoding="utf-8")
    s = _Summarizer()
    _stub_engine(cfg, monkeypatch, s)
    pipeline.process_recording(cfg, _claimed(cfg))
    assert "ROSTER: Ada, Grace" in s.prompts[0]
    (card,) = (pack / "_history").glob("*.md")
    assert "- ship it" in card.read_text(encoding="utf-8")


def test_no_pack_means_the_plain_prompt(tmp_path, monkeypatch):
    cfg = _workspace(tmp_path)
    s = _Summarizer()
    _stub_engine(cfg, monkeypatch, s)
    pipeline.process_recording(cfg, _claimed(cfg, "budget review.m4a"))
    assert "NOT from this recording" not in s.prompts[0]


def test_process_command_ignores_a_pre_0_2_type_flag(tmp_path, monkeypatch, capsys):
    cfg = _workspace(tmp_path)
    src = tmp_path / "rec.m4a"
    src.write_bytes(b"x" * 100)
    got = []
    monkeypatch.setattr("moim_scribe.pipeline.process_recording",
                        lambda cfg_, claimed: got.append(claimed.name) or claimed)
    assert cli.main(["process", str(src), "--type", "meeting", "--workspace", str(cfg.workspace)]) == 0
    assert got == ["rec.m4a"]
    assert "ignored" in capsys.readouterr().out
    assert cli.main(["process", str(src), "--workspace", str(cfg.workspace)]) == 0
    assert "ignored" not in capsys.readouterr().out


# --- the partial-download guard ---------------------------------------------

def _stat(size, blocks):
    return types.SimpleNamespace(st_size=size, st_blocks=blocks)


class _FakePath:
    def __init__(self, st, name="rec.m4a"):
        self._st, self.name = st, name

    def stat(self):
        if isinstance(self._st, Exception):
            raise self._st
        return self._st


def test_on_disk_ratio_reads_allocated_blocks_on_macos(monkeypatch):
    monkeypatch.setattr(pipeline.sys, "platform", "darwin")
    assert on_disk_ratio(_FakePath(_stat(1_000_000, 1954))) == pytest.approx(1.0, abs=0.01)
    assert on_disk_ratio(_FakePath(_stat(1_000_000, 1260))) == pytest.approx(0.645, abs=0.001)
    assert on_disk_ratio(_FakePath(_stat(1_000_000, 0))) == 0.0


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_on_disk_ratio_declines_to_answer_off_macos(monkeypatch, platform):
    """🔴 A compressing filesystem stores fewer blocks than the logical size on
    purpose. Applying the guard there would skip a complete file forever."""
    monkeypatch.setattr(pipeline.sys, "platform", platform)
    assert on_disk_ratio(_FakePath(_stat(1_000_000, 10))) is None


def test_on_disk_ratio_cannot_tell_is_not_zero(monkeypatch):
    """Not measurable must stay distinct from measured-as-empty."""
    monkeypatch.setattr(pipeline.sys, "platform", "darwin")
    assert on_disk_ratio(_FakePath(OSError("gone"))) is None
    assert on_disk_ratio(_FakePath(types.SimpleNamespace(st_size=1000))) is None  # no st_blocks
    assert on_disk_ratio(_FakePath(_stat(0, 0))) is None


def _stable(monkeypatch, tmp_path, ratio):
    f = tmp_path / "rec.m4a"
    f.write_bytes(b"x" * 20000)
    monkeypatch.setattr(pipeline, "STABILIZE_WAIT_SEC", 0)
    monkeypatch.setattr(pipeline, "on_disk_ratio", lambda p: ratio)
    probes = []
    monkeypatch.setattr(
        pipeline.subprocess, "run",
        lambda *a, **k: probes.append(a) or types.SimpleNamespace(returncode=0),
    )
    return f, probes


def test_a_partly_downloaded_file_is_skipped_even_though_it_decodes(monkeypatch, tmp_path, caplog):
    """The upstream incident: final size from the start, first 0.1s decodes,
    64.5% on disk. Size check and probe both say yes."""
    f, probes = _stable(monkeypatch, tmp_path, 0.645)
    with caplog.at_level(logging.INFO, logger="moim_scribe.pipeline"):
        assert pipeline.is_stable_audio(f) is False
    assert probes == []                                   # stopped before the probe
    assert any("on disk" in r.message and "rec.m4a" in r.message for r in caplog.records)


@pytest.mark.parametrize("ratio,expected", [(0.949, False), (0.95, True), (1.0, True), (1.3, True)])
def test_on_disk_threshold_boundary(monkeypatch, tmp_path, ratio, expected):
    f, _ = _stable(monkeypatch, tmp_path, ratio)
    assert pipeline.is_stable_audio(f) is expected


def test_an_unmeasurable_ratio_does_not_block_the_file(monkeypatch, tmp_path):
    """Linux, Windows, or a stat that failed: fall back to the checks that
    existed before, never to a permanent skip."""
    f, probes = _stable(monkeypatch, tmp_path, None)
    assert pipeline.is_stable_audio(f) is True
    assert len(probes) == 1
