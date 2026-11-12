"""`count`, `waiting` and the wait inside `scan-inbox` (0.2.1).

Time is never waited for: the waiting loop takes its clock and sleep from
cli._clock / cli._sleep, which these tests replace with a fake whose sleep
advances the clock (and may do something while "time passes").
"""
import unicodedata
from pathlib import Path

import pytest

from moim_scribe import cli, headcount
from moim_scribe.config import load_config

T0 = 1_000_000.0


class FakeTime:
    def __init__(self):
        self.t = T0
        self.sleeps = 0
        self.on_sleep = None        # callable(n_sleeps) run before time advances

    def clock(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps += 1
        if self.on_sleep:
            self.on_sleep(self.sleeps)
        self.t += seconds


class _Workspace(type(Path())):
    """A workspace folder that also carries the fake time and the process calls."""


@pytest.fixture
def ws(tmp_path, monkeypatch, real_count_wait):
    w = _Workspace(tmp_path / "ws")
    assert cli.main(["init", str(w), "--language", "en"]) == 0
    ft = FakeTime()
    monkeypatch.setattr(cli, "_clock", ft.clock)
    monkeypatch.setattr(cli, "_sleep", ft.sleep)
    monkeypatch.setattr("moim_scribe.pipeline.is_stable_audio", lambda p: True)
    calls = []
    times = {}

    def fake_process(cfg, claimed, progress=None, **kw):
        calls.append((claimed.name, kw))
        times[claimed.name] = ft.t
        return claimed

    monkeypatch.setattr("moim_scribe.pipeline.process_recording", fake_process)
    w.calls = calls
    w.call_times = times
    w.time = ft
    return w


def _audio(ws, name):
    p = ws / "inbox" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * 20000)
    return p


def _seen(ws, name, when):
    """Make the scan see `name` at `when` (as watch would have)."""
    cfg = load_config(ws)
    headcount.mark_seen(cfg, headcount.recording_name(cfg, ws / "inbox" / name), when)


# --- count -------------------------------------------------------------------

def test_count_without_a_name_records_for_the_one_waiting_recording(ws, capsys):
    _audio(ws, "standup.m4a")
    _seen(ws, "standup.m4a", T0 - 10)
    assert cli.main(["count", "4", "--workspace", str(ws)]) == 0
    assert "recorded 4 speakers for standup.m4a" in capsys.readouterr().out
    assert headcount.given_count(load_config(ws), "standup.m4a") == 4


def test_count_without_a_name_and_two_waiting_records_nothing_and_lists_them(ws, capsys):
    _audio(ws, "older.m4a")
    _audio(ws, "newer.m4a")
    _seen(ws, "older.m4a", T0 - 100)
    _seen(ws, "newer.m4a", T0 - 10)
    assert cli.main(["count", "4", "--workspace", str(ws)]) == 1
    cap = capsys.readouterr()
    assert cap.out == ""
    assert "2 recordings are waiting" in cap.err and "not recorded" in cap.err
    assert "  older.m4a\t1:20 left" in cap.err and "  newer.m4a\t2:50 left" in cap.err
    assert "moim-scribe count 4 <part of its name>" in cap.err
    cfg = load_config(ws)
    assert headcount.given_count(cfg, "newer.m4a") is None
    assert headcount.given_count(cfg, "older.m4a") is None


def test_count_names_a_recording_by_part_of_its_name(ws, capsys):
    _audio(ws, "Team Standup.m4a")
    _audio(ws, "book club.m4a")
    assert cli.main(["count", "5", "STANDUP", "--workspace", str(ws)]) == 0
    assert "recorded 5 speakers for Team Standup.m4a" in capsys.readouterr().out
    assert headcount.given_count(load_config(ws), "Team Standup.m4a") == 5


def test_count_with_an_ambiguous_name_lists_candidates_and_fails(ws, capsys):
    _audio(ws, "club one.m4a")
    _audio(ws, "club two.m4a")
    assert cli.main(["count", "4", "club", "--workspace", str(ws)]) == 1
    cap = capsys.readouterr()
    assert cap.out == ""
    assert "club one.m4a" in cap.err and "club two.m4a" in cap.err
    cfg = load_config(ws)
    assert headcount.given_count(cfg, "club one.m4a") is None
    assert headcount.given_count(cfg, "club two.m4a") is None


