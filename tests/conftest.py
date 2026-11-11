"""Shared fixtures.

The test environment has no ffmpeg guarantee, no whisper, no pyannote and no
tokens, and most tests stub the engine anyway. So the setup preflight that
scan_inbox runs before claiming anything is switched off by default; a test
that is about the preflight itself asks for the `real_preflight` fixture.
"""
import pytest

from moim_scribe import preflight


@pytest.fixture
def real_preflight():
    """Request this to run the real preflight checks in a test."""
    return preflight.preflight


@pytest.fixture(autouse=True)
def _preflight_passes(request, monkeypatch):
    if "real_preflight" in request.fixturenames:
        return
    monkeypatch.setattr(preflight, "preflight", lambda cfg: [])
