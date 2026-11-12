"""The wait for a speaker count (0.2.1).

A recording whose file name gives no count stays in inbox/ for a short while
so the user can give the count; then it is processed with an estimated count.
These tests pin the gate in scan_inbox, the records it keeps (headcount.py),
and where the summary is told that a count is or is not an estimate.

Time is never waited for: scan_inbox takes a clock.
"""
import json
import logging
import os
import subprocess
import sys
import threading
import unicodedata
from pathlib import Path

import pytest

import moim_scribe
from moim_scribe import cli, diarize, headcount, pipeline
from moim_scribe.config import ConfigError, ensure_workspace_dirs, load_config
from moim_scribe.pipeline import scan_inbox

T0 = 1_000_000.0


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def ws(tmp_path, monkeypatch, real_count_wait):
    cfg = load_config(tmp_path)
    ensure_workspace_dirs(cfg)
    cfg.language = "en"
    monkeypatch.setattr("moim_scribe.pipeline.is_stable_audio", lambda p: True)
    calls = []

    def fake_process(cfg_, claimed, progress=None, **kw):
        calls.append((claimed.name, kw))
        return claimed

    monkeypatch.setattr("moim_scribe.pipeline.process_recording", fake_process)
    cfg._calls = calls
    return cfg


def _audio(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * 20000)
    return path


def _records(cfg):
    d = headcount.headcount_dir(cfg)
    return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


# --- the gate ----------------------------------------------------------------

def test_a_recording_without_a_count_waits_in_place(ws, caplog):
    cfg = ws
    rec = _audio(cfg.inbox_dir / "standup.m4a")
    with caplog.at_level(logging.INFO):
        assert scan_inbox(cfg, clock=Clock()) == {
            "processed": 0, "failed": 0, "skipped": 0, "waiting": 1}
    assert rec.exists() and cfg._calls == []
    assert sum("waiting up to 180s" in r.getMessage() for r in caplog.records) == 1


def test_the_wait_is_announced_once_not_every_pass(ws, caplog):
    cfg = ws
    _audio(cfg.inbox_dir / "standup.m4a")
    clock = Clock()
    with caplog.at_level(logging.INFO):
        for step in range(5):
            clock.t = T0 + step * 30
            assert scan_inbox(cfg, clock=clock)["waiting"] == 1
    assert sum("waiting up to" in r.getMessage() for r in caplog.records) == 1


def test_a_given_count_starts_at_once_and_reaches_processing(ws):
    cfg = ws
    rec = _audio(cfg.inbox_dir / "standup.m4a")
    clock = Clock()
    assert scan_inbox(cfg, clock=clock)["waiting"] == 1
    headcount.record_count(cfg, rec, 4, now=clock.t + 10)
    clock.t += 20
    assert scan_inbox(cfg, clock=clock) == {"processed": 1, "failed": 0, "skipped": 0}
    assert cfg._calls == [("standup.m4a", {"summarize": True, "given_speakers": 4})]


def test_a_count_given_before_the_scan_sees_the_file_also_works(ws):
    cfg = ws
    rec = _audio(cfg.inbox_dir / "standup.m4a")
    headcount.record_count(cfg, rec, 3, now=T0)
    assert scan_inbox(cfg, clock=Clock(T0 + 1))["processed"] == 1
    assert cfg._calls[0][1]["given_speakers"] == 3


def test_after_the_wait_it_is_processed_with_an_estimate(ws, caplog):
    cfg = ws
    _audio(cfg.inbox_dir / "standup.m4a")
    clock = Clock()
    scan_inbox(cfg, clock=clock)
    clock.t = T0 + 179
    assert scan_inbox(cfg, clock=clock)["waiting"] == 1
    clock.t = T0 + 180
    with caplog.at_level(logging.INFO):
        assert scan_inbox(cfg, clock=clock) == {"processed": 1, "failed": 0, "skipped": 0}
    assert cfg._calls == [("standup.m4a", {"summarize": True})]   # no count passed
    assert any("estimating it" in r.getMessage() for r in caplog.records)


