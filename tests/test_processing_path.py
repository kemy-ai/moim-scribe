"""The processing path must not consume, strand or duplicate a recording.

From an independent review of the 0.2 release fixes: a token the Hub refuses
sent the whole inbox to _error; a setup problem raised after the transcript was
written put the recording back for an endless re-transcription loop; anything
escaping a pass ended `watch`; an unwritable _done made the next pass
quarantine a processed recording; two runs could pick the same result name;
Ctrl-C stranded the claimed file in _processing.
"""
import json
import logging
import os
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from moim_scribe import diarize, diarize_cloud, pipeline
from moim_scribe.config import ensure_workspace_dirs, load_config
from moim_scribe.diarize import SetupProblem
from moim_scribe.pipeline import RunProgress, claim_result_base, scan_inbox

COUNTS = ("processed", "failed", "skipped")


def _cfg(tmp_path, yaml="language: en\nstt_backend: mlx\nsummarizer: claude_cli\n"):
    (tmp_path / "config.yaml").write_text(yaml, encoding="utf-8")
    cfg = load_config(tmp_path)
    ensure_workspace_dirs(cfg)
    return cfg


def _drop(folder, name, body=b"A" * 20_000):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(body)


def _names(folder):
    return sorted(p.name for p in folder.iterdir())


def _fake_run(audio, **kw):
    base = Path(audio).parent / Path(audio).stem
    txt = Path(f"{base}_transcript_diarized.txt")
    txt.write_text("[  0.0s] SPEAKER_00: hello\n", encoding="utf-8")
    jsonl = Path(f"{base}_transcript_diarized.jsonl")
    jsonl.write_text("{}\n", encoding="utf-8")
    sp = Path(f"{base}_speakers.json")
    sp.write_text(json.dumps({"n_speakers": 1, "speakers": []}), encoding="utf-8")
    return {"txt_path": str(txt), "jsonl_path": str(jsonl), "stats_path": str(sp)}


class _Summarizer:
    def __init__(self, action):
        self.action = action

    def summarize(self, prompt):
        return self.action()


def _stub(monkeypatch, *, run=_fake_run, summarize=lambda: "# ok\n"):
    monkeypatch.setattr(pipeline._preflight, "preflight", lambda cfg: [])
    monkeypatch.setattr(pipeline, "is_stable_audio", lambda p: True)
    monkeypatch.setattr(pipeline, "get_hf_token", lambda *a, **k: "tok")
    monkeypatch.setattr(pipeline, "get_backend", lambda *a, **k: object())
    monkeypatch.setattr(pipeline, "run_diarization", run)
    monkeypatch.setattr(pipeline, "get_summarizer", lambda *a, **k: _Summarizer(summarize))
    monkeypatch.setattr(pipeline, "_recording_time_block", lambda *a, **k: "")


# --- 1. a token the Hub refuses is a setup problem -----------------------------

def _hub_error(name, status=None, base=Exception):
    cls = type(name, (base,), {})
    e = cls(f"{name} from the hub")
    if status is not None:
        e.response = SimpleNamespace(status_code=status)
    return e


class _FakeAnnotation:
    def itertracks(self, yield_label):
        yield SimpleNamespace(start=0.0, end=1.5), None, "SPEAKER_00"


class _FakePipe:
    def __init__(self):
        self.calls = []

    def to(self, device):
        pass

    def __call__(self, audio, **kw):
        self.calls.append((audio, kw))
        return SimpleNamespace(speaker_diarization=_FakeAnnotation())


def _fake_engine_modules(monkeypatch, load, n_samples=32_000, sample_rate=16_000):
    """torch, pyannote.audio and soundfile as the bare minimum diarize_turns touches."""
    torch = types.ModuleType("torch")
    torch.backends = SimpleNamespace(mps=SimpleNamespace(is_available=lambda: False))
    torch.cuda = SimpleNamespace(is_available=lambda: False)
    torch.device = lambda name: name
    torch.from_numpy = lambda a: a
    pa = types.ModuleType("pyannote.audio")
    pa.Pipeline = SimpleNamespace(from_pretrained=load)
    pyannote = types.ModuleType("pyannote")
    pyannote.audio = pa
    sf = types.ModuleType("soundfile")
    sf.reads = []

    def read(path, dtype, always_2d):
        sf.reads.append((path, dtype, always_2d))
        return np.zeros((n_samples, 1), dtype=np.float32), sample_rate

    sf.read = read
    for name, mod in (("torch", torch), ("pyannote", pyannote),
                      ("pyannote.audio", pa), ("soundfile", sf)):
        monkeypatch.setitem(sys.modules, name, mod)
    return sf


