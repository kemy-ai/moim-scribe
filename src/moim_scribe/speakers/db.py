"""Accumulating voice-fingerprint database.

Layout (inside a user-chosen data directory, never inside the repo):
  speakers/index.json   name -> {sources: [...], n_samples, total_sec, updated}
  speakers/<name>.npy   per-name mean embedding (192-d float32)

When the same person appears in multiple recordings, their fingerprint is
merged as a speech-seconds weighted average — this is what makes recurring
groups progressively more accurate.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from .embed import cosine_similarity

logger = logging.getLogger("moim_scribe.speakers.db")


def _safe_filename(name: str) -> str:
    """Only neutralize path separators — non-ASCII names and spaces are kept."""
    return name.replace("/", "_").replace("\\", "_").strip()


class SpeakerDB:
    """File-backed fingerprint store rooted at db_dir (e.g. <data_dir>/speakers)."""

    def __init__(self, db_dir: Path):
        self.db_dir = Path(db_dir)
        self.index_path = self.db_dir / "index.json"

    def _ensure_dir(self) -> None:
        self.db_dir.mkdir(parents=True, exist_ok=True)

    def load_index(self) -> dict:
        if not self.index_path.exists():
            return {}
        with open(self.index_path, encoding="utf-8") as f:
            return json.load(f)

    def save_index(self, index: dict) -> None:
        self._ensure_dir()
        # Atomic write: concurrent enrollments must not corrupt the index.
        tmp = self.index_path.with_name(self.index_path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.index_path)

    def load_all_embeddings(self) -> dict[str, np.ndarray]:
        """All enrolled speakers as {name: embedding}. Empty dict when none."""
        index = self.load_index()
        result = {}
        for name in index:
            p = self.db_dir / f"{_safe_filename(name)}.npy"
            if p.exists():
                result[name] = np.load(p)
        return result

    def get_embedding(self, name: str) -> np.ndarray | None:
        p = self.db_dir / f"{_safe_filename(name)}.npy"
        if not p.exists():
            return None
        return np.load(p)

    def upsert_speaker(
        self, name: str, new_embedding: np.ndarray, source: str, used_sec: float
    ) -> dict:
        """Enroll a new fingerprint, or merge with the existing one.

        Weighted average: (old_emb * old_sec + new_emb * new_sec) / (old_sec + new_sec).
        Returns the updated index entry.
        """
        self._ensure_dir()
        index = self.load_index()
        entry = index.get(name, {"sources": [], "n_samples": 0, "total_sec": 0.0, "updated": None})

        prev_emb = self.get_embedding(name)
        prev_sec = entry.get("total_sec", 0.0)

        if prev_emb is not None and prev_sec > 0:
            total_sec = prev_sec + used_sec
            merged = (prev_emb * prev_sec + new_embedding * used_sec) / total_sec
        else:
            total_sec = used_sec
            merged = new_embedding

        np.save(self.db_dir / f"{_safe_filename(name)}.npy", merged.astype(np.float32))

        entry["sources"].append({"source": source, "sec": round(used_sec, 1)})
        entry["n_samples"] = entry.get("n_samples", 0) + 1
        entry["total_sec"] = round(total_sec, 1)
        entry["updated"] = datetime.now(UTC).isoformat()

        index[name] = entry
        self.save_index(index)
        logger.info(
            f"fingerprint updated: {name} ({entry['total_sec']}s total, {entry['n_samples']} samples)"
        )
        return entry

    def identify(
        self, embedding: np.ndarray, threshold: float
    ) -> tuple[str | None, float, list[tuple[str, float]]]:
        """Compare an unknown embedding against every enrolled speaker.

        Returns (best name or None if below threshold, best score,
        all candidates as [(name, score), ...] sorted descending).
        """
        db = self.load_all_embeddings()
        if not db:
            return None, 0.0, []

        scored = [(name, cosine_similarity(embedding, emb)) for name, emb in db.items()]
        scored.sort(key=lambda x: x[1], reverse=True)
        best_name, best_score = scored[0]
        if best_score >= threshold:
            return best_name, best_score, scored
        return None, best_score, scored
