"""`watch` while recordings wait for their speaker count (0.2.1 review fix).

A count is a file outside inbox/, so no file event announces it: watch looks
every headcount.WAIT_POLL_SEC while something waits, and passes again as soon
as a count arrives or a wait runs out, instead of at the next 60 s fallback
scan. With nothing waiting it behaves as before. watch() runs here on fake
time, with the observer and the event replaced, so watchdog is not needed.
"""
import importlib
import sys
import types
from pathlib import Path

import pytest

from moim_scribe import headcount, pipeline
from moim_scribe.config import ensure_workspace_dirs, load_config

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
    cfg = load_config(tmp_path)
    ensure_workspace_dirs(cfg)
    cfg.language = "en"
    monkeypatch.setattr(pipeline, "is_stable_audio", lambda p: True)
    calls = []

    def fake_process(cfg_, claimed, progress=None, **kw):
        calls.append((claimed.name, kw))
        if cfg._on_process:
            cfg._on_process(claimed)
        return claimed

    monkeypatch.setattr(pipeline, "process_recording", fake_process)
    cfg._calls = calls
    cfg._on_process = None        # callable(claimed): what happens while it is processed
    return cfg


def _import_watch(monkeypatch):
    """moim_scribe.watch, importable without watchdog installed."""
    try:
        import watchdog.events
        import watchdog.observers  # noqa: F401
    except ImportError:
        events = types.ModuleType("watchdog.events")
        events.FileSystemEventHandler = object
        observers = types.ModuleType("watchdog.observers")
        observers.Observer = object
        monkeypatch.setitem(sys.modules, "watchdog", types.ModuleType("watchdog"))
        monkeypatch.setitem(sys.modules, "watchdog.events", events)
        monkeypatch.setitem(sys.modules, "watchdog.observers", observers)
        monkeypatch.delitem(sys.modules, "moim_scribe.watch", raising=False)
    return importlib.import_module("moim_scribe.watch")


class _FakeObserver:
    def schedule(self, *a, **k):
        pass

    def start(self):
        pass

    def stop(self):
        pass

    def join(self):
        pass


def _run_watch(monkeypatch, cfg, *, on_wait=None, passes=3, interval=60.0):
    """Run watch() on fake time until it has made `passes` passes.

    Returns (pass times, the timeouts it waited with)."""
    w = _import_watch(monkeypatch)
    clock = cfg._clock = Clock()
    timeouts = []

    class FakeEvent:
        def __init__(self):
            self.flag = False

        def set(self):
            self.flag = True

        def clear(self):
            self.flag = False

        def wait(self, timeout=None):
            timeouts.append(timeout)
            if on_wait:
                on_wait(len(timeouts), clock.t)
            if self.flag:
                return True
            clock.t += timeout
            return False

    pass_times = []
    real_scan = pipeline.scan_inbox

    def scan(cfg_, **kw):
        if len(pass_times) == passes:
            raise KeyboardInterrupt
        pass_times.append(clock.t)
        result = real_scan(cfg_, clock=clock, **kw)
        if result.get("processed"):
            processed_at.append(clock.t)
        return result

    processed_at = []

    monkeypatch.setattr(w, "Observer", _FakeObserver)
    monkeypatch.setattr(w, "_AudioEventHandler", lambda wake: None)
    monkeypatch.setattr(w, "threading", types.SimpleNamespace(Event=FakeEvent))
    monkeypatch.setattr(w, "time", types.SimpleNamespace(time=clock))
    monkeypatch.setattr(w, "scan_inbox", scan)
    w.watch(cfg, interval_sec=interval)
    return types.SimpleNamespace(passes=pass_times, timeouts=timeouts, processed_at=processed_at)


def test_f7_watch_starts_a_recording_right_after_its_count_arrives(ws, monkeypatch):
    rec = _audio(ws.inbox_dir / "standup.m4a")

    def give(n, t):
        if n == 2:
            headcount.record_count(ws, rec, 4)

    run = _run_watch(monkeypatch, ws, on_wait=give, passes=2)
    assert ws._calls == [("standup.m4a", {"summarize": True, "given_speakers": 4})]
    assert run.processed_at[0] - T0 < 10            # not at the 60 s fallback