def _raising(e):
    def load(*a, **k):
        raise e
    return load


@pytest.mark.parametrize("error", [
    _hub_error("GatedRepoError"),
    _hub_error("RepositoryNotFoundError"),
    _hub_error("HfHubHTTPError", 401),
    _hub_error("HfHubHTTPError", 403),
], ids=["gated", "not-found", "401", "403"])
def test_a_refused_token_at_model_load_is_a_setup_problem(error, monkeypatch, tmp_path):
    _fake_engine_modules(monkeypatch, _raising(error))
    with pytest.raises(SetupProblem, match="doctor --online"):
        diarize.diarize_turns(str(tmp_path / "w.wav"), "hf_x", None)


def test_a_subclass_and_a_wrapped_hub_error_are_recognised(monkeypatch, tmp_path):
    gated_base = type("RepositoryNotFoundError", (Exception,), {})
    gated = type("GatedRepoError", (gated_base,), {})("terms not accepted")
    try:
        try:
            raise gated
        except Exception as inner:
            raise RuntimeError("could not load the segmentation model") from inner
    except RuntimeError as outer:
        wrapped = outer
    _fake_engine_modules(monkeypatch, _raising(wrapped))
    with pytest.raises(SetupProblem):
        diarize.diarize_turns(str(tmp_path / "w.wav"), "hf_x", None)


def test_none_from_from_pretrained_is_a_setup_problem(monkeypatch, tmp_path):
    _fake_engine_modules(monkeypatch, lambda *a, **k: None)
    with pytest.raises(SetupProblem, match="Agree and access repository"):
        diarize.diarize_turns(str(tmp_path / "w.wav"), "hf_x", None)


@pytest.mark.parametrize("error", [
    # Offline and not cached: no token fixes that. In huggingface_hub it is an
    # HfHubHTTPError subclass, so it must be ruled out before the status check.
    _hub_error("LocalEntryNotFoundError", 401,
               base=type("HfHubHTTPError", (Exception,), {})),
    _hub_error("HfHubHTTPError", 500),
    RuntimeError("CUDA out of memory"),
], ids=["offline", "500", "other"])
def test_other_load_failures_stay_ordinary_errors(error, monkeypatch, tmp_path):
    _fake_engine_modules(monkeypatch, _raising(error))
    with pytest.raises(Exception) as got:
        diarize.diarize_turns(str(tmp_path / "w.wav"), "hf_x", None)
    assert got.value is error


def test_a_setup_problem_puts_the_recording_back_and_stops_the_pass(tmp_path, monkeypatch, caplog):
    cfg = _cfg(tmp_path)

    def refused(audio, **kw):
        _fake_run(audio)  # the engine got as far as its work products
        (Path(audio).parent / f"_diarize_full_{os.getpid()}.wav").write_bytes(b"W")
        raise SetupProblem(diarize.MODEL_ACCESS_FIX)

    _stub(monkeypatch, run=refused)
    _drop(cfg.inbox_dir, "a.m4a")
    _drop(cfg.inbox_dir, "b.m4a")
    with caplog.at_level(logging.ERROR):
        result = scan_inbox(cfg)
    assert {k: result[k] for k in COUNTS} == {"processed": 0, "failed": 0, "skipped": 0}
    assert "doctor --online" in result["setup_problems"][0]
    assert _names(cfg.inbox_dir) == sorted(["a.m4a", "b.m4a", "_processing", "_done", "_error"])
    assert _names(cfg.processing_dir) == []
    assert _names(cfg.error_dir) == []
    assert list(cfg.results_dir.iterdir()) == []


