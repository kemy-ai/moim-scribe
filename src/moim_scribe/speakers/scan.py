"""Post-hoc verification of speaker labels in a finished transcript.

Compares each labeled block of a diarized transcript against enrolled
fingerprints to flag (1) blocks whose label looks wrong and (2) mismatches
between an expected attendee roster and the labels actually present.

Principles this tool exists to enforce (docs/speaker-verification.md):
  - attendee count != cluster count; never assign names by elimination
  - speech from an un-enrolled speaker can be scattered across ALL labels,
    so after enrolling someone new, scan every label — not just one axis
  - ranges corrected via this scan must NOT be re-enrolled (circular
    reinforcement); only human-confirmed ranges feed the fingerprint DB
"""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path

import numpy as np

from .db import SpeakerDB
from .embed import extract_embedding_from_segments, load_wav_mono16k

LINE_RE = re.compile(r"^\[\s*([\d.]+)s\]\s*([^:]+):(.*)$")
MIN_BLOCK_SEC = 4.0
SEG_CAP_SEC = 15.0


def load_rows(txt_path: str | Path) -> list[tuple[float, str, str]]:
    rows = []
    for line in Path(txt_path).read_text(encoding="utf-8").splitlines():
        m = LINE_RE.match(unicodedata.normalize("NFC", line))
        if m:
            rows.append((float(m.group(1)), m.group(2).strip(), m.group(3).strip()))
    return rows


def to_blocks(rows: list[tuple[float, str, str]]) -> list[dict]:
    blocks, cur = [], None
    for i, (t, spk, tx) in enumerate(rows):
        end = min(rows[i + 1][0] if i + 1 < len(rows) else t + 5, t + SEG_CAP_SEC)
        if cur and cur["spk"] == spk:
            cur["segs"].append((t, end))
            cur["e"] = end
        else:
            if cur:
                blocks.append(cur)
            cur = {"spk": spk, "s": t, "e": end, "segs": [(t, end)], "first": tx}
    if cur:
        blocks.append(cur)
    return blocks


def _cos(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))


def scan(
    audio: str | Path,
    transcript: str | Path,
    db: SpeakerDB,
    model_dir: Path,
    axis: str | None = None,
    expected: str | None = None,
    margin: float = 0.03,
) -> int:
    """Print suspicious blocks and roster mismatches. Returns the suspect count."""
    rows = load_rows(transcript)
    embs = db.load_all_embeddings()
    labels_in_txt = {spk for _, spk, _ in rows}

    if expected:
        roster = {n.strip() for n in expected.split(",") if n.strip()}
        missing = roster - labels_in_txt
        unknown = {s for s in labels_in_txt if s not in roster and not s.startswith("SPEAKER")}
        if missing:
            print(f"WARNING roster names absent from transcript (possible scatter): {sorted(missing)}")
        if unknown:
            print(f"WARNING transcript names absent from roster (possible mislabel): {sorted(unknown)}")
        if not missing and not unknown:
            print("OK roster and transcript labels match")

    axes = [n.strip() for n in axis.split(",")] if axis else sorted(embs.keys())
    for a in axes:
        if a not in embs:
            raise SystemExit(f"axis speaker not enrolled: {a}")

    wav = load_wav_mono16k(audio)
    sr = 16000
    sus = 0
    for b in to_blocks(rows):
        if b["spk"] not in embs or sum(e - s for s, e in b["segs"]) < MIN_BLOCK_SEC:
            continue
        be, _ = extract_embedding_from_segments(wav, sr, b["segs"], model_dir, max_segments=20)
        if be is None:
            continue
        own = _cos(be, embs[b["spk"]])
        for a in axes:
            if a == b["spk"]:
                continue
            ca = _cos(be, embs[a])
            if ca > own + margin:
                mark = "STRONG" if ca - own >= 0.15 else ("LIKELY" if ca - own >= 0.08 else "WEAK")
                print(
                    f"{mark} {b['s']:.0f}s-{b['e']:.0f}s | current {b['spk']} {own:.3f} "
                    f"vs {a} {ca:.3f} | {b['first'][:55]}"
                )
                sus += 1
                break
    print(
        f"\n{sus} suspicious block(s) (margin {margin}+). "
        "Confirm via transcript evidence or a human, never from embeddings alone."
    )
    return sus