def test_count_with_an_unknown_name_says_so_and_fails(ws, capsys):
    _audio(ws, "club one.m4a")
    assert cli.main(["count", "4", "nothing like it", "--workspace", str(ws)]) == 1
    err = capsys.readouterr().err
    assert "no recording" in err
    assert "already started" not in err      # nothing of that name has left inbox/


def test_count_for_a_recording_already_processing_says_it_left_inbox(ws, capsys):
    claimed = ws / "inbox" / "_processing"
    claimed.mkdir(parents=True, exist_ok=True)
    (claimed / "gone.m4a").write_bytes(b"x" * 100)
    assert cli.main(["count", "4", "gone", "--workspace", str(ws)]) == 1
    assert "left inbox/" in capsys.readouterr().err


def test_count_with_nothing_waiting_fails_and_says_to_name_one(ws, capsys):
    assert cli.main(["count", "4", "--workspace", str(ws)]) == 1
    err = capsys.readouterr().err
    assert "no recording is waiting" in err and "moim-scribe count 4" in err


@pytest.mark.parametrize("bad", ["0", "31", "abc", "2.5", "-1", ""])
def test_count_out_of_range_or_not_a_number_fails_and_records_nothing(ws, capsys, bad):
    _audio(ws, "standup.m4a")
    assert cli.main(["count", bad, "--workspace", str(ws)]) == 1
    assert "whole number from 1 to 30" in capsys.readouterr().err
    assert headcount.given_count(load_config(ws), "standup.m4a") is None


def test_count_out_of_range_is_reported_as_such_even_with_nothing_waiting(ws, capsys):
    assert cli.main(["count", "31", "--workspace", str(ws)]) == 1
    assert "whole number from 1 to 30" in capsys.readouterr().err


@pytest.mark.parametrize("good", ["1", "30"])
def test_count_accepts_the_ends_of_the_range(ws, good):
    _audio(ws, "standup.m4a")
    assert cli.main(["count", good, "--workspace", str(ws)]) == 0
    assert headcount.given_count(load_config(ws), "standup.m4a") == int(good)


def test_count_warns_when_the_file_name_already_has_a_count(ws, capsys):
    _audio(ws, "standup 5p.m4a")
    assert cli.main(["count", "3", "standup", "--workspace", str(ws)]) == 0
    out = capsys.readouterr().out
    assert "recorded 3 speakers for standup 5p.m4a" in out
    assert "file name already says 5" in out and "will not be used" in out


def test_count_without_a_file_name_count_has_no_warning(ws, capsys):
    _audio(ws, "standup.m4a")
    assert cli.main(["count", "3", "--workspace", str(ws)]) == 0
    assert "will not be used" not in capsys.readouterr().out


def test_count_finds_a_decomposed_hangul_name_from_a_composed_query(ws, capsys):
    nfc = "회의 녹음.m4a"
    nfd = unicodedata.normalize("NFD", nfc)
    assert nfd != nfc
    try:
        _audio(ws, nfd)
    except OSError:
        pytest.skip("filesystem rejects the decomposed name")
    assert cli.main(["count", "4", "회의", "--workspace", str(ws)]) == 0
    cfg = load_config(ws)
    assert headcount.given_count(cfg, headcount.recording_name(cfg, ws / "inbox" / nfd)) == 4
    capsys.readouterr()


def test_count_refuses_a_folder_that_is_not_a_workspace(tmp_path, capsys):
    assert cli.main(["count", "3", "--workspace", str(tmp_path / "nope")]) == 1
    assert "moim-scribe init" in capsys.readouterr().err


# --- waiting -----------------------------------------------------------------

def test_waiting_prints_one_line_per_recording_with_time_left(ws, capsys, monkeypatch):
    monkeypatch.setattr(headcount.time, "time", lambda: T0)
    _audio(ws, "older one.m4a")
    _audio(ws, "newer.m4a")
    _seen(ws, "older one.m4a", T0 - 100)
    _seen(ws, "newer.m4a", T0 - 10)
    assert cli.main(["waiting", str(ws)]) == 0
    cap = capsys.readouterr()
    assert cap.out.splitlines() == ["older one.m4a\t1:20 left", "newer.m4a\t2:50 left"]


def test_waiting_with_nothing_waiting_prints_nothing_on_stdout(ws, capsys):
    assert cli.main(["waiting", str(ws)]) == 0
    cap = capsys.readouterr()
    assert cap.out == ""
    assert "no recordings are waiting" in cap.err