def test_the_hybrid_step_still_absorbs_a_setup_problem(tmp_path, monkeypatch):
    """On the Soniox path pyannote is an improvement: a refused token there
    must mean "keep Soniox's labels", never a stopped pass."""
    assert issubclass(SetupProblem, Exception)
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"A")
    merged = [{"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00", "text": "hi"}]
    monkeypatch.setattr(diarize_cloud, "find_hf_token", lambda *a, **k: "tok")
    monkeypatch.setattr(diarize_cloud, "prepare_audio", lambda inp, wd: str(audio))

    def refused(*a, **k):
        raise SetupProblem(diarize.MODEL_ACCESS_FIX)

    monkeypatch.setattr(diarize_cloud, "diarize_turns", refused)
    assert diarize_cloud.apply_hybrid_boundaries(str(audio), merged, tmp_path) is merged


# --- 7. pyannote gets the audio in memory ---------------------------------------

@pytest.mark.parametrize("n", [None, 3])
def test_pyannote_gets_a_waveform_dict_never_a_path(n, monkeypatch, tmp_path):
    pipe = _FakePipe()
    sf = _fake_engine_modules(monkeypatch, lambda *a, **k: pipe, n_samples=48_000)
    wav = str(tmp_path / "_diarize_full_1.wav")

    turns = diarize.diarize_turns(wav, "hf_x", n)

    assert sf.reads == [(wav, "float32", True)]
    assert len(pipe.calls) == 1
    arg, kw = pipe.calls[0]
    assert not isinstance(arg, (str, os.PathLike))
    assert set(arg) == {"waveform", "sample_rate"}
    assert arg["waveform"].shape == (1, 48_000)      # (channel, time)
    assert arg["waveform"].flags["C_CONTIGUOUS"]
    assert arg["sample_rate"] == 16_000
    assert kw == ({"num_speakers": 3} if n else {})
    assert turns == [(0.0, 1.5, "SPEAKER_00")]


# --- 2. a setup problem after results were written ---------------------------

def test_a_setup_problem_after_the_transcript_is_a_failure_not_a_loop(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)

    def gone():
        raise SystemExit("summarizer setup gone")

    _stub(monkeypatch, summarize=gone)
    _drop(cfg.inbox_dir, "standup.m4a")

    first = scan_inbox(cfg)
    second = scan_inbox(cfg)

    assert first == {"processed": 0, "failed": 1, "skipped": 0}
    assert second == {"processed": 0, "failed": 0, "skipped": 0}
    assert "standup.m4a" in _names(cfg.error_dir)
    assert "standup.m4a" not in _names(cfg.inbox_dir)
    transcripts = [n for n in _names(cfg.results_dir) if n.endswith("_transcript.txt")]
    assert len(transcripts) == 1 and " (2)" not in transcripts[0]


def test_progress_says_when_the_transcript_is_in_results(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, "language: en\nstt_backend: mlx\nsummarizer: none\n")
    _stub(monkeypatch)
    monkeypatch.setattr(pipeline, "get_summarizer", lambda *a, **k: None)
    claimed = cfg.processing_dir / "x.m4a"
    claimed.write_bytes(b"A" * 100)
    progress = RunProgress()

    pipeline.process_recording(cfg, claimed, progress=progress)

    assert progress.results_written
    assert [p.name.split("_", 1)[1] for p in progress.created] == [
        "x_transcript.txt", "x_diarized.jsonl"]


# --- 3a. watch survives a pass that raises -------------------------------------

def _import_watch(monkeypatch):
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
    return watch_mod


def test_watch_logs_a_failed_pass_and_keeps_going(tmp_path, monkeypatch, caplog):
    watch_mod = _import_watch(monkeypatch)
    passes = []

    def scan(cfg, *, reporter):
        passes.append(1)
        if len(passes) == 1:
            raise PermissionError("inbox/ is not readable")
        if len(passes) == 3:
            raise KeyboardInterrupt
        return {"processed": 0, "failed": 0, "skipped": 0}

    monkeypatch.setattr(watch_mod, "scan_inbox", scan)
    with caplog.at_level(logging.ERROR, logger="moim_scribe.watch"):
        watch_mod.watch(load_config(tmp_path), interval_sec=0)
    assert len(passes) == 3
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "PermissionError" in errors[0] and "inbox/ is not readable" in errors[0]


