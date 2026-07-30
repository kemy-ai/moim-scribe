"""Which engine process_recording dispatches to, and what it must not touch.

The dangerous direction is local-only setup running on the cloud path.
get_hf_token raises SystemExit when no token is present, and SystemExit is a
BaseException — so it slips past the `except Exception` that scan_inbox uses to
contain one bad recording. Called unconditionally, a Soniox user without a
Hugging Face token would lose the whole watch loop rather than one file.
"""
import pytest

from moim_scribe import pipeline
from moim_scribe.config import ensure_workspace_dirs, load_config


class _Reached(Exception):
    """Raised by the stub that stands in for the engine, to stop the run right
    after dispatch — everything downstream (summarizing, file moves) is out of
    scope here."""


@pytest.fixture
def ws(tmp_path):
    cfg = load_config(tmp_path)
    ensure_workspace_dirs(cfg)
    audio = cfg.inbox_dir / "meeting 2p.m4a"
    audio.write_bytes(b"x" * 100)
    return cfg, audio


def _forbid(name):
    def fail(*a, **k):
        raise AssertionError(f"{name} must not run on this path")
    return fail


def _reach(*a, **k):
    raise _Reached


def test_soniox_path_does_not_resolve_a_hugging_face_token(ws, monkeypatch):
    cfg, audio = ws
    cfg.stt_backend = "soniox"
    monkeypatch.setattr(pipeline, "get_hf_token", _forbid("get_hf_token"))
    monkeypatch.setattr(pipeline, "get_backend", _forbid("get_backend"))
    monkeypatch.setattr("moim_scribe.stt.soniox_client.get_api_key", lambda *a, **k: "key")
    monkeypatch.setattr("moim_scribe.diarize_cloud.run_cloud_diarization", _reach)

    with pytest.raises(_Reached):
        pipeline.process_recording(cfg, audio, "meeting")


def test_local_path_still_resolves_the_token(ws, monkeypatch):
    """The mirror of the test above: the guard must not have disabled the local
    path's requirement."""
    cfg, audio = ws
    cfg.stt_backend = "mlx"
    called = []
    monkeypatch.setattr(pipeline, "get_hf_token", lambda *a, **k: called.append("hf") or "tok")
    monkeypatch.setattr(pipeline, "get_backend", lambda *a, **k: object())
    monkeypatch.setattr(pipeline, "run_diarization", _reach)

    with pytest.raises(_Reached):
        pipeline.process_recording(cfg, audio, "meeting")
    assert called == ["hf"]


def test_local_path_does_not_look_for_a_soniox_key(ws, monkeypatch):
    cfg, audio = ws
    cfg.stt_backend = "mlx"
    monkeypatch.setattr("moim_scribe.stt.soniox_client.get_api_key",
                        _forbid("get_api_key"))
    monkeypatch.setattr(pipeline, "get_hf_token", lambda *a, **k: "tok")
    monkeypatch.setattr(pipeline, "get_backend", lambda *a, **k: object())
    monkeypatch.setattr(pipeline, "run_diarization", _reach)

    with pytest.raises(_Reached):
        pipeline.process_recording(cfg, audio, "meeting")


def test_soniox_path_passes_region_and_language_through(ws, monkeypatch):
    cfg, audio = ws
    cfg.stt_backend = "soniox"
    cfg.soniox_region = "eu"
    cfg.language = "en"
    seen = {}

    def capture(path, **kw):
        seen.update(kw)
        raise _Reached

    monkeypatch.setattr("moim_scribe.stt.soniox_client.get_api_key", lambda *a, **k: "key")
    monkeypatch.setattr("moim_scribe.diarize_cloud.run_cloud_diarization", capture)
    with pytest.raises(_Reached):
        pipeline.process_recording(cfg, audio, "meeting")

    assert seen["region"] == "eu"
    assert seen["language_hints"] == ["en"]
    # Passed through even though Soniox ignores it, so the log can name the
    # hint it is dropping instead of the filename count vanishing silently.
    assert seen["num_speakers"] == 2


def test_a_missing_soniox_key_is_catchable_not_fatal(ws, monkeypatch):
    """The failure mode this whole module exists for: one recording fails, the
    caller's `except Exception` catches it, the loop lives."""
    cfg, audio = ws
    cfg.stt_backend = "soniox"
    monkeypatch.delenv("SONIOX_API_KEY", raising=False)

    try:
        pipeline.process_recording(cfg, audio, "meeting")
    except Exception as e:  # noqa: BLE001 — being catchable here is the assertion
        assert "soniox" in str(e).lower()
    else:
        pytest.fail("expected a missing-key error")