def test_waiting_when_the_wait_is_off_is_quiet_and_says_why(ws, capsys):
    (ws / "config.yaml").write_text("language: en\nspeaker_count_wait_sec: 0\n", encoding="utf-8")
    _audio(ws, "standup.m4a")
    assert cli.main(["waiting", str(ws)]) == 0
    cap = capsys.readouterr()
    assert cap.out == "" and "wait is off" in cap.err


def test_waiting_when_a_default_count_is_set_is_quiet(ws, capsys):
    (ws / "config.yaml").write_text("language: en\ndefault_speakers: 4\n", encoding="utf-8")
    _audio(ws, "standup.m4a")
    assert cli.main(["waiting", str(ws)]) == 0
    assert capsys.readouterr().out == ""


def test_a_recording_with_a_count_is_no_longer_listed(ws, capsys):
    _audio(ws, "standup.m4a")
    assert cli.main(["waiting", str(ws)]) == 0
    assert capsys.readouterr().out.startswith("standup.m4a\tnot scanned yet")
    assert cli.main(["count", "3", "--workspace", str(ws)]) == 0
    capsys.readouterr()
    assert cli.main(["waiting", str(ws)]) == 0
    assert capsys.readouterr().out == ""


# --- scan-inbox waits --------------------------------------------------------

def test_scan_inbox_waits_then_processes_with_the_count_it_receives(ws, capsys):
    _audio(ws, "standup.m4a")

    def give_count(n):
        if n == 3:
            assert cli.main(["count", "4", "--workspace", str(ws)]) == 0

    ws.time.on_sleep = give_count
    assert cli.main(["scan-inbox", str(ws)]) == 0
    cap = capsys.readouterr()
    assert ws.calls == [("standup.m4a", {"summarize": True, "given_speakers": 4})]
    assert ws.time.t - T0 < 8          # noticed within a poll or two, not at the next full rescan
    assert "{'processed': 1, 'failed': 0, 'skipped': 0}" in cap.out
    assert cap.err.count("waiting for the speaker count: standup.m4a (3:00 left)") == 1
    assert "moim-scribe count <N>" in cap.err
    assert not (ws / "inbox" / "standup.m4a").exists()


def test_scan_inbox_estimates_when_no_count_arrives(ws, capsys):
    _audio(ws, "standup.m4a")
    assert cli.main(["scan-inbox", str(ws)]) == 0
    assert ws.calls == [("standup.m4a", {"summarize": True})]      # no count passed
    assert 180 <= ws.time.t - T0 < 200
    assert "'processed': 1" in capsys.readouterr().out


def test_scan_inbox_starts_an_expired_wait_within_a_poll(ws):
    (ws / "config.yaml").write_text("language: en\nspeaker_count_wait_sec: 183\n", encoding="utf-8")
    _audio(ws, "standup.m4a")
    assert cli.main(["scan-inbox", str(ws)]) == 0
    assert 183 <= ws.call_times["standup.m4a"] - T0 < 186     # not at the next full rescan


def test_scan_inbox_does_not_flood_the_screen_while_it_waits(ws, capsys):
    _audio(ws, "standup.m4a")
    assert cli.main(["scan-inbox", str(ws)]) == 0
    cap = capsys.readouterr()
    assert ws.time.sleeps > 50
    assert len(cap.err.splitlines()) <= 4
    assert len(cap.out.splitlines()) == 1


def test_scan_inbox_no_wait_processes_at_once(ws, capsys):
    _audio(ws, "standup.m4a")
    assert cli.main(["scan-inbox", str(ws), "--no-wait"]) == 0
    assert ws.calls == [("standup.m4a", {"summarize": True})]
    assert ws.time.sleeps == 0
    assert "waiting for" not in capsys.readouterr().err


def test_scan_inbox_no_wait_still_uses_a_count_already_given(ws):
    rec = _audio(ws, "standup.m4a")
    headcount.record_count(load_config(ws), rec, 6)
    assert cli.main(["scan-inbox", str(ws), "--no-wait"]) == 0
    assert ws.calls == [("standup.m4a", {"summarize": True, "given_speakers": 6})]


def test_scan_inbox_with_nothing_waiting_is_the_0_2_0_run(ws, capsys):
    _audio(ws, "standup 3p.m4a")
    assert cli.main(["scan-inbox", str(ws)]) == 0
    cap = capsys.readouterr()
    assert ws.time.sleeps == 0
    assert cap.out == "{'processed': 1, 'failed': 0, 'skipped': 0}\n"
    assert "waiting" not in cap.err