# --- 3b. _done cannot be written -------------------------------------------------

@pytest.mark.skipif(os.name == "nt" or getattr(os, "geteuid", lambda: 1)() == 0,
                    reason="needs POSIX permissions that apply to this user")
def test_an_unwritable_done_neither_ends_the_pass_nor_requarantines(tmp_path, monkeypatch, caplog):
    cfg = _cfg(tmp_path)
    _stub(monkeypatch)
    _drop(cfg.inbox_dir, "y.m4a")
    _drop(cfg.inbox_dir, "z.m4a")
    os.chmod(cfg.done_dir, 0o500)
    try:
        with caplog.at_level(logging.INFO):
            first = scan_inbox(cfg)
            second = scan_inbox(cfg)
    finally:
        os.chmod(cfg.done_dir, 0o700)

    assert first == {"processed": 2, "failed": 0, "skipped": 0}
    assert second == {"processed": 0, "failed": 0, "skipped": 0}
    assert {"y.m4a", "z.m4a", "y_transcript_diarized.txt"} <= set(_names(cfg.error_dir))
    assert _names(cfg.processing_dir) == []
    assert "WAS processed" in caplog.text and "needs no retry" in caplog.text
    assert "a previous run stopped" not in caplog.text
    assert not (cfg.workspace / ".moim-scribe.lock").exists()
    transcripts = [n for n in _names(cfg.results_dir) if n.endswith("_transcript.txt")]
    assert len(transcripts) == 2


# --- 3c. the decode probe times out ------------------------------------------

def test_a_probe_timeout_is_not_ready_this_pass(tmp_path, monkeypatch, caplog):
    f = tmp_path / "slow.m4a"
    f.write_bytes(b"A" * (pipeline.MIN_FILE_BYTES + 10))
    monkeypatch.setattr(pipeline, "STABILIZE_WAIT_SEC", 0.0)

    def stalls(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))

    monkeypatch.setattr(pipeline.subprocess, "run", stalls)
    with caplog.at_level(logging.WARNING):
        assert pipeline.is_stable_audio(f) is False
    assert "TimeoutExpired" in caplog.text and "slow.m4a" in caplog.text


# --- 4. result names are claimed atomically ----------------------------------

def test_two_claims_of_one_base_never_get_the_same_name(tmp_path):
    assert claim_result_base(tmp_path, "260101_x") == "260101_x"
    # The first claimant has not written anything yet; the name is still taken.
    assert claim_result_base(tmp_path, "260101_x") == "260101_x (2)"
    (tmp_path / "260101_x (3)_diarized.jsonl").write_text("{}")
    assert claim_result_base(tmp_path, "260101_x") == "260101_x (4)"


