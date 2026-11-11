"""A setup mistake must stop a pass before any recording is touched.

From a hands-on red team: a first-time user on the local backend who forgot the
Hugging Face token lost one recording to _error per run (get_hf_token raised
SystemExit inside the per-file loop, past `except Exception`), `watch` died,
and saving the token afterwards recovered nothing. The preflight now runs
before anything is claimed, and SystemExit in the per-file handler puts the
claimed recording back where it came from.
"""
import json
import logging
import os
import sys
import types
from pathlib import Path

import pytest

from moim_scribe import diarize, pipeline, preflight
from moim_scribe.config import ensure_workspace_dirs, load_config
from moim_scribe.pipeline import SetupReporter, scan_inbox

COUNTS = ("processed", "failed", "skipped")


def _cfg(tmp_path, yaml="language: en\nstt_backend: mlx\nsummarizer: none\n"):
    (tmp_path / "config.yaml").write_text(yaml, encoding="utf-8")
    cfg = load_config(tmp_path)
    ensure_workspace_dirs(cfg)
    return cfg


def _tools_present(monkeypatch, missing=()):
    """Every PATH tool and package is there except those named in `missing`."""
    monkeypatch.setattr(preflight.shutil, "which",
                        lambda name: None if name in missing else f"/usr/bin/{name}")
    monkeypatch.setattr(preflight, "module_findable", lambda name: name not in missing)