def test_the_configured_wait_is_used(ws):
    cfg = ws
    cfg.speaker_count_wait_sec = 600
    _audio(cfg.inbox_dir / "standup.m4a")
    clock = Clock()
    scan_inbox(cfg, clock=clock)
    clock.t = T0 + 590
    assert scan_inbox(cfg, clock=clock)["waiting"] == 1
    clock.t = T0 + 600
    assert scan_inbox(cfg, clock=clock)["processed"] == 1


def test_the_wait_counts_from_first_seen_not_from_the_file_time(ws):
    """A copy can keep an old modification time; the wait must not be over
    before it began."""
    cfg = ws
    rec = _audio(cfg.inbox_dir / "standup.m4a")
    os.utime(rec, (T0 - 86400, T0 - 86400))
    assert scan_inbox(cfg, clock=Clock())["waiting"] == 1


def test_a_wait_of_zero_is_the_0_2_0_behaviour(ws):
    cfg = ws
    cfg.speaker_count_wait_sec = 0
    _audio(cfg.inbox_dir / "standup.m4a")
    assert scan_inbox(cfg, clock=Clock()) == {"processed": 1, "failed": 0, "skipped": 0}
    assert cfg._calls == [("standup.m4a", {"summarize": True})]
    assert not headcount.headcount_dir(cfg).exists()      # nothing written either


def test_default_speakers_means_no_wait(ws):
    cfg = ws
    cfg.default_speakers = 2
    _audio(cfg.inbox_dir / "standup.m4a")
    assert scan_inbox(cfg, clock=Clock())["processed"] == 1
    assert not headcount.headcount_dir(cfg).exists()


@pytest.mark.parametrize("language, name", [("ko", "독서모임 5명.m4a"), ("en", "standup 4p.m4a")])
def test_a_count_in_the_file_name_means_no_wait(ws, language, name):
    cfg = ws
    cfg.language = language
    _audio(cfg.inbox_dir / name)
    assert scan_inbox(cfg, clock=Clock())["processed"] == 1
    assert cfg._calls == [(name, {"summarize": True})]   # the name's count is read later
    assert not headcount.headcount_dir(cfg).exists()


def test_wait_false_processes_now(ws):
    cfg = ws
    _audio(cfg.inbox_dir / "standup.m4a")
    assert scan_inbox(cfg, wait=False, clock=Clock()) == {
        "processed": 1, "failed": 0, "skipped": 0}


def test_wait_false_still_uses_a_given_count(ws):
    cfg = ws
    rec = _audio(cfg.inbox_dir / "standup.m4a")
    headcount.record_count(cfg, rec, 6, now=T0)
    scan_inbox(cfg, wait=False, clock=Clock(T0 + 1))
    assert cfg._calls[0][1]["given_speakers"] == 6


def test_only_the_countless_one_waits(ws):
    cfg = ws
    cfg.language = "ko"
    _audio(cfg.inbox_dir / "a 3명.m4a")
    _audio(cfg.inbox_dir / "b.m4a")
    assert scan_inbox(cfg, clock=Clock()) == {
        "processed": 1, "failed": 0, "skipped": 0, "waiting": 1}
    assert [c[0] for c in cfg._calls] == ["a 3명.m4a"]


# --- records -----------------------------------------------------------------

