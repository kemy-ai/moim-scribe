"""What the faster-whisper backend hands to the model.

Found on a clean Windows install: faster-whisper 1.2.1 decodes a file path
through PyAV with an argument PyAV 19 rejects, so every recording failed.
The backend now reads the prepared 16 kHz wav itself and passes samples.
"""
import sys
import types

import numpy as np
import pytest

from moim_scribe.stt import faster_backend
from moim_scribe.stt.faster_backend import FasterWhisperBackend, load_waveform


def _fake_soundfile(monkeypatch, data, sr):
    mod = types.ModuleType("soundfile")
    calls = []

    def read(path, dtype=None, always_2d=False):
        calls.append((path, dtype, always_2d))
        if isinstance(data, Exception):
            raise data
        return data, sr

    mod.read = read
    monkeypatch.setitem(sys.modules, "soundfile", mod)
    return calls


def test_a_16k_mono_wav_becomes_samples(monkeypatch):
    data = np.linspace(-1, 1, 1600, dtype="float32").reshape(-1, 1)
    calls = _fake_soundfile(monkeypatch, data, 16000)
    out = load_waveform("x.wav")
    assert isinstance(out, np.ndarray) and out.dtype == np.float32
    assert out.shape == (1600,) and out.flags["C_CONTIGUOUS"]
    assert np.array_equal(out, data[:, 0])
    assert calls == [("x.wav", "float32", True)]


def test_stereo_is_mixed_down(monkeypatch):
    data = np.stack([np.ones(100, dtype="float32"), np.zeros(100, dtype="float32")], axis=1)
    _fake_soundfile(monkeypatch, data, 16000)
    out = load_waveform("x.wav")
    assert out.shape == (100,) and np.allclose(out, 0.5)


@pytest.mark.parametrize("sr", [8000, 44100, 48000])
def test_another_sample_rate_is_left_to_the_library(monkeypatch, sr):
    """Samples at the wrong rate would be transcribed as sped-up or slowed
    speech with no error at all. The path goes through instead."""
    _fake_soundfile(monkeypatch, np.zeros((100, 1), dtype="float32"), sr)
    assert load_waveform("x.wav") == "x.wav"


def test_an_unreadable_file_is_left_to_the_library(monkeypatch):
    _fake_soundfile(monkeypatch, RuntimeError("format not recognised"), 0)
    assert load_waveform("x.m4a") == "x.m4a"


def test_an_empty_file_is_left_to_the_library(monkeypatch):
    _fake_soundfile(monkeypatch, np.zeros((0, 1), dtype="float32"), 16000)
    assert load_waveform("x.wav") == "x.wav"


def test_the_model_is_given_samples_not_a_path(monkeypatch):
    """🔴 The whole fix: with a path, PyAV is asked to decode and the run fails."""
    _fake_soundfile(monkeypatch, np.zeros((16000, 1), dtype="float32"), 16000)
    seen = {}

    class FakeModel:
        def transcribe(self, audio, **kw):
            seen["audio"] = audio
            seen["kw"] = kw
            seg = types.SimpleNamespace(start=0.0, end=1.0, text=" hello ")
            return iter([seg]), None

    backend = FasterWhisperBackend("tiny")
    monkeypatch.setattr(backend, "_load", lambda: FakeModel())
    assert backend.transcribe("x.wav", "en", initial_prompt="names") == [(0.0, 1.0, "hello")]
    assert isinstance(seen["audio"], np.ndarray) and seen["audio"].shape == (16000,)
    assert seen["kw"]["language"] == "en" and seen["kw"]["initial_prompt"] == "names"
    assert faster_backend.WHISPER_SAMPLE_RATE == 16000
