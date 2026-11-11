"""The Soniox diarization path: label normalization, ordering guarantees, and
the branch that must not touch local-only setup.

No network and no models — every remote call and both shared helpers are
stubbed, so what is under test is the wiring.
"""
import pytest

from moim_scribe import diarize_cloud as dc

# ── label normalization ──────────────────────────────────────────────────────

def test_empty_text_segments_are_dropped():
    """Matches transcribe_segments, which keeps only segments whose text
    survives .strip() — otherwise the txt gains blank speaker lines."""
    out = dc.normalize_segments([
        {"start": 0.0, "end": 1.0, "speaker": "1", "text": "hello"},
        {"start": 1.0, "end": 2.0, "speaker": "1", "text": "   "},
        {"start": 2.0, "end": 3.0, "speaker": "2", "text": ""},
    ])
    assert len(out) == 1
    assert out[0]["text"] == "hello"


def test_null_speaker_becomes_unknown_not_none():
    """Soniox can leave silence and special tokens unattributed even with
    diarization on. Left as None it reaches the txt as "None:" and
    speakers.json as a null label."""
    out = dc.normalize_segments([
        {"start": 0.0, "end": 1.0, "speaker": None, "text": "hm"},
    ])
    assert out[0]["speaker"] == dc.UNKNOWN_SPEAKER_LABEL
    assert out[0]["speaker"] == "UNKNOWN"  # the label diarize.speaker_at falls back to


def test_provider_ids_become_speaker_nn():
    """Everything the user reads — the lenses, the enroll instructions, the
    label-confidence header — talks about SPEAKER_NN."""
    out = dc.normalize_segments([
        {"start": 0.0, "end": 1.0, "speaker": "1", "text": "a"},
        {"start": 1.0, "end": 2.0, "speaker": "2", "text": "b"},
    ])
    assert [s["speaker"] for s in out] == ["SPEAKER_00", "SPEAKER_01"]


def test_numeric_ids_sort_numerically_not_lexically():
    """Ten speakers would otherwise map "10" before "2"."""
    mapping = dc._label_map({"2", "10", "1"})
    assert mapping["1"] == "SPEAKER_00"
    assert mapping["2"] == "SPEAKER_01"
    assert mapping["10"] == "SPEAKER_02"


def test_non_numeric_ids_still_map_deterministically():
    a = dc._label_map({"spk_b", "spk_a"})
    b = dc._label_map({"spk_a", "spk_b"})
    assert a == b  # set iteration order must not leak into the labels


def test_normalize_does_not_mutate_the_input():
    original = [{"start": 0.0, "end": 1.0, "speaker": "1", "text": "a"}]
    dc.normalize_segments(original)
    assert original[0]["speaker"] == "1"


def test_unknown_and_named_speakers_coexist():
    out = dc.normalize_segments([
        {"start": 0.0, "end": 1.0, "speaker": None, "text": "hm"},
        {"start": 1.0, "end": 2.0, "speaker": "1", "text": "hello"},
    ])
    assert [s["speaker"] for s in out] == ["UNKNOWN", "SPEAKER_00"]


# ── duration guard ───────────────────────────────────────────────────────────

def test_duration_returns_none_when_ffprobe_fails(tmp_path, monkeypatch):
    """Unknown duration must proceed: refusing a file because we could not
    measure it would be worse than trying."""
    monkeypatch.setattr(dc.subprocess, "run", lambda *a, **k: _proc(1, ""))
    assert dc._audio_duration_sec(str(tmp_path / "x.m4a")) is None


def test_duration_parses_ffprobe_json(monkeypatch):
    monkeypatch.setattr(dc.subprocess, "run",
                        lambda *a, **k: _proc(0, '{"format": {"duration": "123.5"}}'))
    assert dc._audio_duration_sec("x.m4a") == pytest.approx(123.5)


def test_duration_survives_garbage_output(monkeypatch):
    monkeypatch.setattr(dc.subprocess, "run", lambda *a, **k: _proc(0, "not json"))
    assert dc._audio_duration_sec("x.m4a") is None


def test_overlong_file_is_refused_before_uploading(tmp_path, monkeypatch):
    """The 300-minute cap is fixed and cannot be raised, so uploading first
    spends the bandwidth and the wait to be told no."""
    f = tmp_path / "long.m4a"
    f.write_bytes(b"x" * 10)
    monkeypatch.setattr(dc, "_audio_duration_sec", lambda p: 301 * 60)
    monkeypatch.setattr(dc.soniox_client, "upload_file",
                        lambda *a, **k: pytest.fail("must not upload"))
    with pytest.raises(ValueError, match="300-minute"):
        dc.run_cloud_diarization(str(f), api_key="k", speaker_db=None, model_dir=tmp_path)


def test_directory_input_is_refused(tmp_path):
    with pytest.raises(ValueError, match="single file"):
        dc.run_cloud_diarization(str(tmp_path), api_key="k", speaker_db=None,
                                 model_dir=tmp_path)


def _proc(returncode, stdout):
    class P:
        pass
    p = P()
    p.returncode = returncode
    p.stdout = stdout
    return p


# ── end-to-end wiring, all remote calls stubbed ──────────────────────────────