def test_nfd_file_name_and_nfc_query_are_the_same_recording(ws):
    cfg = ws
    nfc = "주간 회의.m4a"
    nfd = unicodedata.normalize("NFD", nfc)
    _audio(cfg.inbox_dir / nfd)
    assert headcount.record_key(headcount.recording_name(cfg, cfg.inbox_dir / nfd)) == \
        headcount.record_key(headcount.recording_name(cfg, cfg.inbox_dir / nfc))
    assert headcount.recording_name(cfg, cfg.inbox_dir / nfd) == nfc   # what a list shows
    found = headcount.find_recording(cfg, "주간")              # typed: NFC
    headcount.record_count(cfg, found, 5, now=T0)
    assert scan_inbox(cfg, clock=Clock(T0 + 1))["processed"] == 1
    assert cfg._calls[0][1]["given_speakers"] == 5


def test_the_key_itself_does_not_depend_on_normalization():
    """A name typed by a command can reach record_key without going through
    recording_name."""
    nfc = "팀/주간 회의.m4a"
    assert headcount.record_key(unicodedata.normalize("NFD", nfc)) == headcount.record_key(nfc)


def test_record_file_names_do_not_grow_with_the_recording_name(ws):
    """Decomposed Hangul is three bytes per jamo: a long title would pass the
    255-byte name limit of Linux if the record used it as its file name."""
    cfg = ws
    long_nfd = unicodedata.normalize("NFD", "가" * 60) + ".m4a"
    rec = cfg.inbox_dir / long_nfd
    try:
        _audio(rec)
    except OSError:
        pytest.skip("this filesystem does not take the name itself")
    headcount.record_count(cfg, rec, 2, now=T0)
    assert all(len(n.encode()) < 64 for n in _records(cfg))


def test_records_of_a_removed_recording_are_cleaned_up(ws):
    cfg = ws
    rec = _audio(cfg.inbox_dir / "standup.m4a")
    scan_inbox(cfg, clock=Clock())
    headcount.record_count(cfg, rec, 4, now=T0 + 1)
    assert len(_records(cfg)) == 2
    rec.unlink()
    scan_inbox(cfg, clock=Clock(T0 + 10))
    assert _records(cfg) == []


def test_a_count_written_after_the_listing_is_not_pruned(ws):
    """The file can arrive and get its count between the scan listing inbox/
    and pruning: that count must survive."""
    cfg = ws
    gone = cfg.inbox_dir / "arriving.m4a"
    name = headcount.recording_name(cfg, gone)
    headcount._write(headcount.headcount_dir(cfg) / f"{headcount.record_key(name)}.count",
                     {"path": name, "speakers": 3, "written_at": T0 + 5})
    headcount.prune(cfg, set(), listed_at=T0, now=T0 + 6)
    assert len(_records(cfg)) == 1
    headcount.prune(cfg, set(), listed_at=T0 + 10, now=T0 + 11)
    assert _records(cfg) == []


def test_processing_removes_the_records_so_a_same_name_file_starts_fresh(ws):
    cfg = ws
    rec = _audio(cfg.inbox_dir / "standup.m4a")
    headcount.record_count(cfg, rec, 4, now=T0)
    scan_inbox(cfg, clock=Clock(T0 + 1))
    assert _records(cfg) == []
    _audio(cfg.inbox_dir / "standup.m4a")                 # next week's
    assert scan_inbox(cfg, clock=Clock(T0 + 100))["waiting"] == 1


def test_a_setup_problem_keeps_the_given_count(ws, monkeypatch):
    cfg = ws

    def stop(cfg_, claimed, progress=None, **kw):
        raise SystemExit("no token")

    monkeypatch.setattr("moim_scribe.pipeline.process_recording", stop)
    rec = _audio(cfg.inbox_dir / "standup.m4a")
    headcount.record_count(cfg, rec, 4, now=T0)
    result = scan_inbox(cfg, clock=Clock(T0 + 1))
    assert result["setup_problems"]
    assert rec.exists()
    assert headcount.given_count(cfg, "standup.m4a") == 4


