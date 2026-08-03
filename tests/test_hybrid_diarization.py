"""The hybrid step must be incapable of costing a transcription.

By the time it runs, the audio has been uploaded, transcribed and paid for, and
that work cannot be repeated. So every way pyannote can fail — absent token,
undownloaded model, no GPU, unreadable file, OOM — has to end with the Soniox
labels intact and the run finishing. These tests assert that failure shape
directly, one cause at a time, because "it worked when I tried it" only ever
exercises the success path.

The missing-token case carries a second requirement: it must not raise
SystemExit. get_hf_token does, SystemExit is a BaseException, and scan_inbox's
`except Exception` does not catch it — so a Soniox user without a Hugging Face
token would lose the watch loop rather than one optional improvement. That is
why this path calls find_hf_token instead.
"""
from pathlib import Path

import pytest

from moim_scribe import diarize_cloud


def seg(start, end, speaker):
    return {"start": start, "end": end, "speaker": speaker, "text": "x"}


@pytest.fixture
def merged():
    return [seg(0.0, 5.0, "SPEAKER_00"), seg(6.0, 9.0, "SPEAKER_00")]


@pytest.fixture
def audio(tmp_path):
    p = tmp_path / "meeting.m4a"
    p.write_bytes(b"x" * 100)
    return p


def _boom(name):
    def fail(*a, **k):
        raise RuntimeError(f"{name} exploded")
    return fail


def test_no_token_returns_the_segments_untouched(merged, audio, tmp_path, monkeypatch):
    monkeypatch.setattr(diarize_cloud, "find_hf_token", lambda *a, **k: None)
    monkeypatch.setattr(diarize_cloud, "prepare_audio", _boom("prepare_audio"))

    out = diarize_cloud.apply_hybrid_boundaries(str(audio), merged, tmp_path)

    assert out is merged


def test_no_token_does_not_raise_system_exit(merged, audio, tmp_path, monkeypatch):
    """The reason find_hf_token exists. An empty workspace has no .hf_token, so
    this exercises the real lookup rather than a stub of it."""
    monkeypatch.delenv("HF_TOKEN", raising=False)

    out = diarize_cloud.apply_hybrid_boundaries(str(audio), merged, tmp_path)

    assert out is merged


def test_a_failing_diarizer_keeps_the_soniox_labels(merged, audio, tmp_path, monkeypatch):
    monkeypatch.setattr(diarize_cloud, "find_hf_token", lambda *a, **k: "tok")
    monkeypatch.setattr(diarize_cloud, "prepare_audio", lambda inp, wd: str(audio))
    monkeypatch.setattr(diarize_cloud, "diarize_turns", _boom("diarize_turns"))

    out = diarize_cloud.apply_hybrid_boundaries(str(audio), merged, tmp_path)

    assert [m["speaker"] for m in out] == ["SPEAKER_00", "SPEAKER_00"]


def test_a_failing_audio_conversion_keeps_the_soniox_labels(
    merged, audio, tmp_path, monkeypatch
):
    monkeypatch.setattr(diarize_cloud, "find_hf_token", lambda *a, **k: "tok")
    monkeypatch.setattr(diarize_cloud, "prepare_audio", _boom("prepare_audio"))

    out = diarize_cloud.apply_hybrid_boundaries(str(audio), merged, tmp_path)

    assert out is merged


def test_the_temp_wav_is_removed_after_success(merged, audio, tmp_path, monkeypatch):
    """A 140 MB recording expands to roughly twice that as 16k mono wav. Left
    behind once per recording, it fills the disk quietly."""
    wav = tmp_path / "_diarize_full_1.wav"
    wav.write_bytes(b"wav")
    monkeypatch.setattr(diarize_cloud, "find_hf_token", lambda *a, **k: "tok")
    monkeypatch.setattr(diarize_cloud, "prepare_audio", lambda inp, wd: str(wav))
    monkeypatch.setattr(diarize_cloud, "diarize_turns", lambda *a, **k: [(0.0, 9.0, "A")])

    diarize_cloud.apply_hybrid_boundaries(str(audio), merged, tmp_path)

    assert not wav.exists()


def test_the_temp_wav_is_removed_after_failure(merged, audio, tmp_path, monkeypatch):
    wav = tmp_path / "_diarize_full_2.wav"
    wav.write_bytes(b"wav")
    monkeypatch.setattr(diarize_cloud, "find_hf_token", lambda *a, **k: "tok")
    monkeypatch.setattr(diarize_cloud, "prepare_audio", lambda inp, wd: str(wav))
    monkeypatch.setattr(diarize_cloud, "diarize_turns", _boom("diarize_turns"))

    diarize_cloud.apply_hybrid_boundaries(str(audio), merged, tmp_path)

    assert not wav.exists()