def test_a_failed_copy_leaves_no_empty_name_behind(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _stub(monkeypatch)
    claimed = cfg.processing_dir / "x.m4a"
    claimed.write_bytes(b"A" * 100)

    def disk_full(src, dst, **kw):
        Path(dst).write_bytes(b"")
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(pipeline.shutil, "copy2", disk_full)
    with pytest.raises(OSError):
        pipeline.process_recording(cfg, claimed)
    assert list(cfg.results_dir.iterdir()) == []


# --- 5. Ctrl-C puts the recording back -----------------------------------------

def test_ctrl_c_during_the_engine_puts_the_recording_back(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)

    def interrupted(audio, **kw):
        _fake_run(audio)
        (Path(audio).parent / f"_diarize_full_{os.getpid()}.wav").write_bytes(b"W")
        raise KeyboardInterrupt

    _stub(monkeypatch, run=interrupted)
    _drop(cfg.inbox_dir / "team", "a.m4a")
    with pytest.raises(KeyboardInterrupt):
        scan_inbox(cfg)
    assert _names(cfg.inbox_dir / "team") == ["a.m4a"]
    assert (cfg.inbox_dir / "team" / "a.m4a").read_bytes() == b"A" * 20_000
    assert _names(cfg.processing_dir) == []
    assert _names(cfg.error_dir) == []
    assert not (cfg.workspace / ".moim-scribe.lock").exists()


def test_ctrl_c_during_the_summary_removes_this_runs_results(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)

    def interrupted():
        raise KeyboardInterrupt

    _stub(monkeypatch, summarize=interrupted)
    (cfg.results_dir / "older_transcript.txt").write_text("keep")
    _drop(cfg.inbox_dir, "a.m4a")
    with pytest.raises(KeyboardInterrupt):
        scan_inbox(cfg)
    assert "a.m4a" in _names(cfg.inbox_dir)
    assert _names(cfg.results_dir) == ["older_transcript.txt"]
    assert _names(cfg.processing_dir) == []


# --- 6. what the messages say ---------------------------------------------------

def _exits(audio, **kw):
    raise SystemExit("No Hugging Face token found.")


def test_a_put_back_is_not_called_untouched(tmp_path, monkeypatch, caplog):
    cfg = _cfg(tmp_path)
    _stub(monkeypatch, run=_exits)
    _drop(cfg.inbox_dir, "x.m4a")
    with caplog.at_level(logging.ERROR):
        problem = scan_inbox(cfg)["setup_problems"][0]
    assert "no recording was touched" not in caplog.text
    assert "x.m4a was put back where it was" in problem
    assert "was could" not in problem


def test_a_vanished_folder_says_where_the_recording_really_is(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    team = cfg.inbox_dir / "team"

    def gone(audio, **kw):
        for f in team.iterdir():
            f.unlink()
        team.rmdir()
        raise SystemExit("no token")

    _stub(monkeypatch, run=gone)
    _drop(team, "x.m4a")
    problem = scan_inbox(cfg)["setup_problems"][0]
    assert _names(cfg.error_dir) == ["x.m4a"]
    assert str(cfg.error_dir / "x.m4a") in problem
    assert "move it back to inbox/" in problem
    assert "picked up again" not in problem
    assert scan_inbox(cfg) == {"processed": 0, "failed": 0, "skipped": 0}


@pytest.mark.parametrize("language, example", [("en", "meeting 4p"), ("ko", "회의 4명")])
def test_the_spoken_cue_line_points_at_the_file_name(language, example, tmp_path, monkeypatch, caplog):
    rec = tmp_path / "rec.m4a"
    rec.write_bytes(b"A" * 100)

    def fake_ffmpeg(cmd, check):
        Path(cmd[-1]).write_bytes(b"W" * 1000)

    calls = []

    def transcribe(*a, **k):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("stop after the cue")
        return []

    monkeypatch.setattr(diarize.subprocess, "run", fake_ffmpeg)
    monkeypatch.setattr(diarize, "transcribe_segments", transcribe)
    monkeypatch.setattr(diarize, "detect_num_speakers", lambda *a, **k: 3)
    monkeypatch.setattr(diarize, "load_diarization_pipeline", lambda token: object())
    monkeypatch.setattr(diarize, "diarize_turns", lambda *a, **k: [])
    with caplog.at_level(logging.INFO), pytest.raises(RuntimeError):
        diarize.run_diarization(str(rec), stt_backend=object(), language=language,
                                speaker_db=None, model_dir=tmp_path, hf_token="t")
    line = next(r.getMessage() for r in caplog.records
                if r.getMessage().startswith("speaker count: 3"))
    assert "default_speakers" not in line
    assert "file name" in line and example in line
    assert pipeline.filename_speaker_hint(f"x {example}", language)[0] == 4


# --- 8. low items --------------------------------------------------------------

def test_the_name_probe_is_as_long_as_the_longest_name():
    assert len(pipeline.NAME_PROBE_SUFFIX) >= len(pipeline.LONGEST_STEM_SUFFIX)
    assert pipeline.NAME_PROBE_SUFFIX.isascii() and pipeline.LONGEST_STEM_SUFFIX.isascii()


def test_an_orphaned_name_probe_is_removed_but_a_real_file_is_not(tmp_path):
    cfg = _cfg(tmp_path)
    orphan = cfg.processing_dir / f"rec{pipeline.NAME_PROBE_SUFFIX}"
    orphan.write_bytes(b"")
    not_a_probe = cfg.processing_dir / f"song{pipeline.NAME_PROBE_EXT}"
    not_a_probe.write_bytes(b"data")
    pipeline.quarantine_leftovers(cfg)
    assert not orphan.exists()
    assert not_a_probe.exists()


def test_name_fits_leaves_nothing_behind(tmp_path):
    assert pipeline.name_fits(tmp_path, "rec") is True
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("body, said", [
    ("[]", "holds a list"),
    ('"x"', "holds a str"),
    ("{not json", "is not valid JSON"),
])
def test_a_speaker_index_of_the_wrong_shape_says_what_it_is(body, said, tmp_path, real_preflight):
    cfg = _cfg(tmp_path)
    cfg.speakers_dir.mkdir(parents=True, exist_ok=True)
    (cfg.speakers_dir / "index.json").write_text(body, encoding="utf-8")
    problems = [p for p in real_preflight(cfg) if "speaker database" in p]
    assert len(problems) == 1
    assert said in problems[0]
    if said != "is not valid JSON":
        assert "not valid JSON" not in problems[0]


def test_a_refused_token_is_found_before_anything_is_transcribed(tmp_path, monkeypatch):
    """Found in review: the pipeline was loaded in the worker thread, so a
    refused token only surfaced after whisper had transcribed the whole
    recording. The recording was put back, and `watch` transcribed it again
    on every pass. The model access is now checked first."""
    rec = tmp_path / "rec.m4a"
    rec.write_bytes(b"x" * 100)

    def fake_ffmpeg(cmd, check):
        Path(cmd[-1]).write_bytes(b"W" * 1000)

    transcribed = []
    monkeypatch.setattr(diarize.subprocess, "run", fake_ffmpeg)
    monkeypatch.setattr(diarize, "transcribe_segments",
                        lambda *a, **k: transcribed.append(a) or [])
    monkeypatch.setattr(diarize, "detect_num_speakers", lambda *a, **k: None)

    def refused(token):
        raise diarize.SetupProblem("the Hub refused this token")

    monkeypatch.setattr(diarize, "load_diarization_pipeline", refused)
    for kwargs in ({"num_speakers": 2}, {}):           # with a count, and with the head clip path
        with pytest.raises(diarize.SetupProblem):
            diarize.run_diarization(str(rec), stt_backend=object(), language="en",
                                    speaker_db=None, model_dir=tmp_path, hf_token="t", **kwargs)
    assert transcribed == []                            # neither the head clip nor the recording
    assert sorted(p.name for p in tmp_path.iterdir()) == ["rec.m4a"]   # and no temp wav left


def test_the_loaded_pipeline_is_the_one_that_runs(tmp_path, monkeypatch):
    rec = tmp_path / "rec.m4a"
    rec.write_bytes(b"x" * 100)

    def fake_ffmpeg(cmd, check):
        Path(cmd[-1]).write_bytes(b"W" * 1000)

    sentinel = object()
    loads, used = [], []
    monkeypatch.setattr(diarize.subprocess, "run", fake_ffmpeg)
    monkeypatch.setattr(diarize, "transcribe_segments", lambda *a, **k: [])
    monkeypatch.setattr(diarize, "load_diarization_pipeline",
                        lambda token: loads.append(token) or sentinel)

    def turns(audio, token, num_speakers=None, pipe=None):
        used.append(pipe)
        raise RuntimeError("stop here")

    monkeypatch.setattr(diarize, "diarize_turns", turns)
    with pytest.raises(RuntimeError, match="stop here"):
        diarize.run_diarization(str(rec), stt_backend=object(), language="en",
                                speaker_db=None, model_dir=tmp_path, hf_token="t", num_speakers=2)
    assert loads == ["t"] and used == [sentinel]        # loaded once, and reused