def test_ctrl_c_keeps_the_given_count(ws, monkeypatch):
    cfg = ws

    def stop(cfg_, claimed, progress=None, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr("moim_scribe.pipeline.process_recording", stop)
    rec = _audio(cfg.inbox_dir / "standup.m4a")
    headcount.record_count(cfg, rec, 4, now=T0)
    with pytest.raises(KeyboardInterrupt):
        scan_inbox(cfg, clock=Clock(T0 + 1))
    assert rec.exists() and headcount.given_count(cfg, "standup.m4a") == 4


# --- broken records fall back to 0.2.0 ---------------------------------------

def _record_path(cfg, name, suffix):
    d = headcount.headcount_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{headcount.record_key(name)}{suffix}"


@pytest.mark.parametrize("body", ["", "{not json", "[]", '{"first_seen": "soon"}'])
def test_a_broken_seen_record_does_not_hold_the_recording(ws, body):
    cfg = ws
    _audio(cfg.inbox_dir / "standup.m4a")
    _record_path(cfg, "standup.m4a", ".seen").write_text(body, encoding="utf-8")
    assert scan_inbox(cfg, clock=Clock()) == {"processed": 1, "failed": 0, "skipped": 0}


@pytest.mark.parametrize("body", ["{not json", '{"speakers": 0}', '{"speakers": "4"}',
                                  '{"speakers": true}', '{"speakers": 99}'])
def test_a_broken_count_record_is_ignored(ws, body):
    cfg = ws
    _audio(cfg.inbox_dir / "standup.m4a")
    _record_path(cfg, "standup.m4a", ".count").write_text(body, encoding="utf-8")
    assert scan_inbox(cfg, clock=Clock())["waiting"] == 1
    assert scan_inbox(cfg, clock=Clock(T0 + 180)) == {"processed": 1, "failed": 0, "skipped": 0}
    assert cfg._calls == [("standup.m4a", {"summarize": True})]


def test_an_unusable_record_folder_means_no_wait(ws, caplog):
    cfg = ws
    headcount.headcount_dir(cfg).write_text("not a folder", encoding="utf-8")
    _audio(cfg.inbox_dir / "standup.m4a")
    with caplog.at_level(logging.WARNING):
        assert scan_inbox(cfg, clock=Clock()) == {"processed": 1, "failed": 0, "skipped": 0}
    assert any("cannot keep a record" in r.getMessage() for r in caplog.records)


def test_a_first_seen_in_the_future_does_not_wait_forever(ws):
    cfg = ws
    _audio(cfg.inbox_dir / "standup.m4a")
    _record_path(cfg, "standup.m4a", ".seen").write_text(
        json.dumps({"path": "standup.m4a", "first_seen": T0 + 10_000, "written_at": T0}),
        encoding="utf-8")
    assert scan_inbox(cfg, clock=Clock())["processed"] == 1


# --- concurrent writers ------------------------------------------------------

def test_scan_and_count_writers_in_threads_lose_nothing(ws):
    cfg = ws
    _audio(cfg.inbox_dir / "standup.m4a")
    name = "standup.m4a"
    errors = []

    def writer(n):
        try:
            for i in range(200):
                headcount._write(_record_path(cfg, name, ".count"),
                                 {"path": name, "speakers": 1 + (n + i) % 30, "written_at": T0})
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    def seer():
        try:
            for i in range(200):
                headcount.mark_seen(cfg, f"other{i}.m4a", T0)
                assert headcount.first_seen(cfg, name) in (None, T0)
                headcount.mark_seen(cfg, name, T0)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(3)]
    threads.append(threading.Thread(target=seer))
    for t in threads:
        t.start()
    reads = []
    while any(t.is_alive() for t in threads):
        reads.append(headcount.given_count(cfg, name))
    for t in threads:
        t.join()
    assert errors == []
    # Once written, a count is always readable: never torn, never lost.
    first = next((i for i, r in enumerate(reads) if r is not None), len(reads))
    assert all(r is not None for r in reads[first:])
    assert headcount.given_count(cfg, name) is not None
    assert headcount.first_seen(cfg, name) == T0
    assert not [p for p in headcount.headcount_dir(cfg).iterdir() if p.name.endswith(".tmp")]


