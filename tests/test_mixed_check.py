"""Two-voice suspicion: display only, never fatal, boundaries pinned.

No torch: the embedder is injected. Each block is one segment, separated by
more than BLOCK_GAP_SEC so every segment is its own block, and the fake
embedder returns a voice vector chosen by the block's start time.
"""
import json
from pathlib import Path

import numpy as np
import pytest

from moim_scribe import diarize
from moim_scribe.diarize import write_outputs
from moim_scribe.speaker_stats import strip_diagnostic_keys
from moim_scribe.speakers import mixed

A = np.array([1.0, 0.0])


def _at(cos_sim: float) -> np.ndarray:
    theta = np.arccos(cos_sim)
    return np.array([np.cos(theta), np.sin(theta)])


def _fixture(voices, block_sec=13.0, label="SPEAKER_00"):
    """One segment per voice, 3 s apart (> BLOCK_GAP_SEC), all one raw label."""
    merged, starts = [], {}
    t = 0.0
    for v in voices:
        merged.append({"start": t, "end": t + block_sec, "speaker": label, "text": "x"})
        starts[t] = v
        t += block_sec + 3.0
    raw = [m["speaker"] for m in merged]

    def embed_fn(wav, sr, segs):
        s, e = segs[0]
        return starts[s], e - s

    return merged, raw, embed_fn


def test_two_voices_flagged():
    merged, raw, fn = _fixture([A] * 6 + [_at(0.5)] * 3)
    info = mixed.check_mixed_clusters(None, 16000, merged, raw, fn)
    assert info["checked"] == 1
    sus = info["suspects"]["SPEAKER_00"]
    assert sus["centroid_sim"] == pytest.approx(0.5, abs=1e-3)
    assert sus["minor_share"] == pytest.approx(3 / 9, abs=1e-3)
    assert info["error"] is None


def test_one_voice_not_flagged_but_measured():
    merged, raw, fn = _fixture([A] * 8)
    info = mixed.check_mixed_clusters(None, 16000, merged, raw, fn)
    assert info["suspects"] == {}
    assert "SPEAKER_00" in info["metrics"]


def test_exactly_five_long_blocks_is_checked_four_is_skipped():
    merged, raw, fn = _fixture([A] * 3 + [_at(0.3)] * 2)
    info = mixed.check_mixed_clusters(None, 16000, merged, raw, fn)
    assert info["checked"] == 1
    assert "SPEAKER_00" in info["suspects"]

    merged, raw, fn = _fixture([A] * 2 + [_at(0.3)] * 2, block_sec=15.0)
    info = mixed.check_mixed_clusters(None, 16000, merged, raw, fn)
    assert info["checked"] == 0
    assert "long blocks" in info["skipped"]["SPEAKER_00"]


def test_blocks_under_eight_seconds_do_not_count():
    """7.9 s blocks are not long blocks, however many there are."""
    merged, raw, fn = _fixture([A] * 6 + [_at(0.3)] * 6, block_sec=7.9)
    info = mixed.check_mixed_clusters(None, 16000, merged, raw, fn)
    assert info["checked"] == 0


def test_minor_share_boundary_ten_percent():
    # 9 blocks vs 1 block of equal length: share exactly 0.10 -> flagged.
    merged, raw, fn = _fixture([A] * 9 + [_at(0.3)])
    info = mixed.check_mixed_clusters(None, 16000, merged, raw, fn)
    assert info["metrics"]["SPEAKER_00"]["minor_share"] == 0.1
    assert "SPEAKER_00" in info["suspects"]
    # 10 vs 1: share 0.0909 -> not flagged, although clearly two vectors.
    merged, raw, fn = _fixture([A] * 10 + [_at(0.3)])
    info = mixed.check_mixed_clusters(None, 16000, merged, raw, fn)
    assert info["suspects"] == {}


def test_centroid_similarity_boundary():
    merged, raw, fn = _fixture([A] * 5 + [_at(0.69)] * 3)
    assert "SPEAKER_00" in mixed.check_mixed_clusters(None, 16000, merged, raw, fn)["suspects"]
    merged, raw, fn = _fixture([A] * 5 + [_at(0.71)] * 3)
    assert mixed.check_mixed_clusters(None, 16000, merged, raw, fn)["suspects"] == {}


def test_labels_are_never_changed():
    merged, raw, fn = _fixture([A] * 6 + [_at(0.3)] * 3)
    before = json.loads(json.dumps(merged))
    mixed.check_mixed_clusters(None, 16000, merged, raw, fn)
    assert merged == before


def test_sub_own_sim_puts_majority_first():
    merged, raw, fn = _fixture([A] * 6 + [_at(0.3)] * 3)
    info = mixed.check_mixed_clusters(None, 16000, merged, raw, fn,
                                      id_mapping={"SPEAKER_00": "Ada"}, db={"Ada": A})
    assert info["suspects"]["SPEAKER_00"]["label"] == "Ada"
    assert info["suspects"]["SPEAKER_00"]["sub_own_sim"] == [1.0, 0.3]


