"""Enrollment guards, end to end through enroll().

No model: the embedder is injected and looks a vector up by range start.
Voices are built so the numbers are realistic rather than ideal: segments of
one voice sit at 0.7 to each other (not 1.0), different voices at 0.0.
"""
import json

import numpy as np
import pytest

from moim_scribe.speakers import enroll as en
from moim_scribe.speakers.db import SpeakerDB

DIM = 128
WITHIN = 0.7


class Voices:
    """Unit vectors: voice k's segments share base axis k at cos WITHIN."""

    def __init__(self):
        self._next = 8  # axes 0-7 are voice bases, the rest are per-segment noise

    def seg(self, voice: int) -> np.ndarray:
        v = np.zeros(DIM)
        v[voice] = np.sqrt(WITHIN)
        v[self._next] = np.sqrt(1 - WITHIN)
        self._next += 1
        return v


def at(base: np.ndarray, cos: float, axis: int) -> np.ndarray:
    """A unit vector at exactly `cos` to unit `base`, using a spare axis."""
    out = cos * base
    out[axis] += np.sqrt(1 - cos * cos)
    return out


class Rec:
    """A recording: segments (label, duration, vector) laid out 1 s apart."""

    def __init__(self):
        self.rows, self.vec, self.t = [], {}, 0.0

    def add(self, label: str, dur: float, vec: np.ndarray, n: int = 1):
        for _ in range(n):
            self.rows.append({"start": self.t, "end": self.t + dur, "speaker": label, "text": "x"})
            self.vec[self.t] = vec() if callable(vec) else vec
            self.t += dur + 1.0
        return self

    def write(self, tmp_path):
        p = tmp_path / "rec_transcript_diarized.jsonl"
        p.write_text("\n".join(json.dumps(r) for r in self.rows), encoding="utf-8")
        return p

    def embed_fn(self, wav, sr, ranges):
        vs = [self.vec[s] for s, _ in ranges]
        return np.mean(vs, axis=0), sum(e - s for s, e in ranges)


@pytest.fixture(autouse=True)
def _no_audio(monkeypatch):
    monkeypatch.setattr(en, "load_wav_mono16k", lambda p: np.zeros(16000))


def run(tmp_path, rec, mapping, db=None, **kw):
    db = db or SpeakerDB(tmp_path / "speakers")
    res = en.enroll(tmp_path / "rec.m4a", rec.write(tmp_path), mapping, db, tmp_path,
                    embed_fn=rec.embed_fn, **kw)
    return res, db


# ── duplicate name ───────────────────────────────────────────────────────────

def test_same_name_on_two_labels_is_refused(tmp_path):
    vs = Voices()
    rec = Rec().add("SPEAKER_00", 10, lambda: vs.seg(0), 6).add("SPEAKER_01", 10, lambda: vs.seg(1), 6)
    res, db = run(tmp_path, rec, {"SPEAKER_00": "Ada", "SPEAKER_01": "Ada"})
    assert res["enrolled"] == []
    assert {r["label"] for r in res["refused"]} == {"SPEAKER_00", "SPEAKER_01"}
    assert all("--allow-duplicate" in r["reason"] for r in res["refused"])
    assert db.get_embedding("Ada") is None


def test_duplicate_override(tmp_path):
    vs = Voices()
    rec = Rec().add("SPEAKER_00", 10, lambda: vs.seg(0), 6).add("SPEAKER_01", 10, lambda: vs.seg(0), 6)
    res, _ = run(tmp_path, rec, {"SPEAKER_00": "Ada", "SPEAKER_01": "Ada"}, allow_duplicate=True)
    assert res["enrolled"] == ["Ada", "Ada"]
    assert res["refused"] == []


def test_name_already_on_another_label_of_the_recording(tmp_path):
    """Identification already put Ada on one cluster; naming another one Ada is
    the copied-label-number accident."""
    vs = Voices()
    rec = Rec().add("Ada", 10, lambda: vs.seg(0), 6).add("SPEAKER_01", 10, lambda: vs.seg(1), 6)
    res, _ = run(tmp_path, rec, {"SPEAKER_01": "Ada"})
    assert res["enrolled"] == []
    assert "already has a label named Ada" in res["refused"][0]["reason"]


def test_reinforcing_the_identified_label_itself_is_not_a_duplicate(tmp_path):
    vs = Voices()
    rec = Rec().add("Ada", 10, lambda: vs.seg(0), 6)
    res, _ = run(tmp_path, rec, {"Ada": "Ada"})
    assert res["enrolled"] == ["Ada"]


# ── similarity to the existing fingerprint ───────────────────────────────────

def _with_existing(tmp_path, cos):
    """Existing fingerprint F; the label's segments are all exactly at cos to F."""
    db = SpeakerDB(tmp_path / "speakers")
    f = np.zeros(DIM)
    f[0] = 1.0
    db.upsert_speaker("Ada", f, source="old", used_sec=100.0)
    x = at(f, cos, axis=1)
    rec = Rec().add("SPEAKER_00", 10, x, 6)
    return rec, db


def test_below_half_is_refused_with_what_was_compared(tmp_path):
    rec, db = _with_existing(tmp_path, 0.49)
    res, _ = run(tmp_path, rec, {"SPEAKER_00": "Ada"}, db=db)
    assert res["enrolled"] == []
    msg = res["refused"][0]["reason"]
    assert "existing fingerprint" in msg and "0.49" in msg and "--force" in msg
    assert res["checks"][0]["similarity"] == pytest.approx(0.49, abs=1e-3)
    assert db.load_index()["Ada"]["n_samples"] == 1


def test_just_above_half_is_enrolled(tmp_path):
    rec, db = _with_existing(tmp_path, 0.51)
    res, _ = run(tmp_path, rec, {"SPEAKER_00": "Ada"}, db=db)
    assert res["enrolled"] == ["Ada"]
    assert db.load_index()["Ada"]["n_samples"] == 2


def test_force_enrolls_and_still_records_the_score(tmp_path):
    rec, db = _with_existing(tmp_path, 0.2)
    res, _ = run(tmp_path, rec, {"SPEAKER_00": "Ada"}, db=db, force=True)
    assert res["enrolled"] == ["Ada"]
    assert res["checks"][0]["similarity"] == pytest.approx(0.2, abs=1e-3)


def test_new_name_has_nothing_to_compare(tmp_path):
    vs = Voices()
    rec = Rec().add("SPEAKER_00", 10, lambda: vs.seg(0), 6)
    res, _ = run(tmp_path, rec, {"SPEAKER_00": "Bo"})
    assert res["enrolled"] == ["Bo"] and res["checks"] == []


# ── what gets averaged ───────────────────────────────────────────────────────

def test_every_range_of_the_label_goes_into_one_average(tmp_path):
    """No selection step: the fingerprint is the average of the label's longest
    ranges, as it was before 0.2. (A selection step was built and removed; see
    the enroll module docstring for the measurement that removed it.)"""
    vs = Voices()
    rec = Rec().add("SPEAKER_00", 10, lambda: vs.seg(0), 6).add("SPEAKER_00", 2, lambda: vs.seg(0), 4)
    res, db = run(tmp_path, rec, {"SPEAKER_00": "Ada"})
    assert res["enrolled"] == ["Ada"]
    assert set(res) == {"enrolled", "refused", "checks"}
    assert db.load_index()["Ada"]["total_sec"] == pytest.approx(68.0, abs=0.1)


def test_enroll_takes_no_selection_switch():
    import inspect

    assert "enroll_all" not in inspect.signature(en.enroll).parameters