_CHILD = """
import sys
from pathlib import Path
from moim_scribe import headcount
from moim_scribe.config import load_config
cfg = load_config(sys.argv[1])
rec = cfg.inbox_dir / "standup.m4a"
for i in range(150):
    headcount.record_count(cfg, rec, 1 + (int(sys.argv[2]) + i) % 30, now=1.0)
"""


def test_two_processes_writing_counts_never_tear_the_record(ws):
    cfg = ws
    _audio(cfg.inbox_dir / "standup.m4a")
    src = str(Path(moim_scribe.__file__).resolve().parent.parent)
    env = dict(os.environ, PYTHONPATH=src + os.pathsep + os.environ.get("PYTHONPATH", ""))
    procs = [subprocess.Popen([sys.executable, "-c", _CHILD, str(cfg.workspace), str(n)], env=env)
             for n in (0, 7)]
    reads = []
    while any(p.poll() is None for p in procs):
        reads.append(headcount.given_count(cfg, "standup.m4a"))
        headcount.mark_seen(cfg, "standup.m4a", T0)         # the scan's own writes meanwhile
    assert [p.wait() for p in procs] == [0, 0]
    first = next((i for i, r in enumerate(reads) if r is not None), len(reads))
    assert all(r is not None for r in reads[first:])
    assert 1 <= headcount.given_count(cfg, "standup.m4a") <= 30
    assert headcount.first_seen(cfg, "standup.m4a") == T0


# --- the functions the commands use ------------------------------------------

def test_waiting_list_and_latest(ws):
    cfg = ws
    _audio(cfg.inbox_dir / "older.m4a")
    scan_inbox(cfg, clock=Clock(T0))
    _audio(cfg.inbox_dir / "newer.m4a")
    scan_inbox(cfg, clock=Clock(T0 + 60))
    _audio(cfg.inbox_dir / "with 3p.m4a")                    # has a count: not waiting
    waiting = headcount.waiting_recordings(cfg, now=T0 + 90)
    assert [(w.name, w.seconds_left) for w in waiting] == [("older.m4a", 90.0), ("newer.m4a", 150.0)]
    assert headcount.latest_waiting(cfg, now=T0 + 90).name == "newer.m4a"
    _audio(cfg.inbox_dir / "just dropped.m4a")              # not scanned yet
    latest = headcount.latest_waiting(cfg, now=T0 + 90)
    assert latest.name == "just dropped.m4a" and latest.first_seen is None
    assert latest.seconds_left == 180.0


def test_a_given_count_takes_it_off_the_waiting_list(ws):
    cfg = ws
    rec = _audio(cfg.inbox_dir / "standup.m4a")
    scan_inbox(cfg, clock=Clock())
    headcount.record_count(cfg, rec, 2, now=T0 + 1)
    assert headcount.waiting_recordings(cfg, now=T0 + 2) == []
    assert headcount.clear_count(cfg, rec) is True
    assert [w.name for w in headcount.waiting_recordings(cfg, now=T0 + 2)] == ["standup.m4a"]
    assert headcount.clear_count(cfg, rec) is False


def test_nothing_waits_when_waiting_is_off(ws):
    cfg = ws
    _audio(cfg.inbox_dir / "standup.m4a")
    cfg.speaker_count_wait_sec = 0
    assert headcount.waiting_recordings(cfg) == []
    cfg.speaker_count_wait_sec, cfg.default_speakers = 180, 2
    assert headcount.waiting_recordings(cfg) == []