def test_f7_watch_starts_an_expired_wait_within_a_poll(ws, monkeypatch):
    _audio(ws.inbox_dir / "standup.m4a")
    # An interval that does not divide the wait, so the expiry must wake it.
    run = _run_watch(monkeypatch, ws, passes=4, interval=100.0)
    assert ws._calls == [("standup.m4a", {"summarize": True})]
    assert 180 <= run.processed_at[0] - T0 < 180 + 2 * headcount.WAIT_POLL_SEC   # not at 200
    # While it waited: a pass at the start, one per interval, one at expiry.
    assert [t - T0 for t in run.passes[:3]] == [0, 100, run.processed_at[0] - T0]


def test_f7_watch_with_nothing_waiting_sleeps_the_interval_as_before(ws, monkeypatch):
    _audio(ws.inbox_dir / "standup 3p.m4a")
    run = _run_watch(monkeypatch, ws, passes=3, interval=60.0)
    assert run.timeouts == [60.0, 60.0, 60.0]
    assert run.passes == [T0, T0 + 60, T0 + 120]


def test_f7_watch_returns_to_the_interval_once_nothing_waits(ws, monkeypatch):
    rec = _audio(ws.inbox_dir / "standup.m4a")

    def remove(n, t):
        if n == 3 and rec.exists():
            rec.unlink()

    run = _run_watch(monkeypatch, ws, on_wait=remove, passes=3)
    assert ws._calls == []
    assert run.timeouts[:3] == [headcount.WAIT_POLL_SEC] * 3
    assert run.timeouts[3:] == [60.0, 60.0]
    assert run.passes[1] - T0 < 10                  # noticed the removal at once


# --- G1: what happens while a pass is busy with another recording -------------

def _long_pass(cfg, then=None, seconds=200):
    """Processing "b 3p.m4a" takes `seconds`; `then(clock)` runs 50 s in."""
    def on_process(claimed):
        if claimed.name == "b 3p.m4a":
            cfg._clock.t += 50
            if then:
                then(cfg._clock)
            cfg._clock.t += seconds - 50
            cfg._pass_end = cfg._clock.t
    cfg._on_process = on_process


def test_g1_watch_a_count_given_during_a_long_pass_starts_right_after_it(ws, monkeypatch):
    a = _audio(ws.inbox_dir / "a.m4a")
    _audio(ws.inbox_dir / "b 3p.m4a")
    _long_pass(ws, then=lambda clock: headcount.record_count(ws, a, 4, now=clock.t))
    run = _run_watch(monkeypatch, ws, passes=3)
    assert [c[0] for c in ws._calls] == ["b 3p.m4a", "a.m4a"]
    assert ws._calls[1][1].get("given_speakers") == 4
    assert run.processed_at[1] - ws._pass_end < headcount.WAIT_POLL_SEC   # not 60 s later


def test_g1_watch_a_wait_that_ran_out_during_a_long_pass_starts_right_after_it(ws, monkeypatch):
    _audio(ws.inbox_dir / "a.m4a")
    _audio(ws.inbox_dir / "b 3p.m4a")
    _long_pass(ws, seconds=200)                       # a's 180 s run out meanwhile
    run = _run_watch(monkeypatch, ws, passes=3)
    assert [c[0] for c in ws._calls] == ["b 3p.m4a", "a.m4a"]
    assert run.passes[1] - ws._pass_end < headcount.WAIT_POLL_SEC


def test_g1_an_expired_unclaimable_recording_does_not_make_watch_pass_in_a_loop(ws, monkeypatch):
    _audio(ws.inbox_dir / "stuck.m4a")
    headcount.mark_seen(ws, "stuck.m4a", T0 - 1000)
    _audio(ws.inbox_dir / "other.m4a")                 # keeps watch in its waiting mode
    monkeypatch.setattr(pipeline.shutil, "move",
                        lambda s, d: (_ for _ in ()).throw(OSError("busy")))
    run = _run_watch(monkeypatch, ws, passes=4, interval=60.0)
    gaps = [b - a for a, b in zip(run.passes, run.passes[1:])]
    assert all(g >= headcount.WAIT_POLL_SEC for g in gaps)   # never back to back