def _no_secrets(monkeypatch):
    for var in ("HF_TOKEN", "SONIOX_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)


def _drop(folder, name):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(b"A" * 20_000)


def _stub_engine(cfg, monkeypatch, *, real_token=True):
    """Engine and summarizer stubbed; the token lookup stays the real one."""
    def fake_run(audio, **kw):
        base = Path(audio).parent / Path(audio).stem
        txt = Path(f"{base}_transcript_diarized.txt")
        txt.write_text("[  0.0s] SPEAKER_00: hello\n", encoding="utf-8")
        jsonl = Path(f"{base}_transcript_diarized.jsonl")
        jsonl.write_text("{}\n", encoding="utf-8")
        sp = Path(f"{base}_speakers.json")
        sp.write_text(json.dumps({"n_speakers": 1, "speakers": []}), encoding="utf-8")
        return {"txt_path": str(txt), "jsonl_path": str(jsonl), "stats_path": str(sp)}

    if real_token:
        monkeypatch.setattr(pipeline, "get_hf_token", diarize.get_hf_token)
    monkeypatch.setattr(pipeline, "get_backend", lambda *a, **k: object())
    monkeypatch.setattr(pipeline, "run_diarization", fake_run)
    monkeypatch.setattr(pipeline, "_recording_time_block", lambda *a, **k: "")
    monkeypatch.setattr(pipeline, "is_stable_audio", lambda p: True)


def _names(folder):
    return sorted(p.name for p in folder.iterdir())


def _inbox_files(cfg):
    return sorted(p.name for p in cfg.inbox_dir.iterdir() if p.is_file())


# --- A2: the red-team scenario (a4b_no_token) --------------------------------

def test_a_missing_token_leaves_every_recording_in_inbox_until_it_is_saved(
        tmp_path, monkeypatch, real_preflight, caplog):
    cfg = _cfg(tmp_path)
    _no_secrets(monkeypatch)
    _tools_present(monkeypatch)
    _stub_engine(cfg, monkeypatch)
    _drop(cfg.inbox_dir, "first meeting.m4a")
    _drop(cfg.inbox_dir, "second meeting.m4a")

    for _ in range(2):
        with caplog.at_level(logging.ERROR):
            result = scan_inbox(cfg)
        assert {k: result[k] for k in COUNTS} == {"processed": 0, "failed": 0, "skipped": 0}
        assert len(result["setup_problems"]) == 1
        assert ".hf_token" in result["setup_problems"][0]
        assert _inbox_files(cfg) == ["first meeting.m4a", "second meeting.m4a"]
        assert _names(cfg.error_dir) == []
        assert _names(cfg.processing_dir) == []
    assert "Hugging Face token" in caplog.text

    (tmp_path / ".hf_token").write_text("hf_dummy\n", encoding="utf-8")
    result = scan_inbox(cfg)
    assert result == {"processed": 2, "failed": 0, "skipped": 0}
    assert "setup_problems" not in result
    assert _inbox_files(cfg) == []
    assert _names(cfg.error_dir) == []
    assert sorted(p for p in _names(cfg.done_dir) if p.endswith(".m4a")) == [
        "first meeting.m4a", "second meeting.m4a"]


def test_a_failed_preflight_moves_nothing_not_even_stranded_files(
        tmp_path, monkeypatch, real_preflight, caplog):
    """quarantine_leftovers moves files too, so it waits for the preflight."""
    cfg = _cfg(tmp_path)
    _no_secrets(monkeypatch)
    _tools_present(monkeypatch, missing=("ffmpeg", "ffprobe"))
    _drop(cfg.processing_dir, "stranded.m4a")
    _drop(cfg.inbox_dir / "team", "rec.m4a")
    with caplog.at_level(logging.ERROR):
        result = scan_inbox(cfg)
    assert len(result["setup_problems"]) == 3       # ffmpeg, ffprobe, token
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 3                          # one ERROR per problem
    assert _names(cfg.processing_dir) == ["stranded.m4a"]
    assert _names(cfg.inbox_dir / "team") == ["rec.m4a"]
    assert _names(cfg.error_dir) == []


def test_an_empty_or_bom_only_token_file_is_no_token(tmp_path, monkeypatch, real_preflight):
    cfg = _cfg(tmp_path)
    _no_secrets(monkeypatch)
    _tools_present(monkeypatch)
    for content in (b"", b"\xef\xbb\xbf", b"\xff\xfe", b"  \r\n"):
        (tmp_path / ".hf_token").write_bytes(content)
        assert any("Hugging Face token" in p for p in preflight.preflight(cfg)), content


# --- A2: what each configuration requires -------------------------------------

def test_local_stt_needs_backend_pyannote_and_token(tmp_path, monkeypatch, real_preflight):
    cfg = _cfg(tmp_path)
    _no_secrets(monkeypatch)
    _tools_present(monkeypatch, missing=("mlx_whisper", "pyannote.audio"))
    problems = preflight.preflight(cfg)
    assert len(problems) == 3
    assert any("mlx_whisper" in p and "moim-scribe[mlx]" in p for p in problems)
    assert any("pyannote.audio" in p for p in problems)
    assert any("HF_TOKEN" in p and ".hf_token" in p for p in problems)


def test_faster_backend_asks_for_its_own_package(tmp_path, monkeypatch, real_preflight):
    cfg = _cfg(tmp_path, "stt_backend: faster\nsummarizer: none\n")
    monkeypatch.setenv("HF_TOKEN", "hf_x")
    _tools_present(monkeypatch, missing=("faster_whisper",))
    assert preflight.preflight(cfg) == [
        next(c.problem for c in preflight.checks(cfg) if not c.ok)]
    assert "moim-scribe[cross]" in preflight.preflight(cfg)[0]


def test_soniox_needs_the_key_and_neither_pyannote_nor_a_token(tmp_path, monkeypatch, real_preflight):
    cfg = _cfg(tmp_path, "stt_backend: soniox\nsummarizer: none\n")
    _no_secrets(monkeypatch)
    _tools_present(monkeypatch, missing=("pyannote.audio", "mlx_whisper", "faster_whisper"))
    problems = preflight.preflight(cfg)
    assert len(problems) == 1
    assert "Soniox API key" in problems[0] and ".soniox_key" in problems[0]
    (tmp_path / ".soniox_key").write_text("key\n", encoding="utf-8")
    assert preflight.preflight(cfg) == []


def test_speechbrain_is_needed_only_once_someone_is_enrolled(tmp_path, monkeypatch, real_preflight):
    cfg = _cfg(tmp_path)
    monkeypatch.setenv("HF_TOKEN", "hf_x")
    _tools_present(monkeypatch, missing=("speechbrain",))
    assert preflight.preflight(cfg) == []
    index = cfg.speakers_dir / "index.json"
    index.write_text("{}", encoding="utf-8")
    assert preflight.preflight(cfg) == []
    index.write_text(json.dumps({"Alex": {"n_samples": 1}}), encoding="utf-8")
    problems = preflight.preflight(cfg)
    assert len(problems) == 1 and "speechbrain" in problems[0]
    index.write_text("{not json", encoding="utf-8")
    problems = preflight.preflight(cfg)
    assert len(problems) == 1 and "index.json" in problems[0]


@pytest.mark.parametrize("summarizer, env, missing, bad", [
    ("claude_cli", {}, ("claude",), True),
    ("claude_cli", {}, (), False),
    ("anthropic_api", {}, (), True),
    ("anthropic_api", {"ANTHROPIC_API_KEY": "k"}, (), False),
    ("ollama", {}, ("claude",), False),
    ("none", {}, ("claude",), False),
])
def test_the_summarizer_tool_is_checked(tmp_path, monkeypatch, real_preflight,
                                        summarizer, env, missing, bad):
    cfg = _cfg(tmp_path, f"stt_backend: mlx\nsummarizer: {summarizer}\n")
    _no_secrets(monkeypatch)
    monkeypatch.setenv("HF_TOKEN", "hf_x")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    _tools_present(monkeypatch, missing=missing)
    problems = preflight.preflight(cfg)
    assert bool(problems) is bad
    if bad:
        assert "config.yaml" in problems[0]


def test_module_findable_does_not_run_the_parent_package(tmp_path, monkeypatch):
    """find_spec("a.b") would import `a`; for pyannote that can be heavy."""
    pkg = tmp_path / "heavyparent"
    (pkg / "child").mkdir(parents=True)
    (pkg / "__init__.py").write_text("raise RuntimeError('parent executed')\n", encoding="utf-8")
    (pkg / "child" / "__init__.py").write_text("raise RuntimeError('child executed')\n",
                                               encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    assert preflight.module_findable("heavyparent.child") is True
    assert preflight.module_findable("heavyparent.nothere") is False
    assert preflight.module_findable("no_such_top_level_pkg.audio") is False
    assert "heavyparent" not in sys.modules


# --- A2: SystemExit inside the per-file handler --------------------------------

def test_systemexit_puts_the_recording_back_and_stops_the_pass(tmp_path, monkeypatch, caplog):
    """The preflight cannot foresee everything. A SystemExit from processing is
    a setup problem: back to the folder it came from (never _error, never
    over a same-named file that appeared meanwhile), leftovers removed, pass
    stopped, and SystemExit does not escape (it would end `watch`)."""
    cfg = _cfg(tmp_path)
    _stub_engine(cfg, monkeypatch, real_token=False)
    team = cfg.inbox_dir / "team"
    _drop(team, "a.m4a")
    _drop(team, "b.m4a")

    def exits(audio, **kw):
        folder = Path(audio).parent
        (folder / "a_transcript_diarized.jsonl").write_text("{}", encoding="utf-8")
        (folder / f"_diarize_full_{os.getpid()}.wav").write_bytes(b"W" * 1000)
        (team / "a.m4a").write_bytes(b"NEW")   # the user drops a new take meanwhile
        raise SystemExit("No Hugging Face token found.")

    monkeypatch.setattr(pipeline, "get_hf_token", lambda *a, **k: "tok")
    monkeypatch.setattr(pipeline, "run_diarization", exits)
    with caplog.at_level(logging.ERROR):
        result = scan_inbox(cfg)

    assert {k: result[k] for k in COUNTS} == {"processed": 0, "failed": 0, "skipped": 0}
    assert len(result["setup_problems"]) == 1
    assert "setup problem" in result["setup_problems"][0]
    assert "No Hugging Face token" in caplog.text
    assert _names(team) == ["a (2).m4a", "a.m4a", "b.m4a"]   # b never claimed
    assert (team / "a.m4a").read_bytes() == b"NEW"            # not overwritten
    assert (team / "a (2).m4a").read_bytes() == b"A" * 20_000
    assert _names(cfg.processing_dir) == []
    assert _names(cfg.error_dir) == []


# --- watch: keeps running, does not repeat itself -----------------------------

def test_setup_reporter_repeats_only_on_change_or_every_n(caplog):
    rep = SetupReporter(repeat_every=3)
    with caplog.at_level(logging.INFO):
        for problems in (["x"], ["x"], ["x"], ["x"], ["x", "y"], ["x", "y"], [], []):
            rep.report(problems)
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    # pass 1 logs, 2-3 are quiet, 4 is the third repeat, 5 changed (2 lines)
    assert len(errors) == 1 + 1 + 2
    resolved = [r for r in caplog.records if "resolved" in r.getMessage()]
    assert len(resolved) == 1


def test_a_one_off_scan_always_logs(caplog):
    rep = SetupReporter()
    with caplog.at_level(logging.ERROR):
        rep.report(["x"])
        rep.report(["x"])
    assert len(caplog.records) == 2


def test_watch_survives_setup_problems_and_shares_one_reporter(tmp_path, monkeypatch):
    fake_events = types.ModuleType("watchdog.events")
    fake_events.FileSystemEventHandler = object
    fake_observers = types.ModuleType("watchdog.observers")

    class _Observer:
        def schedule(self, *a, **k): pass
        def start(self): pass
        def stop(self): pass
        def join(self): pass

    fake_observers.Observer = _Observer
    monkeypatch.setitem(sys.modules, "watchdog", types.ModuleType("watchdog"))
    monkeypatch.setitem(sys.modules, "watchdog.events", fake_events)
    monkeypatch.setitem(sys.modules, "watchdog.observers", fake_observers)
    monkeypatch.delitem(sys.modules, "moim_scribe.watch", raising=False)
    import moim_scribe.watch as watch_mod

    seen = []

    def fake_scan(cfg, *, reporter):
        seen.append(reporter)
        if len(seen) == 3:
            raise KeyboardInterrupt
        return {"processed": 0, "failed": 0, "skipped": 0, "setup_problems": ["x"]}

    monkeypatch.setattr(watch_mod, "scan_inbox", fake_scan)
    watch_mod.watch(load_config(tmp_path), interval_sec=0)
    assert len(seen) == 3
    assert seen[0] is seen[1] is seen[2]
    assert seen[0].repeat_every == watch_mod.SETUP_REPEAT_EVERY_PASSES


# --- B5: a workspace from 0.1 (the a9_upgrade scenario) -----------------------

def test_a_types_block_scans_exactly_the_folders_it_names(tmp_path, monkeypatch, caplog):
    import unicodedata

    cfg = _cfg(tmp_path, "language: en\nsummarizer: none\ntypes:\n  meeting: meeting\n"
                         "  gathering: gathering\n  transcript: null\n  Private: null\n"
                         "  비공개: null\n")
    nfd = unicodedata.normalize("NFD", "비공개")
    calls = {}
    monkeypatch.setattr(pipeline, "is_stable_audio", lambda p: True)
    monkeypatch.setattr(pipeline, "process_recording",
                        lambda cfg_, claimed, progress=None, **kw: calls.__setitem__(claimed.name, kw) or claimed)
    for sub, name in (("meeting", "m.m4a"), ("gathering", "g.m4a"), ("transcript", "t.m4a"),
                      ("private", "p.m4a"), (nfd, "n.m4a"), ("hold-for-later", "h.m4a"),
                      (".", "stray in root.m4a"), ("hold-for-later", "h2.m4a")):
        _drop(cfg.inbox_dir / sub, name)
    with caplog.at_level(logging.WARNING):
        result = scan_inbox(cfg)
    assert result == {"processed": 5, "failed": 0, "skipped": 0}
    assert calls == {
        "m.m4a": {"summarize": True},
        "g.m4a": {"summarize": True},
        "t.m4a": {"summarize": False},
        "p.m4a": {"summarize": False},       # key `Private` vs folder `private`
        "n.m4a": {"summarize": False},       # NFC key vs NFD folder
    }
    assert (cfg.inbox_dir / "stray in root.m4a").exists()
    assert _names(cfg.inbox_dir / "hold-for-later") == ["h.m4a", "h2.m4a"]
    warnings = [r.getMessage() for r in caplog.records
                if r.name == "moim_scribe.pipeline" and "`types:` block" in r.getMessage()]
    assert len(warnings) == 1
    assert warnings[0].startswith("3 recording(s)")
    assert "delete the `types:` block from config.yaml" in warnings[0]


def test_no_types_block_means_root_and_subfolders_and_no_warning(tmp_path, monkeypatch, caplog):
    cfg = _cfg(tmp_path, "summarizer: none\n")
    monkeypatch.setattr(pipeline, "is_stable_audio", lambda p: True)
    seen = []
    monkeypatch.setattr(pipeline, "process_recording",
                        lambda cfg_, claimed, progress=None, **kw: seen.append((claimed.name, kw)) or claimed)
    _drop(cfg.inbox_dir, "r.m4a")
    _drop(cfg.inbox_dir / "transcript", "t.m4a")
    with caplog.at_level(logging.WARNING):
        assert scan_inbox(cfg)["processed"] == 2
    assert sorted(seen) == [("r.m4a", {"summarize": True}), ("t.m4a", {"summarize": True})]
    assert "`types:` block" not in caplog.text


def test_a_types_block_still_skips_symlinked_and_reserved_folders(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, "summarizer: none\ntypes:\n  _done: x\n  .hidden: x\n  link: x\n")
    _drop(cfg.done_dir, "old.m4a")
    (cfg.inbox_dir / ".hidden").mkdir()
    try:
        (cfg.inbox_dir / "link").symlink_to(cfg.done_dir, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks not permitted here")
    assert pipeline.inbox_folders(cfg) == []


# --- diarize temp files (W3a's leftover) ---------------------------------------

def test_run_diarization_removes_its_temp_wav_when_it_raises(tmp_path, monkeypatch):
    rec = tmp_path / "rec.m4a"
    rec.write_bytes(b"A" * 100)

    def fake_ffmpeg(cmd, check):
        Path(cmd[-1]).write_bytes(b"W" * 1000)

    monkeypatch.setattr(diarize.subprocess, "run", fake_ffmpeg)
    monkeypatch.setattr(diarize, "diarize_turns", lambda *a, **k: [])

    def boom(*a, **k):
        raise RuntimeError("whisper died")

    monkeypatch.setattr(diarize, "transcribe_segments", boom)
    with pytest.raises(RuntimeError, match="whisper died"):
        diarize.run_diarization(str(rec), stt_backend=object(), language="en",
                                speaker_db=None, model_dir=tmp_path, hf_token="t",
                                num_speakers=2)
    assert _names(tmp_path) == ["rec.m4a"]


def test_prepare_audio_removes_a_partial_wav_when_ffmpeg_fails(tmp_path, monkeypatch):
    import subprocess

    rec = tmp_path / "rec.m4a"
    rec.write_bytes(b"A" * 100)

    def half_ffmpeg(cmd, check):
        Path(cmd[-1]).write_bytes(b"W" * 10)
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(diarize.subprocess, "run", half_ffmpeg)
    with pytest.raises(subprocess.CalledProcessError):
        diarize.prepare_audio(str(rec), str(tmp_path))
    assert _names(tmp_path) == ["rec.m4a"]
