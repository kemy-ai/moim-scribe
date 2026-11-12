"""Fixes from the independent review of the speaker count wait (0.2.1).

Each test pins the right behaviour for one finding: a recording name the file
system cannot decode, an upgraded workspace (old summary lens, old
.gitignore), races around the claim, which recording a count without a name
is for, paths given to `count`, how often the waiting loops pass, and the
output of `waiting`. Time is never waited for.
"""
import logging
import os
import types
from pathlib import Path

import pytest

from moim_scribe import cli, headcount, pipeline
from moim_scribe.config import ensure_workspace_dirs, load_config
from moim_scribe.pipeline import scan_inbox

T0 = 1_000_000.0


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def _audio(path: Path, size: int = 20000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


@pytest.fixture
def ws(tmp_path, monkeypatch, real_count_wait):
    """A workspace for the pipeline functions, with the engine stubbed."""
    cfg = load_config(tmp_path)
    ensure_workspace_dirs(cfg)
    cfg.language = "en"
    monkeypatch.setattr(pipeline, "is_stable_audio", lambda p: True)
    calls = []

    def fake_process(cfg_, claimed, progress=None, **kw):
        calls.append((claimed.name, kw))
        return claimed

    monkeypatch.setattr(pipeline, "process_recording", fake_process)
    cfg._calls = calls
    return cfg


class FakeTime:
    def __init__(self):
        self.t = T0
        self.sleeps = 0
        self.on_sleep = None

    def clock(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps += 1
        if self.on_sleep:
            self.on_sleep(self.sleeps)
        self.t += seconds


@pytest.fixture
def cws(tmp_path, monkeypatch, real_count_wait):
    """A workspace made by `init`, for the commands, with fake time.

    is_stable_audio is the real rule on size (a file under 10 KB is never
    stable) without its read and pause, so a stub file stays in inbox/
    unscanned the way it does for real.
    """
    w = tmp_path / "ws"
    assert cli.main(["init", str(w), "--language", "en"]) == 0
    ft = FakeTime()
    monkeypatch.setattr(cli, "_clock", ft.clock)
    monkeypatch.setattr(cli, "_sleep", ft.sleep)
    monkeypatch.setattr(pipeline, "is_stable_audio", lambda p: p.stat().st_size >= 10_000)
    calls = []

    def fake_process(cfg, claimed, progress=None, **kw):
        calls.append((claimed.name, kw, ft.t))
        return claimed

    monkeypatch.setattr(pipeline, "process_recording", fake_process)
    monkeypatch.chdir(w)
    return types.SimpleNamespace(path=w, time=ft, calls=calls, cfg=lambda: load_config(w))


def _seen(cfg, name, when):
    headcount.mark_seen(cfg, name, when)


# --- F1: a name the file system could not decode ------------------------------

UNDECODABLE = "\udcc8\udcb8\udcc0\udcc7"      # a CP949 name read by Linux as UTF-8


def test_f1_record_key_never_raises():
    assert len(headcount.record_key(UNDECODABLE + ".m4a")) == 32
    assert headcount.record_key("\ud800.m4a") != headcount.record_key("\ud801.m4a")


def _list_with(monkeypatch, folder: Path, extra: Path):
    """Make `folder` list `extra` too, as a file (APFS and NTFS refuse such
    names, so the listing is simulated on every system)."""
    real_iterdir = Path.iterdir
    real_is_file = Path.is_file

    def iterdir(self):
        out = list(real_iterdir(self))
        if self == folder:
            out.append(extra)
        return iter(out)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    monkeypatch.setattr(Path, "is_file", lambda self: self == extra or real_is_file(self))


def test_f1_an_undecodable_name_does_not_stop_the_pass(ws, monkeypatch):
    good = _audio(ws.inbox_dir / "standup 3p.m4a")
    headcount.record_count(ws, good, 3)                     # the record folder exists
    _list_with(monkeypatch, ws.inbox_dir, ws.inbox_dir / f"{UNDECODABLE}.txt")
    assert scan_inbox(ws, clock=Clock())["processed"] == 1
    assert [c[0] for c in ws._calls] == ["standup 3p.m4a"]


def test_f1_an_undecodable_recording_is_processed_like_0_2_0(ws, monkeypatch, caplog):
    bad = ws.inbox_dir / f"{UNDECODABLE}.m4a"         # no count: the gate looks at it
    _list_with(monkeypatch, ws.inbox_dir, bad)
    monkeypatch.setattr(pipeline, "classify_audio", lambda p: True)
    monkeypatch.setattr(pipeline, "name_fits", lambda folder, stem: True)
    monkeypatch.setattr(pipeline.shutil, "move", lambda src, dst: None)
    with caplog.at_level(logging.INFO):
        result = scan_inbox(ws, clock=Clock())
    assert result["processed"] == 1 and "waiting" not in result
    assert [c[0] for c in ws._calls] == [bad.name]
    said = [r.getMessage() for r in caplog.records]
    assert any("bytes that are not UTF-8" in m for m in said)
    assert not any("cannot check its speaker count wait" in m for m in said)   # not by accident


def test_f1_a_count_can_be_recorded_for_an_undecodable_recording(ws, monkeypatch):
    bad = ws.inbox_dir / f"{UNDECODABLE}.m4a"
    _list_with(monkeypatch, ws.inbox_dir, bad)
    monkeypatch.setattr(pipeline, "classify_audio", lambda p: True)
    done = headcount.record_count(ws, bad, 3)
    assert headcount.given_count(ws, done.name) == 3


def test_f1_a_real_undecodable_file_name(ws):
    """Where the file system takes the bytes (Linux), with no simulation."""
    raw = os.fsencode(str(ws.inbox_dir)) + b"/\xc8\xb8\xc0\xc7.m4a"
    try:
        with open(raw, "wb") as fh:
            fh.write(b"x" * 20000)
    except (OSError, ValueError):
        pytest.skip("this file system refuses names that are not UTF-8")
    other = _audio(ws.inbox_dir / "standup.m4a")
    headcount.record_count(ws, other, 2)
    result = scan_inbox(ws, clock=Clock())
    assert result["processed"] == 2 and "waiting" not in result


def test_f1_a_failing_gate_processes_the_recording_now(ws, monkeypatch, caplog):
    _audio(ws.inbox_dir / "standup.m4a")

    def broken(cfg, f, *, wait, now):
        raise UnicodeEncodeError("utf-8", "x", 0, 1, "surrogates not allowed")

    monkeypatch.setattr(pipeline, "_count_gate", broken)
    with caplog.at_level(logging.WARNING):
        assert scan_inbox(ws, clock=Clock())["processed"] == 1
    assert any("cannot check its speaker count wait" in r.getMessage() for r in caplog.records)


def test_f1_a_failing_prune_does_not_stop_the_pass(ws, monkeypatch):
    rec = _audio(ws.inbox_dir / "standup 3p.m4a")
    headcount.record_count(ws, rec, 3)

    def broken(*a, **k):
        raise ValueError("boom")

    monkeypatch.setattr(headcount, "prune", broken)
    assert scan_inbox(ws, clock=Clock())["processed"] == 1


def test_f1_an_undecodable_recording_is_not_listed_as_waiting(cws, monkeypatch, capsys):
    cfg = cws.cfg()
    _list_with(monkeypatch, cfg.inbox_dir, cfg.inbox_dir / f"{UNDECODABLE}.m4a")
    monkeypatch.setattr(pipeline, "classify_audio", lambda p: True)
    assert cli.main(["waiting"]) == 0
    assert capsys.readouterr().out == ""


def test_f1_display_name_escapes_undecodable_bytes():
    assert headcount.display_name(f"{UNDECODABLE}.m4a") == "\\udcc8\\udcb8\\udcc0\\udcc7.m4a"


# --- F2: an old summary lens still takes a given count as given ---------------

# What the summary lens of 0.2.0 quotes. Workspaces made by 0.2.0 keep their
# copy of that lens, which treats a count as estimated unless a line with
# these words is present.
OLD_LENS_QUOTE = {"ko": "파일명 지정 참석 인원", "en": "attendee count fixed by filename"}


@pytest.mark.parametrize("lang", ["ko", "en"])
@pytest.mark.parametrize("key", ["spk_user", "spk_spoken", "spk_default"])
def test_f2_every_count_line_names_the_line_the_old_lens_asks_for(lang, key):
    line = pipeline.PROMPT_HEADERS[lang][key].format(n=4)
    assert f'"{OLD_LENS_QUOTE[lang]}"' in line
    # It is a reference, not a filename note itself.
    assert pipeline.PROMPT_HEADERS[lang]["spk_fixed"].split("{n}")[0] not in line


@pytest.mark.parametrize("lang", ["ko", "en"])
def test_f2_the_new_lens_still_quotes_the_old_phrase(lang):
    from moim_scribe.pipeline import _shipped_lens

    assert OLD_LENS_QUOTE[lang] in _shipped_lens("summary", lang)


# --- F3: a count that arrives between the gate and the claim ------------------

def test_f3_a_count_given_just_before_the_claim_is_used(ws, monkeypatch):
    rec = _audio(ws.inbox_dir / "standup.m4a")
    clock = Clock()
    scan_inbox(ws, clock=clock)
    clock.t += 181
    real_move = pipeline.shutil.move

    def move(src, dst):
        if Path(src) == rec and rec.exists():
            headcount.record_count(ws, rec, 4)
        return real_move(src, dst)

    monkeypatch.setattr(pipeline.shutil, "move", move)
    scan_inbox(ws, clock=clock)
    assert ws._calls == [("standup.m4a", {"summarize": True, "given_speakers": 4})]


# --- F4: a path that differs only in case ------------------------------------

def test_f4_a_case_variant_path_is_recorded_under_the_listed_name(ws):
    rec = _audio(ws.inbox_dir / "standup.m4a")
    variant = ws.inbox_dir / "STANDUP.M4A"
    if not variant.is_file():
        pytest.skip("case-sensitive file system")
    got = headcount.record_count(ws, variant, 4)
    assert got.name == "standup.m4a"
    assert headcount.given_count(ws, headcount.recording_name(ws, rec)) == 4
    scan_inbox(ws, clock=Clock())
    assert ws._calls == [("standup.m4a", {"summarize": True, "given_speakers": 4})]


def test_f4_record_count_uses_the_listed_entry_for_the_same_file(ws, monkeypatch):
    """The same rule, shown on any file system: a path that is the same file
    as a listed recording is recorded under the listed name."""
    rec = _audio(ws.inbox_dir / "standup.m4a")
    other = ws.inbox_dir / "Standup-alias.m4a"
    other.write_bytes(b"y")
    monkeypatch.setattr(headcount, "inbox_recordings", lambda cfg: [rec])
    monkeypatch.setattr(headcount.os.path, "samefile", lambda a, b: True)
    assert headcount.record_count(ws, other, 4).name == "standup.m4a"
    assert headcount.given_count(ws, "standup.m4a") == 4


# --- F6: the record folder keeps itself out of git ----------------------------

def test_f6_the_record_folder_ignores_itself(ws):
    rec = _audio(ws.inbox_dir / "standup.m4a")
    headcount.record_count(ws, rec, 4)
    d = headcount.headcount_dir(ws)
    assert (d / ".gitignore").read_text(encoding="utf-8") == "*\n"
    headcount.prune(ws, set(), listed_at=T0 + 10_000_000_000, now=T0 + 10_000_000_001)
    assert (d / ".gitignore").exists()
    assert not [p for p in d.iterdir() if p.name != ".gitignore"]


def test_f6_no_record_no_folder(ws):
    _audio(ws.inbox_dir / "standup 3p.m4a")
    scan_inbox(ws, clock=Clock())
    assert not headcount.headcount_dir(ws).exists()


# --- F7 and F15: when the waiting loops pass again ---------------------------

def test_f7_tracker_is_due_when_a_count_arrives(ws):
    rec = _audio(ws.inbox_dir / "standup.m4a")
    scan_inbox(ws, clock=Clock())
    tracker = headcount.WaitTracker(ws, rescan_sec=60)
    tracker.after_pass(T0)
    assert tracker.due(T0 + 2) is False
    headcount.record_count(ws, rec, 4)
    assert tracker.due(T0 + 4) is True


def test_f7_tracker_is_due_when_a_wait_runs_out(ws):
    _audio(ws.inbox_dir / "standup.m4a")
    scan_inbox(ws, clock=Clock())
    tracker = headcount.WaitTracker(ws, rescan_sec=600)
    tracker.after_pass(T0 + 178)
    assert tracker.due(T0 + 179) is False
    assert tracker.due(T0 + 180) is True


def test_f15_an_expired_recording_that_stays_triggers_no_pass(ws):
    _audio(ws.inbox_dir / "stuck.m4a")
    _audio(ws.inbox_dir / "meeting.m4a")
    _seen(ws, "stuck.m4a", T0 - 1000)
    _seen(ws, "meeting.m4a", T0)
    tracker = headcount.WaitTracker(ws, rescan_sec=30)
    tracker.after_pass(T0 + 1)
    assert [tracker.due(T0 + 1 + k) for k in (2, 10, 28)] == [False, False, False]
    assert tracker.due(T0 + 31) is True                       # the slow rescan
    tracker.after_pass(T0 + 31)
    assert tracker.due(T0 + 33) is False


def test_f15_scan_inbox_does_not_pass_every_poll_for_an_unclaimable_recording(
        cws, monkeypatch, capsys):
    cfg = cws.cfg()
    _audio(cfg.inbox_dir / "stuck.m4a")
    _audio(cfg.inbox_dir / "meeting.m4a")
    _seen(cfg, "stuck.m4a", T0 - 1000)
    real_move = pipeline.shutil.move

    def move(src, dst):
        if str(src).endswith("stuck.m4a"):
            raise OSError("busy")
        return real_move(src, dst)

    monkeypatch.setattr(pipeline.shutil, "move", move)
    passes = []
    real_scan = pipeline.scan_inbox

    def counting(*a, **k):
        passes.append(cws.time.t)
        if len(passes) > 200:                 # fail, rather than hang, on a pass loop
            raise AssertionError(f"{len(passes)} passes without time passing")
        return real_scan(*a, **k)

    monkeypatch.setattr(pipeline, "scan_inbox", counting)
    assert cli.main(["scan-inbox"]) == 0
    assert [c[0] for c in cws.calls] == ["meeting.m4a"]
    assert 180 <= cws.calls[0][2] - T0 < 184
    # One pass at the start, one every WAIT_RESCAN_SEC, one at expiry: not 91.
    assert len(passes) <= 2 + int(180 // cli.WAIT_RESCAN_SEC)


# --- F10: the gate says things once per recording -----------------------------

def test_f10_an_unclaimable_recording_logs_its_count_once(ws, monkeypatch, caplog):
    rec = _audio(ws.inbox_dir / "standup.m4a")
    headcount.record_count(ws, rec, 4)

    def move(src, dst):
        raise OSError("busy")

    monkeypatch.setattr(pipeline.shutil, "move", move)
    with caplog.at_level(logging.INFO):
        for k in range(3):
            scan_inbox(ws, clock=Clock(T0 + k))
    assert sum("given by the user" in r.getMessage() for r in caplog.records) == 1


def test_f10_an_unclaimable_expired_recording_logs_the_estimate_once(ws, monkeypatch, caplog):
    _audio(ws.inbox_dir / "standup.m4a")
    _seen(ws, "standup.m4a", T0 - 1000)
    monkeypatch.setattr(pipeline.shutil, "move", lambda s, d: (_ for _ in ()).throw(OSError("x")))
    with caplog.at_level(logging.INFO):
        for k in range(3):
            scan_inbox(ws, clock=Clock(T0 + k))
    assert sum("estimating it" in r.getMessage() for r in caplog.records) == 1


# --- F12: process_recording checks the given count ----------------------------

class _BoomError(Exception):
    pass


class _Boom:
    @staticmethod
    def now(*a, **k):
        raise _BoomError


@pytest.mark.parametrize("bad", [0, 31, -1, True])
def test_f12_a_given_count_out_of_range_is_ignored(tmp_path, monkeypatch, caplog, bad):
    cfg = load_config(tmp_path)
    ensure_workspace_dirs(cfg)
    claimed = _audio(cfg.processing_dir / "standup.m4a")
    # Stops at the clock read that follows the check: far enough to see it.
    monkeypatch.setattr(pipeline, "datetime", _Boom)
    with caplog.at_level(logging.WARNING), pytest.raises(_BoomError):
        pipeline.process_recording(cfg, claimed, given_speakers=bad)
    assert any("ignoring the given speaker count" in r.getMessage() for r in caplog.records)


def test_f12_a_given_count_in_range_is_not_warned_about(tmp_path, monkeypatch, caplog):
    cfg = load_config(tmp_path)
    ensure_workspace_dirs(cfg)
    claimed = _audio(cfg.processing_dir / "standup.m4a")
    monkeypatch.setattr(pipeline, "datetime", _Boom)
    with caplog.at_level(logging.WARNING), pytest.raises(_BoomError):
        pipeline.process_recording(cfg, claimed, given_speakers=4)
    assert not any("ignoring the given speaker count" in r.getMessage() for r in caplog.records)


# --- F13: a count without a name ----------------------------------------------

def test_f13_a_stub_and_a_real_recording_are_both_candidates_so_nothing_is_recorded(cws, capsys):
    cfg = cws.cfg()
    _audio(cfg.inbox_dir / "zz-stub.m4a", size=10)            # never stable
    _audio(cfg.inbox_dir / "meeting.m4a")
    _seen(cfg, "meeting.m4a", T0 - 5)
    assert cli.main(["count", "3"]) == 1
    err = capsys.readouterr().err
    assert "  zz-stub.m4a\tnot scanned yet" in err and "  meeting.m4a\t2:55 left" in err
    assert headcount.given_count(cfg, "meeting.m4a") is None
    assert headcount.given_count(cfg, "zz-stub.m4a") is None


def test_f13_waiting_shows_a_never_scanned_file_as_such(cws, capsys):
    cfg = cws.cfg()
    _audio(cfg.inbox_dir / "zz-stub.m4a", size=10)
    _audio(cfg.inbox_dir / "meeting.m4a")
    _seen(cfg, "meeting.m4a", T0 - 5)
    cli.main(["waiting"])
    out = capsys.readouterr().out
    assert "zz-stub.m4a\tnot scanned yet\n" in out
    assert "3:00 left" not in out


def test_f13_a_second_count_without_a_name_does_not_land_on_another_recording(cws, capsys):
    cfg = cws.cfg()
    _audio(cfg.inbox_dir / "a.m4a")
    _audio(cfg.inbox_dir / "b.m4a")
    _seen(cfg, "a.m4a", T0 - 50)
    _seen(cfg, "b.m4a", T0 - 10)
    assert cli.main(["count", "3", "b"]) == 0
    capsys.readouterr()
    # Meant to correct b; only a is waiting now, so it goes to a, and says
    # that b already has a count and how to change it.
    assert cli.main(["count", "4"]) == 0
    out = capsys.readouterr().out
    assert "recorded 4 speakers for a.m4a" in out
    assert "note: b.m4a already has 3 speakers recorded" in out
    assert "moim-scribe count <N> <part of its name>" in out


def test_f13_with_nothing_waiting_it_says_how_to_change_a_given_count(cws, capsys):
    cfg = cws.cfg()
    rec = _audio(cfg.inbox_dir / "a.m4a")
    headcount.record_count(cfg, rec, 3)
    assert cli.main(["count", "4"]) == 1
    err = capsys.readouterr().err
    assert "no recording is waiting" in err and "to give or change the count" in err
    assert headcount.given_count(cfg, "a.m4a") == 3


def test_f13_the_list_names_the_workspace_when_it_is_elsewhere(cws, capsys, monkeypatch, tmp_path):
    cfg = cws.cfg()
    _audio(cfg.inbox_dir / "a.m4a")
    _audio(cfg.inbox_dir / "b.m4a")
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)
    assert cli.main(["count", "3", "--workspace", str(cws.path)]) == 1
    assert f'--workspace "{cfg.workspace}"' in capsys.readouterr().err


def test_f13_one_candidate_with_scan_inbox_waiting_gets_the_count(cws, capsys):
    """The case of the review: a stub never stable, one real recording. Named,
    the count reaches the real one and it starts at once."""
    cfg = cws.cfg()
    _audio(cfg.inbox_dir / "zz-stub.m4a", size=10)
    _audio(cfg.inbox_dir / "meeting.m4a")

    def give(n):
        if n == 1:
            assert cli.main(["count", "3", "meeting"]) == 0

    cws.time.on_sleep = give
    assert cli.main(["scan-inbox"]) == 0
    assert [(c[0], c[1].get("given_speakers")) for c in cws.calls] == [("meeting.m4a", 3)]
    assert cws.calls[0][2] - T0 < 8


# --- F14: a path given to count ------------------------------------------------

def test_f14_a_relative_path_names_the_recording(cws, capsys):
    cfg = cws.cfg()
    _audio(cfg.inbox_dir / "meeting.m4a")
    assert cli.main(["count", "3", os.path.join("inbox", "meeting.m4a")]) == 0
    assert headcount.given_count(cfg, "meeting.m4a") == 3


def test_f14_an_absolute_path_names_the_recording(cws, capsys):
    cfg = cws.cfg()
    rec = _audio(cfg.inbox_dir / "meeting.m4a")
    assert cli.main(["count", "3", str(rec)]) == 0
    assert headcount.given_count(cfg, "meeting.m4a") == 3


def test_f14_a_path_relative_to_the_workspace_works_from_elsewhere(cws, capsys, monkeypatch,
                                                                   tmp_path):
    cfg = cws.cfg()
    _audio(cfg.inbox_dir / "team" / "standup.m4a")
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)
    arg = os.path.join("inbox", "team", "standup.m4a")
    assert cli.main(["count", "3", arg, "--workspace", str(cws.path)]) == 0
    assert headcount.given_count(cfg, "team/standup.m4a") == 3


def test_f14_a_path_outside_inbox_is_refused(cws, capsys, tmp_path):
    cfg = cws.cfg()
    _audio(cfg.inbox_dir / "meeting.m4a")
    outside = _audio(tmp_path / "meeting.m4a")
    assert cli.main(["count", "3", str(outside)]) == 1
    err = capsys.readouterr().err
    assert "is not a recording in" in err and "already started" not in err
    assert headcount.given_count(cfg, "meeting.m4a") is None


def test_f14_the_already_started_hint_only_for_a_file_that_left_inbox(cws, capsys):
    cfg = cws.cfg()
    _audio(cfg.done_dir / "meeting.m4a")
    _audio(cfg.done_dir / "standup (2).m4a")
    _audio(cfg.done_dir / "weekly book club.m4a")
    for q in ("meeting", "meeting.m4a", "MEETING", "standup"):
        assert cli.main(["count", "3", q]) == 1
        assert "already started" in capsys.readouterr().err, q
    # A part of an older name is not that recording (G7).
    for q in ("nothing like it", "book club", "meet"):
        assert cli.main(["count", "3", q]) == 1
        assert "already started" not in capsys.readouterr().err, q


# --- F16 and F18: what scan-inbox says when it stops ----------------------------

def test_f16_ctrl_c_does_not_claim_the_recording_is_back_unconditionally(cws, capsys, monkeypatch,
                                                                         caplog):
    cfg = cws.cfg()
    _audio(cfg.inbox_dir / "meeting 3p.m4a")

    def boom(cfg_, claimed, progress=None, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(pipeline, "process_recording", boom)
    monkeypatch.setattr(pipeline, "_put_back", lambda c, f: None)
    with caplog.at_level(logging.ERROR):
        assert cli.main(["scan-inbox"]) == 130
    assert any("could not be put back" in r.getMessage() for r in caplog.records)
    err = capsys.readouterr().err
    assert "unless a message above says otherwise" in err


def _hold_lock(workspace: Path) -> None:
    """Hold the pipeline lock as another live process would (our parent)."""
    d = workspace / ".moim-scribe.lock"
    d.mkdir()
    (d / "pid").write_text(str(os.getppid()))


def test_f18_a_scan_that_finds_another_run_says_so(cws, capsys):
    cfg = cws.cfg()
    _audio(cfg.inbox_dir / "meeting.m4a")
    _hold_lock(cfg.workspace)
    assert cli.main(["scan-inbox"]) == 0
    assert "another moim-scribe run" in capsys.readouterr().err


def test_f18_another_run_taking_over_mid_wait_is_explained(cws, capsys):
    cfg = cws.cfg()
    _audio(cfg.inbox_dir / "meeting.m4a")

    def take_lock(n):
        if n == 3:
            _hold_lock(cfg.workspace)

    cws.time.on_sleep = take_lock
    assert cli.main(["scan-inbox"]) == 0
    err = capsys.readouterr().err
    assert "another moim-scribe run" in err and "leaves the recordings to it" in err
    assert cws.time.t - T0 < 60


def test_f18_scan_inbox_reports_busy_only_when_the_lock_is_taken(ws):
    _audio(ws.inbox_dir / "standup 3p.m4a")
    assert "busy" not in scan_inbox(ws, clock=Clock())
    _audio(ws.inbox_dir / "standup 4p.m4a")
    _hold_lock(ws.workspace)
    assert scan_inbox(ws, clock=Clock()) == {
        "processed": 0, "failed": 0, "skipped": 0, "busy": True}


# --- F17: one line, one tab per recording ---------------------------------------

def test_f17_control_characters_are_escaped_in_waiting(cws, capsys):
    cfg = cws.cfg()
    try:
        _audio(cfg.inbox_dir / "a\tb.m4a")
        _audio(cfg.inbox_dir / "c\nd.m4a")
    except OSError:
        pytest.skip("this file system refuses such names")
    assert cli.main(["waiting"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert sorted(lines) == ["a\\tb.m4a\tnot scanned yet", "c\\nd.m4a\tnot scanned yet"]
    assert all(line.count("\t") == 1 for line in lines)


def test_f17_display_name():
    assert headcount.display_name("a\tb\nc\rd\x01e\u2028f\u202eg") == (
        "a\\tb\\nc\\rd\\x01e\\u2028f\\u202eg")
    assert headcount.display_name("회의 녹음.m4a") == "회의 녹음.m4a"


def test_f17_a_recording_with_a_tab_is_still_found_by_part_of_its_name(cws, capsys):
    cfg = cws.cfg()
    try:
        _audio(cfg.inbox_dir / "team\tstandup.m4a")
    except OSError:
        pytest.skip("this file system refuses such names")
    assert cli.main(["count", "3", "standup"]) == 0
    assert "recorded 3 speakers for team\\tstandup.m4a" in capsys.readouterr().out
    assert headcount.given_count(cfg, "team\tstandup.m4a") == 3


# --- F19: only ASCII digits ----------------------------------------------------

@pytest.mark.parametrize("bad", ["+3", "1_0", "３", "٣", " ", "3 4"])
def test_f19_only_ascii_digits_are_a_count(cws, capsys, bad):
    cfg = cws.cfg()
    _audio(cfg.inbox_dir / "standup.m4a")
    assert cli.main(["count", bad, "standup"]) == 1
    assert "whole number from 1 to 30" in capsys.readouterr().err
    assert headcount.given_count(cfg, "standup.m4a") is None


# --- net: files the scan skips, and claims that keep failing ----------------------

def test_net_a_stub_does_not_hold_scan_inbox_and_the_real_one_is_processed(cws, capsys):
    cfg = cws.cfg()
    _audio(cfg.inbox_dir / "zz-stub.m4a", size=10)
    _audio(cfg.inbox_dir / "meeting.m4a")
    assert cli.main(["scan-inbox"]) == 0
    assert [c[0] for c in cws.calls] == ["meeting.m4a"]
    assert 180 <= cws.calls[0][2] - T0 < 184
    assert (cfg.inbox_dir / "zz-stub.m4a").exists()
    assert cws.time.t - T0 < 190                     # returned once nothing waited
    err = capsys.readouterr().err
    assert "waiting for the speaker count: meeting.m4a" in err
    assert "zz-stub" not in err                      # never held back, so not announced


def test_net_a_claim_that_keeps_failing_ends_scan_inbox(cws, capsys, monkeypatch):
    cfg = cws.cfg()
    _audio(cfg.inbox_dir / "meeting.m4a")
    monkeypatch.setattr(pipeline.shutil, "move", lambda s, d: (_ for _ in ()).throw(OSError("x")))
    assert cli.main(["scan-inbox"]) == 0
    assert cws.calls == []
    assert cws.time.t - T0 < 190
    assert (cfg.inbox_dir / "meeting.m4a").exists()


# --- G1 (tracker and scan-inbox): changes during a pass --------------------------

def test_g1_after_pass_says_pass_again_for_a_count_given_during_the_pass(ws):
    a = _audio(ws.inbox_dir / "a.m4a")
    _seen(ws, "a.m4a", T0)
    tracker = headcount.WaitTracker(ws, rescan_sec=60)
    tracker.before_pass(T0 + 1)
    headcount.record_count(ws, a, 4)                  # during the pass
    assert tracker.after_pass(T0 + 100) is True
    # The next pass could not claim it: no further immediate pass.
    tracker.before_pass(T0 + 100)
    assert tracker.after_pass(T0 + 101) is False


def test_g1_after_pass_says_pass_again_for_a_wait_that_ran_out_during_the_pass(ws):
    _audio(ws.inbox_dir / "a.m4a")
    _seen(ws, "a.m4a", T0)
    tracker = headcount.WaitTracker(ws, rescan_sec=60)
    tracker.before_pass(T0 + 1)
    assert tracker.after_pass(T0 + 200) is True
    tracker.before_pass(T0 + 200)                     # expired already when it began
    assert tracker.after_pass(T0 + 201) is False


def test_g1_after_pass_is_quiet_when_nothing_changed(ws):
    _audio(ws.inbox_dir / "a.m4a")
    _seen(ws, "a.m4a", T0)
    tracker = headcount.WaitTracker(ws, rescan_sec=60)
    tracker.before_pass(T0 + 1)
    assert tracker.after_pass(T0 + 5) is False


def test_g1_scan_inbox_starts_a_recording_whose_count_came_during_a_long_pass(cws, monkeypatch):
    cfg = cws.cfg()
    a = _audio(cfg.inbox_dir / "a.m4a")
    _audio(cfg.inbox_dir / "b 3p.m4a")
    end = {}

    def fake_process(cfg_, claimed, progress=None, **kw):
        cws.calls.append((claimed.name, kw, cws.time.t))
        if claimed.name == "b 3p.m4a":
            cws.time.t += 50
            headcount.record_count(cfg, a, 4)
            cws.time.t += 150
            end["t"] = cws.time.t
        return claimed

    monkeypatch.setattr(pipeline, "process_recording", fake_process)
    assert cli.main(["scan-inbox"]) == 0
    assert [(c[0], c[1].get("given_speakers")) for c in cws.calls] == [
        ("b 3p.m4a", None), ("a.m4a", 4)]
    assert cws.calls[1][2] == end["t"]                # at once, not after a 30 s rescan


# --- G2: the cheap look never runs ffprobe, and says things once ---------------------

def _fake_ffprobe(monkeypatch, audio_names):
    runs = []

    def run(cmd, **kw):
        runs.append(cmd[-1])
        out = "audio\n" if Path(cmd[-1]).name in audio_names else ""
        return types.SimpleNamespace(returncode=0, stdout=out, stderr="")

    monkeypatch.setattr(pipeline.subprocess, "run", run)
    return runs


def test_g2_waiting_loop_looks_do_not_probe_and_the_log_is_said_once(ws, monkeypatch, caplog):
    _audio(ws.inbox_dir / "voice memo")               # audio with no extension
    _audio(ws.inbox_dir / "standup.m4a")
    (ws.inbox_dir / "notes.txt").write_text("hello " * 100, encoding="utf-8")
    runs = _fake_ffprobe(monkeypatch, {"voice memo"})
    tracker = headcount.WaitTracker(ws, rescan_sec=60)
    with caplog.at_level(logging.INFO):
        tracker.before_pass(T0)
        assert scan_inbox(ws, clock=Clock(T0))["waiting"] == 2
        tracker.after_pass(T0)
        probes_in_pass = len(runs)
        for i in range(1, 16):                        # 30 s of polling
            assert tracker.due(T0 + 2 * i) is False
        scan_inbox(ws, clock=Clock(T0 + 31))          # the next pass
    assert probes_in_pass == 2                         # voice memo and notes.txt, once each
    assert len(runs) == probes_in_pass + 1             # the next pass probes notes.txt only
    said = [r.getMessage() for r in caplog.records if "no usable extension" in r.getMessage()]
    assert len(said) == 1


def test_g2_the_cheap_look_still_lists_an_extensionless_recording_the_pass_found(ws, monkeypatch):
    _audio(ws.inbox_dir / "voice memo")
    _fake_ffprobe(monkeypatch, {"voice memo"})
    scan_inbox(ws, clock=Clock(T0))
    names = [w.name for w in headcount.waiting_recordings(ws, T0 + 1, probe=False)]
    assert names == ["voice memo"]


def test_g2_a_changed_file_is_probed_again(ws, monkeypatch):
    f = _audio(ws.inbox_dir / "voice memo")
    runs = _fake_ffprobe(monkeypatch, {"voice memo"})
    assert pipeline.classify_audio(f) and pipeline.classify_audio(f)
    assert len(runs) == 1
    f.write_bytes(b"y" * 30000)
    assert pipeline.classify_audio(f)
    assert len(runs) == 2


def test_g2_a_file_that_was_not_audio_is_probed_again(ws, monkeypatch):
    """A cloud placeholder fails the probe and becomes audio later, possibly
    with the same size and time: a "no" is never remembered."""
    f = _audio(ws.inbox_dir / "voice memo")
    runs = _fake_ffprobe(monkeypatch, set())
    assert not pipeline.classify_audio(f)
    assert not pipeline.probed_audio(f)
    _fake_ffprobe(monkeypatch, {"voice memo"})
    assert pipeline.classify_audio(f)
    assert len(runs) == 1


# --- G3: a name with a folder, in part --------------------------------------------

@pytest.mark.parametrize("query", ["team/meeting", "team/meet", "TEAM/Meeting.m4a",
                                   os.path.join("team", "meet")])
def test_g3_a_name_in_a_subfolder_is_found_in_part(ws, query):
    rec = _audio(ws.inbox_dir / "team" / "meeting.m4a")
    _audio(ws.inbox_dir / "other" / "meeting.m4a")
    assert headcount.find_recording(ws, query) == rec


def test_g3_count_takes_what_waiting_shows_without_the_extension(cws, capsys):
    cfg = cws.cfg()
    _audio(cfg.inbox_dir / "team" / "meeting.m4a")
    assert cli.main(["count", "3", "team/meeting"]) == 0
    assert headcount.given_count(cfg, "team/meeting.m4a") == 3


def test_g3_a_path_outside_inbox_with_no_name_match_is_still_refused(cws, capsys, tmp_path):
    cfg = cws.cfg()
    _audio(cfg.inbox_dir / "standup.m4a")
    outside = _audio(tmp_path / "elsewhere" / "talk.m4a")
    assert cli.main(["count", "3", str(outside)]) == 1
    assert "is not a recording in" in capsys.readouterr().err


# --- G5: a count in the file name wins, and the log says so ---------------------------

def test_g5_a_user_count_is_not_passed_or_logged_when_the_file_name_has_one(ws, caplog):
    rec = _audio(ws.inbox_dir / "standup 3p.m4a")
    headcount.record_count(ws, rec, 5)
    with caplog.at_level(logging.INFO):
        scan_inbox(ws, clock=Clock())
    assert ws._calls == [("standup 3p.m4a", {"summarize": True})]
    assert not any("given by the user" in r.getMessage() for r in caplog.records)


# --- G6: a relative path is tried against the current folder and the workspace --------

def test_g6_a_same_path_in_the_current_folder_does_not_hide_the_workspace_one(ws, tmp_path,
                                                                              monkeypatch):
    rec = _audio(ws.inbox_dir / "standup.m4a")
    other = tmp_path / "otherws"
    _audio(other / "inbox" / "standup.m4a")
    monkeypatch.chdir(other)
    assert headcount.find_recording(ws, os.path.join("inbox", "standup.m4a")) == rec

