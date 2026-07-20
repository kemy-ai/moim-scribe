"""SpeakerDB: weighted-average accumulation and identification."""
import json

import numpy as np
import pytest

from moim_scribe.speakers.db import SpeakerDB, _safe_filename


@pytest.fixture
def db(tmp_path):
    return SpeakerDB(tmp_path / "speakers")


def test_empty_db(db):
    assert db.load_index() == {}
    assert db.load_all_embeddings() == {}
    name, score, scored = db.identify(np.ones(4, dtype=np.float32), threshold=0.5)
    assert name is None and score == 0.0 and scored == []


def test_first_enroll_stores_embedding(db):
    emb = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    entry = db.upsert_speaker("Alice", emb, source="rec1", used_sec=30.0)
    assert entry["n_samples"] == 1 and entry["total_sec"] == 30.0
    stored = db.get_embedding("Alice")
    assert np.allclose(stored, emb)


def test_accumulation_is_seconds_weighted(db):
    a = np.array([1.0, 0.0], dtype=np.float32)
    b = np.array([0.0, 1.0], dtype=np.float32)
    db.upsert_speaker("Bob", a, source="rec1", used_sec=30.0)
    entry = db.upsert_speaker("Bob", b, source="rec2", used_sec=10.0)
    # (a*30 + b*10) / 40 = [0.75, 0.25]
    stored = db.get_embedding("Bob")
    assert np.allclose(stored, [0.75, 0.25])
    assert entry["total_sec"] == 40.0 and entry["n_samples"] == 2
    assert [s["source"] for s in entry["sources"]] == ["rec1", "rec2"]


def test_identify_threshold_gate(db):
    db.upsert_speaker("Alice", np.array([1.0, 0.0], dtype=np.float32), "r", 20.0)
    db.upsert_speaker("Bob", np.array([0.0, 1.0], dtype=np.float32), "r", 20.0)
    probe = np.array([0.9, 0.1], dtype=np.float32)
    name, score, scored = db.identify(probe, threshold=0.6)
    assert name == "Alice" and score > 0.9
    assert [n for n, _ in scored] == ["Alice", "Bob"]
    name, score, _ = db.identify(probe, threshold=0.999)
    assert name is None and score > 0.9  # below threshold: no name, score still reported


def test_non_ascii_names_and_path_safety(db):
    emb = np.ones(3, dtype=np.float32)
    db.upsert_speaker("김하늘", emb, "r", 10.0)
    db.upsert_speaker("evil/../name", emb, "r", 10.0)
    assert db.get_embedding("김하늘") is not None
    assert set(db.load_all_embeddings()) == {"김하늘", "evil/../name"}
    for p in db.db_dir.glob("*.npy"):
        assert ".." not in p.name.split("_")[0] or "/" not in p.name


def test_safe_filename():
    assert _safe_filename("a/b\\c") == "a_b_c"
    assert _safe_filename(" 김하늘 ") == "김하늘"


def test_index_is_valid_json_after_writes(db):
    db.upsert_speaker("X", np.ones(2, dtype=np.float32), "r", 5.0)
    raw = json.loads(db.index_path.read_text(encoding="utf-8"))
    assert "X" in raw