def test_scan_inbox_with_an_empty_inbox_exits_0_without_waiting(ws, capsys):
    assert cli.main(["scan-inbox", str(ws)]) == 0
    assert ws.time.sleeps == 0
    assert capsys.readouterr().out == "{'processed': 0, 'failed': 0, 'skipped': 0}\n"


def test_scan_inbox_with_the_wait_off_in_config_does_not_wait(ws):
    (ws / "config.yaml").write_text("language: en\nspeaker_count_wait_sec: 0\n", encoding="utf-8")
    _audio(ws, "standup.m4a")
    assert cli.main(["scan-inbox", str(ws)]) == 0
    assert ws.time.sleeps == 0 and len(ws.calls) == 1


def test_scan_inbox_ctrl_c_while_waiting_ends_cleanly_and_keeps_the_recording(ws, capsys):
    rec = _audio(ws, "standup.m4a")

    def interrupt(n):
        if n == 2:
            raise KeyboardInterrupt

    ws.time.on_sleep = interrupt
    assert cli.main(["scan-inbox", str(ws)]) == 130
    cap = capsys.readouterr()
    assert rec.exists() and ws.calls == []
    assert "Traceback" not in cap.err and "stopped" in cap.err


def test_scan_inbox_ctrl_c_during_processing_is_also_clean(ws, capsys, monkeypatch):
    _audio(ws, "standup 3p.m4a")

    def boom(cfg, claimed, progress=None, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr("moim_scribe.pipeline.process_recording", boom)
    assert cli.main(["scan-inbox", str(ws)]) == 130
    assert (ws / "inbox" / "standup 3p.m4a").exists()
    assert "Traceback" not in capsys.readouterr().err


def test_scan_inbox_sums_its_passes_and_keeps_waiting_for_the_rest(ws, capsys):
    _audio(ws, "first.m4a")
    _audio(ws, "second.m4a")

    def give(n):
        if n == 2:
            cli.main(["count", "3", "first", "--workspace", str(ws)])
        # "second" is never answered and times out

    ws.time.on_sleep = give
    assert cli.main(["scan-inbox", str(ws)]) == 0
    assert [c[0] for c in ws.calls] == ["first.m4a", "second.m4a"]
    assert ws.call_times["first.m4a"] - T0 < 8       # at once, while "second" kept waiting
    assert ws.call_times["second.m4a"] - T0 >= 180
    assert ws.calls[0][1].get("given_speakers") == 3
    assert "given_speakers" not in ws.calls[1][1]
    assert "{'processed': 2, 'failed': 0, 'skipped': 0}" in capsys.readouterr().out


def test_scan_inbox_failure_after_waiting_exits_1(ws, monkeypatch):
    _audio(ws, "standup.m4a")

    def fail(cfg, claimed, progress=None, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr("moim_scribe.pipeline.process_recording", fail)
    assert cli.main(["scan-inbox", str(ws)]) == 1


def test_scan_inbox_stops_waiting_on_a_setup_problem(ws, monkeypatch, capsys):
    calls = []

    def fake_scan(cfg, **kw):
        calls.append(kw)
        return {"processed": 0, "failed": 0, "skipped": 0, "setup_problems": ["x: y"]}

    monkeypatch.setattr("moim_scribe.pipeline.scan_inbox", fake_scan)
    assert cli.main(["scan-inbox", str(ws)]) == 1
    assert len(calls) == 1 and "setup problem" in capsys.readouterr().out


def test_the_announcement_names_the_workspace_when_it_is_not_the_current_folder(
        ws, capsys, monkeypatch, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)
    _audio(ws, "standup.m4a")
    assert cli.main(["scan-inbox", str(ws)]) == 0
    assert f'--workspace "{ws.resolve()}"' in capsys.readouterr().err


def test_the_announcement_leaves_the_workspace_out_when_it_is_the_current_folder(
        ws, capsys, monkeypatch):
    monkeypatch.chdir(ws)
    _audio(ws, "standup.m4a")
    assert cli.main(["scan-inbox"]) == 0
    err = capsys.readouterr().err
    assert "moim-scribe count <N>" in err and "--workspace" not in err


def test_the_log_line_that_watch_prints_says_how_to_give_a_count(ws, caplog):
    import logging

    from moim_scribe.pipeline import scan_inbox

    _audio(ws, "standup.m4a")
    with caplog.at_level(logging.INFO):
        scan_inbox(load_config(ws), clock=ws.time.clock)
    line = next(r.getMessage() for r in caplog.records if "waiting up to" in r.getMessage())
    assert "moim-scribe count <N>" in line
