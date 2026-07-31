"""The anchor presence gate: every anchor must show segment-level evidence of
being in the recording, or the whole reassignment is withheld.

Why this exists: the anchor set is assembled by comparing against the whole
enrolment DB, so it can contain people who were never in the room — and the
reassignment pass is relative (0.20 floor + margin), so it always picks *some*
anchor. Upstream measurement showed the max-similarity axis cannot separate
attendees from absentees (lowest genuine 0.498 < highest absentee 0.634), but
the count of segments at >= 0.60 does: absentees peak at 0-1, attendees with
any real speech have 2+. The boundary cases (exactly 1 = withheld, exactly
2 = proceeds) are therefore the contract, not incidental values.

No torch, no models: embeddings are stubbed per segment, merge detection is
stubbed to nominate the anchors.
"""
from pathlib import Path
from unittest import mock

import numpy as np

from moim_scribe import diarize

BASE = np.array([1.0, 0.0])


def _vec_at(base: np.ndarray, cos_sim: float) -> np.ndarray:
    """A 2D unit vector at exactly cos_sim to base (algebraic, not approximate)."""
    theta = np.arccos(np.clip(cos_sim, -1.0, 1.0))
    unit = base / np.linalg.norm(base)
    orth = np.array([-unit[1], unit[0]])
    return np.cos(theta) * unit + np.sin(theta) * orth


FAR = _vec_at(BASE, 0.30)  # Bo's fingerprint: 0.30 to Ada — clean margins


def _run(voices):
    """Drive detect_and_fix_merged_speakers with one stubbed embedding per segment.

    Ada is identified (id_mapping), Bo arrives as a merge-suspect winner —
    the two routes into the anchor set. Segments are 5 s each, all labeled
    SPEAKER_00 so any relabeling is visible.
    """
    db = {"Ada": BASE, "Bo": FAR}
    merged = [
        {"start": float(i) * 5, "end": float(i) * 5 + 5.0, "speaker": "SPEAKER_00", "text": "t"}
        for i in range(len(voices))
    ]
    raw_labels = ["SPEAKER_00", "SPEAKER_01"] * (len(voices) // 2 + 1)
    suspects = {
        "SPEAKER_01": {"talk": {"Bo": 10.0}, "winners": ["Bo"], "avg_sim": {"Bo": 0.4}}
    }
    with mock.patch.object(diarize, "detect_cluster_merges", return_value=suspects), \
         mock.patch(
             "moim_scribe.speakers.embed.extract_embedding_from_segments",
             side_effect=list(voices),  # each element is already an (emb, sec) tuple
         ):
        merged_out, info = diarize.detect_and_fix_merged_speakers(
            None, 16000, merged, raw_labels[: len(voices)],
            {"SPEAKER_00": "Ada"}, db, Path("/nonexistent-models"),
        )
    return merged_out, info


def test_all_anchors_confirmed_proceeds():
    """Two evidence segments each -> the gate does not block a normal repair."""
    _, info = _run([(BASE, 5.0), (BASE, 5.0), (FAR, 5.0), (FAR, 5.0)])
    assert info["triggered"] is True
    assert "presence_unconfirmed" not in info


def test_one_unconfirmed_anchor_withholds_everything():
    """Every segment is Ada's voice -> Bo has zero evidence -> withheld, labels kept.

    This is the exact scenario the gate exists for: an anchor nominated by
    whole-DB comparison who was never in the room."""
    merged_out, info = _run([(BASE, 5.0)] * 4)
    assert info["triggered"] is False
    assert info["presence_unconfirmed"] == ["Bo"]
    assert [m["speaker"] for m in merged_out] == ["SPEAKER_00"] * 4, (
        "withheld, yet labels changed — the gate did not run before application"
    )
    assert info["anchors"] == ["Ada", "Bo"], "anchors must stay recorded for the reason trail"


def test_boundary_one_evidence_segment_withholds():
    """Exactly 1 evidence segment = withheld. Absentees reach 1 by chance
    (measured ceiling); the line is between 1 and 2."""
    _, info = _run([(BASE, 5.0), (BASE, 5.0), (FAR, 5.0)])
    assert info["triggered"] is False
    assert info["presence_unconfirmed"] == ["Bo"]


def test_boundary_exactly_min_segs_passes():
    """Exactly ANCHOR_PRESENCE_MIN_SEGS (2) = proceeds (>= boundary)."""
    _, info = _run([(BASE, 5.0), (BASE, 5.0), (BASE, 5.0), (FAR, 5.0), (FAR, 5.0)])
    assert info["stats"]["anchor_presence"] == {"Ada": 3, "Bo": 2}
    assert info["triggered"] is True


def test_below_min_sim_is_not_evidence():
    """Segments at 0.59 never count, however many — the 0.60 boundary is the
    measured separation line, and absentees pile up 0.55-0.59 hits."""
    near_miss = _vec_at(FAR, 0.59)
    _, info = _run([(BASE, 5.0), (BASE, 5.0), (near_miss, 5.0), (near_miss, 5.0), (near_miss, 5.0)])
    assert info["triggered"] is False
    assert info["presence_unconfirmed"] == ["Bo"]


def test_second_place_evidence_counts():
    """Rank does not matter, only the 0.60 line: a voice leaning toward both
    anchors (0.65 to Ada while Bo wins the segment) is still Ada's presence
    evidence. Counting winners only would recreate the statistic the
    separation line was NOT measured on."""
    lean = _vec_at(BASE, 0.65)  # rotates toward FAR: ~0.92 to Bo, 0.65 to Ada
    _, info = _run([(lean, 5.0), (lean, 5.0), (FAR, 5.0), (FAR, 5.0)])
    assert info["stats"]["anchor_presence"]["Ada"] == 2, (
        "runner-up evidence was dropped — different statistic, unfounded separation line"
    )
    assert info["triggered"] is True