def test_embedder_failure_is_captured_not_raised():
    merged, raw, _ = _fixture([A] * 6)

    def boom(*a):
        raise RuntimeError("model gone")

    info = mixed.check_mixed_clusters(None, 16000, merged, raw, boom)
    assert "model gone" in info["error"]
    assert any("failed" in ln for ln in mixed.format_suspect_lines(info))


# ── run_mixed_check: the diarize-side wrapper both engines go through ────────

@pytest.mark.parametrize("exc", [ValueError("bad"), SystemExit("must not escape")])
def test_run_mixed_check_absorbs_failures(monkeypatch, exc):
    """SystemExit included: on the cloud path it would escape the watch loop."""
    monkeypatch.setattr(mixed, "check_mixed_clusters",
                        lambda *a, **k: (_ for _ in ()).throw(exc))
    merged, raw, _ = _fixture([A] * 6)
    assert diarize.run_mixed_check(np.zeros(10), 16000, merged, raw, {}, {}, Path(".")) is None


def test_run_mixed_check_skips_without_audio():
    merged, raw, _ = _fixture([A] * 6)
    assert diarize.run_mixed_check(None, 16000, merged, raw, {}, {}, Path(".")) is None


def test_run_mixed_check_env_switch(monkeypatch):
    called = []
    monkeypatch.setattr(mixed, "check_mixed_clusters", lambda *a, **k: called.append(1) or {})
    monkeypatch.setenv("MOIM_SCRIBE_MIXED_CHECK", "0")
    merged, raw, _ = _fixture([A] * 6)
    assert diarize.run_mixed_check(np.zeros(10), 16000, merged, raw, {}, {}, Path(".")) is None
    assert called == []


def test_identify_and_repair_carries_the_result(monkeypatch, tmp_path):
    """Both engines call identify_and_repair; the result rides on merge_info."""
    class DB:
        def load_all_embeddings(self):
            return {"Ada": A}

    monkeypatch.setattr("moim_scribe.speakers.embed.load_wav_mono16k", lambda p: np.zeros(10))
    monkeypatch.setattr(diarize, "identify_speakers", lambda wav, sr, merged, db, md: (merged, {}, {}))
    monkeypatch.setattr(mixed, "check_mixed_clusters",
                        lambda *a, **k: {"checked": 1, "suspects": {}, "metrics": {}})
    merged, raw, _ = _fixture([A] * 6)
    *_, info = diarize.identify_and_repair("a.wav", merged, raw, DB(), tmp_path)
    assert info["mixed_check"]["checked"] == 1
    assert info["mixed_check"]["reassigned"] is False


# ── output + prompt filter ────────────────────────────────────────────────────

SUSPECT_INFO = {
    "checked": 1,
    "suspects": {"SPEAKER_00": {"label": "Ada", "n_long_blocks": 9, "minor_share": 0.3,
                                "minor_sec": 40.0, "centroid_sim": 0.5, "sub_own_sim": None}},
    "skipped": {}, "metrics": {}, "error": None, "params": {}, "reassigned": False,
}


def test_write_outputs_flags_the_label_and_keeps_the_block(tmp_path):
    merged = [
        {"start": 0.0, "end": 10.0, "speaker": "Ada", "text": "a"},
        {"start": 10.0, "end": 20.0, "speaker": "SPEAKER_01", "text": "b"},
    ]
    *_, data = write_outputs(str(tmp_path), "r_", merged, {"SPEAKER_00": "Ada"}, {},
                             {"triggered": False, "suspects": {}, "anchors": [], "stats": {}},
                             mixed_info=SUSPECT_INFO)
    by = {s["label"]: s for s in data["speakers"]}
    assert by["Ada"]["mixed_suspect_diag"] is True
    assert "mixed_suspect_diag" not in by["SPEAKER_01"]
    assert data["mixed_check_diag"]["checked"] == 1
    # merge_reassign is an allow-list: mixed_check on merge_info never leaks there.
    assert "merge_reassign" not in data


def test_prompt_filter_drops_both_mixed_fields(tmp_path):
    merged = [{"start": 0.0, "end": 10.0, "speaker": "Ada", "text": "a"}]
    *_, data = write_outputs(str(tmp_path), "r_", merged, {"SPEAKER_00": "Ada"}, {},
                             {"triggered": False, "suspects": {}, "anchors": [], "stats": {}},
                             mixed_info=SUSPECT_INFO)
    out = strip_diagnostic_keys(data)
    assert "mixed_check_diag" not in out
    assert all("mixed_suspect_diag" not in s for s in out["speakers"])
