"""Shared fixtures.

The test environment has no ffmpeg guarantee, no whisper, no pyannote and no
tokens, and most tests stub the engine anyway. So the setup preflight that
scan_inbox runs before claiming anything is switched off by default; a test
that is about the preflight itself asks for the `real_preflight` fixture.

Likewise the wait for a speaker count (0.2.1): most tests drop a recording
with no count in its name and expect it processed in the same pass, which is
0.2.0 behaviour. The gate still runs, but as if the caller had passed
wait=False (a count the user gave is still honoured). A test about the wait
asks for the `real_count_wait` fixture.
"""
import pytest

from moim_scribe import pipeline, preflight


@pytest.fixture
def real_preflight():
    """Request this to run the real preflight checks in a test."""
    return preflight.preflight


@pytest.fixture(autouse=True)
def _preflight_passes(request, monkeypatch):
    if "real_preflight" in request.fixturenames:
        return
    monkeypatch.setattr(preflight, "preflight", lambda cfg: [])


@pytest.fixture
def real_count_wait():
    """Request this to run the speaker count wait as configured."""
    return pipeline._count_gate


@pytest.fixture(autouse=True)
def _no_count_wait(request, monkeypatch):
    if "real_count_wait" in request.fixturenames:
        return
    gate = pipeline._count_gate

    def no_wait(cfg, f, *, wait, now):
        return gate(cfg, f, wait=False, now=now)

    monkeypatch.setattr(pipeline, "_count_gate", no_wait)