def test_find_recording_by_part_of_the_name(ws):
    cfg = ws
    a = _audio(cfg.inbox_dir / "book club.m4a")
    _audio(cfg.inbox_dir / "team" / "book review.m4a")
    assert headcount.find_recording(cfg, "CLUB") == a
    assert headcount.find_recording(cfg, "book club.m4a") == a
    with pytest.raises(headcount.AmbiguousRecording) as e:
        headcount.find_recording(cfg, "book")
    assert len(e.value.candidates) == 2
    with pytest.raises(headcount.NoSuchRecording):
        headcount.find_recording(cfg, "nothing like it")
    with pytest.raises(headcount.NoSuchRecording):
        headcount.find_recording(cfg, "  ")


def test_an_exact_name_wins_over_a_longer_one(ws):
    cfg = ws
    a = _audio(cfg.inbox_dir / "sync.m4a")
    _audio(cfg.inbox_dir / "sync 2.m4a")
    assert headcount.find_recording(cfg, "sync") == a


@pytest.mark.parametrize("bad", [0, 31, -1, 2.5, True, "4"])
def test_record_count_rejects_a_count_out_of_range(ws, bad):
    cfg = ws
    rec = _audio(cfg.inbox_dir / "standup.m4a")
    with pytest.raises(headcount.HeadcountError):
        headcount.record_count(cfg, rec, bad)
    assert _records(cfg) == []


def test_record_count_needs_a_file_in_inbox(ws, tmp_path):
    cfg = ws
    outside = _audio(tmp_path / "elsewhere.m4a")
    with pytest.raises(headcount.NoSuchRecording):
        headcount.record_count(cfg, outside, 3)
    with pytest.raises(headcount.NoSuchRecording):
        headcount.record_count(cfg, cfg.inbox_dir / "gone.m4a", 3)


def test_record_count_says_when_the_file_name_wins(ws):
    cfg = ws
    rec = _audio(cfg.inbox_dir / "standup 4p.m4a")
    assert headcount.record_count(cfg, rec, 3).filename_count == 4
    rec2 = _audio(cfg.inbox_dir / "standup.m4a")
    assert headcount.record_count(cfg, rec2, 3).filename_count is None


def test_the_recording_itself_is_never_touched_while_waiting(ws):
    cfg = ws
    rec = _audio(cfg.inbox_dir / "standup.m4a")
    before = (rec.stat().st_mtime_ns, rec.read_bytes())
    scan_inbox(cfg, clock=Clock())
    headcount.record_count(cfg, rec, 2, now=T0)
    assert sorted(p.name for p in cfg.inbox_dir.iterdir() if p.is_file()) == ["standup.m4a"]
    assert (rec.stat().st_mtime_ns, rec.read_bytes()) == before


# --- config ------------------------------------------------------------------

def test_the_wait_defaults_to_three_minutes(tmp_path):
    assert load_config(tmp_path).speaker_count_wait_sec == 180