def test_a_successful_run_replaces_the_labels(merged, audio, tmp_path, monkeypatch):
    monkeypatch.setattr(diarize_cloud, "find_hf_token", lambda *a, **k: "tok")
    monkeypatch.setattr(diarize_cloud, "prepare_audio", lambda inp, wd: str(audio))
    monkeypatch.setattr(
        diarize_cloud, "diarize_turns",
        lambda *a, **k: [(0.0, 5.5, "SPEAKER_A"), (5.5, 10.0, "SPEAKER_B")],
    )

    out = diarize_cloud.apply_hybrid_boundaries(str(audio), merged, tmp_path)

    assert [m["speaker"] for m in out] == ["SPEAKER_A", "SPEAKER_B"]


def test_the_speaker_count_hint_reaches_pyannote(merged, audio, tmp_path, monkeypatch):
    """On the Soniox path this hint has nowhere to go — Soniox takes no count.
    The hybrid step is the first thing that can use it, and the module docstring
    now says so; if the wiring drops it, that sentence becomes false."""
    seen = {}
    monkeypatch.setattr(diarize_cloud, "find_hf_token", lambda *a, **k: "tok")
    monkeypatch.setattr(diarize_cloud, "prepare_audio", lambda inp, wd: str(audio))
    monkeypatch.setattr(
        diarize_cloud, "diarize_turns",
        lambda a, t, n: seen.update(num_speakers=n) or [(0.0, 9.0, "A")],
    )

    diarize_cloud.apply_hybrid_boundaries(str(audio), merged, tmp_path, num_speakers=5)

    assert seen["num_speakers"] == 5


class TestWiring:
    """That the switch reaches the step at all, and that off means off."""

    def _run(self, monkeypatch, merged, audio, **kw):
        calls = []
        monkeypatch.setattr(
            diarize_cloud, "apply_hybrid_boundaries",
            lambda *a, **k: calls.append(a[2]) or merged,  # (path, merged, workspace, n)
        )
        monkeypatch.setattr(diarize_cloud, "_audio_duration_sec", lambda p: 60.0)
        monkeypatch.setattr(
            diarize_cloud.soniox_client, "upload_file", lambda *a, **k: "f1")
        monkeypatch.setattr(
            diarize_cloud.soniox_client, "create_transcription", lambda *a, **k: "t1")
        monkeypatch.setattr(
            diarize_cloud.soniox_client, "wait_for_completion", lambda *a, **k: None)
        monkeypatch.setattr(
            diarize_cloud.soniox_client, "get_transcript", lambda *a, **k: {"tokens": []})
        monkeypatch.setattr(
            diarize_cloud.soniox_client, "tokens_to_segments",
            lambda t: [dict(m, speaker="1") for m in merged])
        monkeypatch.setattr(diarize_cloud.soniox_client, "cleanup", lambda *a, **k: None)
        monkeypatch.setattr(
            diarize_cloud, "identify_and_repair",
            lambda *a, **k: (merged, {}, {}, {"triggered": False}))
        monkeypatch.setattr(
            diarize_cloud, "write_outputs", lambda *a, **k: ("j", "t", "s", {}))
        diarize_cloud.run_cloud_diarization(
            str(audio), api_key="k", speaker_db=object(), model_dir=Path("/m"), **kw)
        return calls

    def test_hybrid_off_never_calls_the_step(self, monkeypatch, merged, audio, tmp_path):
        assert self._run(monkeypatch, merged, audio,
                         workspace=tmp_path, hybrid=False) == []

    def test_hybrid_on_calls_the_step(self, monkeypatch, merged, audio, tmp_path):
        assert self._run(monkeypatch, merged, audio,
                         workspace=tmp_path, hybrid=True) == [tmp_path]

    def test_hybrid_without_a_workspace_is_skipped(self, monkeypatch, merged, audio):
        """There would be nowhere to look for the token; better to skip than to
        guess at a path."""
        assert self._run(monkeypatch, merged, audio, hybrid=True) == []

    def test_the_default_is_off_for_direct_callers(self, monkeypatch, merged, audio,
                                                   tmp_path):
        """The pipeline opts in from config; a bare call does not surprise
        anyone by shelling out to pyannote."""
        assert self._run(monkeypatch, merged, audio, workspace=tmp_path) == []