@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Stub the whole remote surface and record the call order."""
    audio = tmp_path / "rec.m4a"
    audio.write_bytes(b"x" * 100)
    order = []

    monkeypatch.setattr(dc, "_audio_duration_sec", lambda p: 60.0)
    monkeypatch.setattr(dc.soniox_client, "upload_file",
                        lambda *a, **k: order.append("upload") or "file-1")
    monkeypatch.setattr(dc.soniox_client, "create_transcription",
                        lambda *a, **k: order.append("create") or "tr-1")
    monkeypatch.setattr(dc.soniox_client, "wait_for_completion",
                        lambda *a, **k: order.append("wait") or {"status": "completed"})
    monkeypatch.setattr(dc.soniox_client, "get_transcript", lambda *a, **k: (
        order.append("fetch") or {"tokens": [
            {"start_ms": 0, "end_ms": 900, "text": "hello", "speaker": "1"},
            {"start_ms": 900, "end_ms": 1800, "text": "hi", "speaker": "2"},
        ]}
    ))
    monkeypatch.setattr(dc.soniox_client, "cleanup",
                        lambda *a, **k: order.append("cleanup"))
    monkeypatch.setattr(dc, "identify_and_repair",
                        lambda audio, merged, raw, db, md, ident: (
                            order.append("identify") or (merged, {}, {}, {"triggered": False})
                        ))
    monkeypatch.setattr(dc, "write_outputs", lambda *a, **k: (
        order.append("write") or ("j.jsonl", "t.txt", "s.json", {"n_speakers": 2})
    ))
    return audio, order


def test_happy_path_returns_the_output_contract(wired, tmp_path):
    """pipeline.process_recording reads exactly these three keys, and reads
    them the same way for both engines."""
    audio, _ = wired
    result = dc.run_cloud_diarization(str(audio), api_key="k", speaker_db=None,
                                     model_dir=tmp_path)
    assert result["txt_path"] == "t.txt"
    assert result["jsonl_path"] == "j.jsonl"
    assert result["stats_path"] == "s.json"


def test_outputs_are_written_before_remote_deletion(wired, tmp_path):
    """🔴 Transcription is the paid, unrepeatable step. If cleanup ran first and
    then hung or crashed, the work would be gone and already billed."""
    audio, order = wired
    dc.run_cloud_diarization(str(audio), api_key="k", speaker_db=None, model_dir=tmp_path)
    assert order.index("write") < order.index("cleanup")


def test_call_order_is_upload_create_wait_fetch(wired, tmp_path):
    audio, order = wired
    dc.run_cloud_diarization(str(audio), api_key="k", speaker_db=None, model_dir=tmp_path)
    assert order[:4] == ["upload", "create", "wait", "fetch"]


def test_identification_runs_after_labels_are_normalized(wired, tmp_path, monkeypatch):
    """identify_and_repair must see SPEAKER_NN, the same label space the local
    path gives it — otherwise provider ids leak into speakers.json."""
    audio, _ = wired
    seen = {}
    monkeypatch.setattr(dc, "identify_and_repair", lambda audio, merged, raw, db, md, i: (
        seen.update(labels=[m["speaker"] for m in merged], raw=list(raw))
        or (merged, {}, {}, {"triggered": False})
    ))
    dc.run_cloud_diarization(str(audio), api_key="k", speaker_db=None, model_dir=tmp_path)
    assert seen["labels"] == ["SPEAKER_00", "SPEAKER_01"]
    assert seen["raw"] == ["SPEAKER_00", "SPEAKER_01"]


def test_cleanup_still_runs_when_transcription_fails(wired, tmp_path, monkeypatch):
    """An upload nobody deletes keeps counting against the 10 GB / 1,000 file
    cap, and a half-finished run is exactly when it gets forgotten."""
    audio, order = wired
    monkeypatch.setattr(dc.soniox_client, "wait_for_completion",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError, match="boom"):
        dc.run_cloud_diarization(str(audio), api_key="k", speaker_db=None, model_dir=tmp_path)
    assert "cleanup" in order


def test_empty_transcript_raises_rather_than_writing_nothing(wired, tmp_path, monkeypatch):
    audio, order = wired
    monkeypatch.setattr(dc.soniox_client, "get_transcript", lambda *a, **k: {"tokens": []})
    with pytest.raises(RuntimeError, match="no usable segments"):
        dc.run_cloud_diarization(str(audio), api_key="k", speaker_db=None, model_dir=tmp_path)
    assert "cleanup" in order
    assert "write" not in order


def test_speaker_count_hint_is_reported_as_ignored(wired, tmp_path, caplog):
    """Soniox takes no speaker count. Accepting the hint silently would make a
    filename like "conversation 2p" look like it worked."""
    audio, _ = wired
    with caplog.at_level("INFO"):
        dc.run_cloud_diarization(str(audio), api_key="k", speaker_db=None,
                                 model_dir=tmp_path, num_speakers=2)
    assert "ignored" in caplog.text


def test_no_hint_produces_no_ignore_notice(wired, tmp_path, caplog):
    audio, _ = wired
    with caplog.at_level("INFO"):
        dc.run_cloud_diarization(str(audio), api_key="k", speaker_db=None,
                                 model_dir=tmp_path)
    assert "ignored" not in caplog.text


def test_two_voice_result_reaches_write_outputs(wired, tmp_path, monkeypatch):
    """The cloud path must hand identify_and_repair's mixed_check to the writer,
    or the diagnostic exists in memory and never lands in speakers.json."""
    audio, _ = wired
    seen = {}
    marker = {"checked": 1, "suspects": {}}
    monkeypatch.setattr(dc, "identify_and_repair", lambda audio, merged, raw, db, md, i: (
        merged, {}, {}, {"triggered": False, "mixed_check": marker}
    ))
    monkeypatch.setattr(dc, "write_outputs", lambda *a, **k: (
        seen.update(k) or ("j.jsonl", "t.txt", "s.json", {"n_speakers": 2})
    ))
    dc.run_cloud_diarization(str(audio), api_key="k", speaker_db=None, model_dir=tmp_path)
    assert seen["mixed_info"] is marker