@pytest.mark.parametrize("value", ["-1", "2.5", "true", "soon", "'60'"])
def test_a_bad_wait_is_rejected(tmp_path, value):
    (tmp_path / "config.yaml").write_text(f"speaker_count_wait_sec: {value}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="speaker_count_wait_sec"):
        load_config(tmp_path)


def test_the_wait_is_read_from_config_without_an_unknown_key_warning(tmp_path, caplog):
    (tmp_path / "config.yaml").write_text("speaker_count_wait_sec: 0\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert load_config(tmp_path).speaker_count_wait_sec == 0
    assert not [r for r in caplog.records if "speaker_count_wait_sec" in r.getMessage()]


def test_init_writes_the_setting_and_ignores_the_records(tmp_path):
    import argparse

    assert cli.cmd_init(argparse.Namespace(workspace=str(tmp_path), language="en",
                                           force=False)) == 0
    assert load_config(tmp_path).speaker_count_wait_sec == 180
    assert "speaker_count_wait_sec: 180" in (tmp_path / "config.yaml").read_text(encoding="utf-8")
    ignored = (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert headcount.HEADCOUNT_DIR_NAME + "/" in ignored


# --- where the summary learns the count, and whether it is an estimate -------

class _Summarizer:
    def __init__(self):
        self.prompts = []

    def summarize(self, prompt):
        self.prompts.append(prompt)
        return "# Title\n"


def _stub_engine(cfg, monkeypatch, summarizer, source=None, count=None):
    seen = {}

    def fake_run(audio, **kw):
        seen.update(kw)
        stem = os.path.splitext(os.path.basename(audio))[0]
        base = cfg.processing_dir / stem
        txt = Path(f"{base}_transcript_diarized.txt")
        txt.write_text("[  0.0s] SPEAKER_00: hello\n", encoding="utf-8")
        stats = Path(f"{base}_speakers.json")
        stats.write_text(json.dumps({"n_speakers": 3}), encoding="utf-8")
        out = {"txt_path": str(txt), "jsonl_path": f"{base}_none.jsonl", "stats_path": str(stats)}
        if source is not None:
            out.update(speaker_count_source=source, speaker_count=count)
        return out

    monkeypatch.setattr(pipeline, "get_hf_token", lambda *a, **k: "tok")
    monkeypatch.setattr(pipeline, "get_backend", lambda *a, **k: object())
    monkeypatch.setattr(pipeline, "run_diarization", fake_run)
    monkeypatch.setattr("moim_scribe.diarize_cloud.run_cloud_diarization", fake_run)
    monkeypatch.setattr("moim_scribe.stt.soniox_client.get_api_key", lambda *a, **k: "key")
    monkeypatch.setattr(pipeline, "get_summarizer", lambda *a, **k: summarizer)
    monkeypatch.setattr(pipeline, "_recording_time_block", lambda *a, **k: "")
    return seen


def _claimed(cfg, name):
    p = cfg.processing_dir / name
    p.write_bytes(b"x" * 100)
    return p


def _phrase(line: str) -> str:
    """The words of a count note, without its prefix and its number."""
    return line.split("{n}")[0].removeprefix("※ ").removeprefix("Note: ").rstrip(": ")


NOTE_KEYS = ("spk_fixed", "spk_user", "spk_spoken", "spk_default")
NOTES = {lang: {k: _phrase(pipeline.PROMPT_HEADERS[lang][k]) for k in NOTE_KEYS}
         for lang in ("en", "ko")}
# The start of each note line as printed, up to the number. The lens quotes
# the phrases too, so absence is checked on these, not on the phrases.
LINES = {lang: {k: pipeline.PROMPT_HEADERS[lang][k].split("{n}")[0] for k in NOTE_KEYS}
         for lang in ("en", "ko")}


def _new_cfg(tmp_path, lang="en"):
    cfg = load_config(tmp_path)
    ensure_workspace_dirs(cfg)
    cfg.language = lang
    return cfg


@pytest.mark.parametrize("backend", ["mlx", "soniox"])
def test_a_given_count_reaches_both_engines_and_is_not_an_estimate(tmp_path, monkeypatch, backend):
    cfg = _new_cfg(tmp_path)
    cfg.stt_backend = backend
    s = _Summarizer()
    seen = _stub_engine(cfg, monkeypatch, s, source="given", count=4)
    pipeline.process_recording(cfg, _claimed(cfg, "standup.m4a"), given_speakers=4)
    assert seen["num_speakers"] == 4
    (prompt,) = s.prompts
    assert pipeline.PROMPT_HEADERS["en"]["spk_user"].format(n=4) in prompt
    assert LINES["en"]["spk_fixed"] not in prompt


def test_the_file_name_wins_over_a_given_count(tmp_path, monkeypatch):
    cfg = _new_cfg(tmp_path)
    s = _Summarizer()
    seen = _stub_engine(cfg, monkeypatch, s, source="given", count=5)
    pipeline.process_recording(cfg, _claimed(cfg, "standup 5p.m4a"), given_speakers=2)
    assert seen["num_speakers"] == 5
    assert pipeline.PROMPT_HEADERS["en"]["spk_fixed"].format(n=5) in s.prompts[0]
    assert LINES["en"]["spk_user"] not in s.prompts[0]


@pytest.mark.parametrize("lang", ["en", "ko"])
@pytest.mark.parametrize("source, key", [("spoken", "spk_spoken"), ("default", "spk_default")])
def test_a_spoken_or_configured_count_is_not_called_an_estimate(tmp_path, monkeypatch,
                                                                lang, source, key):
    cfg = _new_cfg(tmp_path, lang)
    s = _Summarizer()
    _stub_engine(cfg, monkeypatch, s, source=source, count=3)
    pipeline.process_recording(cfg, _claimed(cfg, "standup.m4a"))
    prompt = s.prompts[0]
    assert pipeline.PROMPT_HEADERS[lang][key].format(n=3) in prompt
    others = [v for k, v in LINES[lang].items() if k != key]
    assert not any(o in prompt for o in others)


@pytest.mark.parametrize("source", ["estimated", None, "given"])
def test_an_estimated_count_gets_no_note(tmp_path, monkeypatch, source):
    """No note = the lens hedges the label count as an estimate. "given" with
    no count from either the name or the user cannot be trusted either."""
    cfg = _new_cfg(tmp_path)
    s = _Summarizer()
    _stub_engine(cfg, monkeypatch, s, source=source, count=None)
    pipeline.process_recording(cfg, _claimed(cfg, "standup.m4a"))
    assert not any(n in s.prompts[0] for n in LINES["en"].values())


def test_the_lens_names_every_note_the_pipeline_prints():
    """The lens decides "estimated" by the presence of these notes: a note the
    lens does not name would be hedged as an estimate."""
    lenses = Path(pipeline.__file__).parent / "templates" / "lenses"
    for lang in ("en", "ko"):
        lens = (lenses / f"summary.{lang}.md").read_text(encoding="utf-8")
        for key in NOTE_KEYS:
            assert NOTES[lang][key] in lens, (lang, key)


# --- the engines report where their count came from ------------------------

def _wire_local(monkeypatch, tmp_path, detected):
    monkeypatch.setattr(diarize, "prepare_audio", lambda inp, session: str(tmp_path / "a.wav"))
    monkeypatch.setattr(diarize, "load_diarization_pipeline", lambda token: object())
    monkeypatch.setattr(diarize.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(diarize, "transcribe_segments", lambda *a, **k: [])
    monkeypatch.setattr(diarize, "detect_num_speakers", lambda *a, **k: detected)
    monkeypatch.setattr(diarize, "diarize_turns", lambda *a, **k: [])
    monkeypatch.setattr(diarize, "identify_and_repair",
                        lambda audio, merged, raw, db, md, ident: (merged, {}, {}, {}))
    monkeypatch.setattr(diarize, "write_outputs",
                        lambda *a, **k: ("j", "t", "s", {"n_speakers": 0}))


@pytest.mark.parametrize("kwargs, detected, source, count", [
    ({"num_speakers": 4}, None, "given", 4),
    ({}, 3, "spoken", 3),
    ({"default_speakers": 2}, 3, "spoken", 3),
    ({"default_speakers": 2}, None, "default", 2),
    ({}, None, "estimated", None),
    ({"num_speakers": 0}, None, "estimated", None),
])
def test_run_diarization_reports_the_count_source(tmp_path, monkeypatch,
                                                  kwargs, detected, source, count):
    rec = tmp_path / "rec.m4a"
    rec.write_bytes(b"x" * 100)
    _wire_local(monkeypatch, tmp_path, detected)
    out = diarize.run_diarization(str(rec), stt_backend=object(), language="en",
                                  speaker_db=None, model_dir=tmp_path, hf_token="t", **kwargs)
    assert (out["speaker_count_source"], out["speaker_count"]) == (source, count)
