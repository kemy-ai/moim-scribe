"""Enroll speaker fingerprints from a diarized recording.

Takes the transcript_diarized.jsonl produced by the pipeline, collects the
speech ranges of the given SPEAKER_NN labels, embeds them, and upserts each
into the fingerprint DB under a real name (weighted-average accumulation).

Enrollment discipline (see docs/speaker-verification.md): only enroll
ranges whose speaker identity a human has confirmed. Never re-enroll ranges
that were themselves corrected by automated scanning — that feeds the
model its own guesses (circular reinforcement).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .db import SpeakerDB
from .embed import extract_embedding_from_segments, load_wav_mono16k

logger = logging.getLogger("moim_scribe.speakers.enroll")

MIN_TOTAL_SEC_WARN = 20.0
MAX_SEGMENTS_PER_SPEAKER = 40


def parse_mapping(args: list[str]) -> dict[str, str]:
    """['SPEAKER_00=Alice', ...] -> {'SPEAKER_00': 'Alice'}"""
    mapping = {}
    for a in args:
        if "=" not in a:
            raise ValueError(f"bad mapping '{a}' (expected SPEAKER_NN=Name)")
        spk, name = a.split("=", 1)
        spk, name = spk.strip(), name.strip()
        if not spk or not name:
            raise ValueError(f"bad mapping '{a}'")
        mapping[spk] = name
    return mapping


def load_segments(jsonl_path: str | Path) -> list[dict]:
    segs = []
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                segs.append(json.loads(line))
    return segs


def enroll(
    audio: str | Path,
    jsonl: str | Path,
    mapping: dict[str, str],
    db: SpeakerDB,
    model_dir: Path,
) -> None:
    """Embed each mapped speaker's ranges and upsert into the fingerprint DB."""
    segs = load_segments(jsonl)
    if not segs:
        raise SystemExit("no segments in jsonl")

    by_speaker: dict[str, list[tuple[float, float]]] = {spk: [] for spk in mapping}
    for s in segs:
        spk = s.get("speaker")
        if spk in by_speaker:
            by_speaker[spk].append((float(s["start"]), float(s["end"])))

    for spk, name in mapping.items():
        n_segs = len(by_speaker[spk])
        total = sum(e - s for s, e in by_speaker[spk])
        if n_segs == 0:
            logger.warning(f"{spk}({name}): no ranges found in jsonl — skipping")
        else:
            logger.info(f"{spk}({name}): {n_segs} ranges, {total:.1f}s total")
            if total < MIN_TOTAL_SEC_WARN:
                logger.warning(
                    f"{spk}({name}): only {total:.1f}s of speech — "
                    f"below {MIN_TOTAL_SEC_WARN}s, fingerprint quality may be low"
                )

    logger.info("loading audio (16 kHz mono)…")
    wav = load_wav_mono16k(audio)
    sr = 16000
    logger.info(f"audio length {len(wav) / sr:.1f}s")

    source_label = Path(audio).stem

    for spk, name in mapping.items():
        segments = by_speaker[spk]
        if not segments:
            continue
        logger.info(f"{spk} -> {name}: embedding (top {MAX_SEGMENTS_PER_SPEAKER} longest ranges)…")
        emb, used_sec = extract_embedding_from_segments(
            wav, sr, segments, model_dir, max_segments=MAX_SEGMENTS_PER_SPEAKER
        )
        if emb is None:
            logger.warning(f"{spk}({name}): no usable ranges (>=1s) — not enrolled")
            continue
        entry = db.upsert_speaker(name, emb, source=source_label, used_sec=used_sec)
        logger.info(
            f"{name} enrolled — {used_sec:.1f}s from this source, "
            f"{entry['total_sec']}s total across {entry['n_samples']} samples"
        )

    logger.info("enrollment done.")
