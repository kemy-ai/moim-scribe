"""Leftover noise labels: counted out of n_speakers, kept in the transcript.

The boundary (exactly 1.0 s is noise, 1.1 s is a person) and the two
exemptions (real names, identified labels) are the contract.
"""
import json
from pathlib import Path

from moim_scribe.diarize import LEFTOVER_NOISE_MAX_SEC, write_outputs
from moim_scribe.speaker_stats import NOISE_NOTE_KEY, strip_diagnostic_keys

CLEAN = {"triggered": False, "suspects": {}, "anchors": [], "stats": {}}


def _write(tmp_path, merged, id_mapping=None):
    return write_outputs(str(tmp_path), "r_", merged, id_mapping or {}, {}, dict(CLEAN))


def _seg(label, start, dur):
    return {"start": start, "end": round(start + dur, 1), "speaker": label, "text": "x"}


def test_exactly_one_second_is_noise(tmp_path):
    assert LEFTOVER_NOISE_MAX_SEC == 1.0
    merged = [_seg("SPEAKER_00", 0.0, 30.0), _seg("SPEAKER_01", 30.0, 30.0),
              _seg("SPEAKER_02", 60.0, 1.0)]
    *_, data = _write(tmp_path, merged)
    by = {s["label"]: s for s in data["speakers"]}
    assert by["SPEAKER_02"]["leftover_noise"] is True
    assert data["n_speakers"] == 2
    assert data["n_labels_total"] == 3


def test_just_over_one_second_is_a_person(tmp_path):
    merged = [_seg("SPEAKER_00", 0.0, 30.0), _seg("SPEAKER_02", 60.0, 1.1)]
    *_, data = _write(tmp_path, merged)
    assert data["n_speakers"] == 2
    assert "n_labels_total" not in data
    assert all("leftover_noise" not in s for s in data["speakers"])


def test_named_and_identified_labels_are_never_noise(tmp_path):
    merged = [_seg("SPEAKER_00", 0.0, 30.0), _seg("Ada", 30.0, 0.5),
              _seg("UNKNOWN", 40.0, 0.5)]
    *_, data = _write(tmp_path, merged, id_mapping={"SPEAKER_09": "Ada"})
    assert data["n_speakers"] == 3
    assert all("leftover_noise" not in s for s in data["speakers"])


def test_transcript_keeps_the_noise_label(tmp_path):
    merged = [_seg("SPEAKER_00", 0.0, 30.0), _seg("SPEAKER_01", 30.0, 0.4)]
    _, txt, _, _ = _write(tmp_path, merged)
    assert "SPEAKER_01: x" in Path(txt).read_text(encoding="utf-8")


def test_prompt_block_counts_people_and_says_what_was_excluded(tmp_path):
    merged = [_seg("SPEAKER_00", 0.0, 30.0), _seg("SPEAKER_01", 30.0, 30.0),
              _seg("SPEAKER_02", 60.0, 0.6), _seg("SPEAKER_03", 70.0, 0.2)]
    *_, data = _write(tmp_path, merged)
    out = strip_diagnostic_keys(data)
    assert out["n_speakers"] == 2
    assert [s["label"] for s in out["speakers"]] == ["SPEAKER_00", "SPEAKER_01"]
    assert "n_labels_total" not in out
    note = out[NOISE_NOTE_KEY]
    assert "SPEAKER_02" in note and "SPEAKER_03" in note and "excluded" in note
    assert "leftover_noise" not in json.dumps(out)


def test_no_note_without_noise(tmp_path):
    *_, data = _write(tmp_path, [_seg("SPEAKER_00", 0.0, 30.0)])
    assert NOISE_NOTE_KEY not in strip_diagnostic_keys(data)
