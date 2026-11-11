"""Enroll speaker fingerprints from a diarized recording.

Takes the transcript_diarized.jsonl produced by the pipeline, collects the
speech ranges of the given SPEAKER_NN labels, embeds them, and upserts each
into the fingerprint DB under a real name (weighted-average accumulation).

Enrollment discipline (see docs/speaker-verification.md): only enroll
ranges whose speaker identity a human has confirmed. Never re-enroll ranges
that were themselves corrected by automated scanning — that feeds the
model its own guesses (circular reinforcement).

Two guards and one selection step stand between a mapping and the database,
because a wrong enrollment is silent: it does not fail, it makes every later
recording slightly more wrong.

  - Duplicate name: one name on two labels of the same recording is refused
    (--allow-duplicate overrides). The usual cause is a label number copied
    from a different part or a different run of the same meeting.
  - Fingerprint similarity: when the name already has a fingerprint, a label
    whose embedding is below CONFIRM_MIN_SIMILARITY to it is refused (--force
    overrides). Measured upstream, wrong-label confirmations that polluted a
    fingerprint scored 0.24 and 0.31. The value is deliberately low: this is a
    check on a human decision, not an automatic match, so wrongly blocking a
    correct confirmation costs more. It is not a safety line either way.
  - Segment selection (speakers/select.py): a label that looks like more than
    one voice is refused; otherwise only the segments consistent with the
    label's stable core are enrolled. --enroll-all restores the old behaviour.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path

import numpy as np

from .db import SpeakerDB
from .embed import (
    MIN_SEGMENT_SEC,
    cosine_similarity,
    extract_embedding_from_segments,
    load_wav_mono16k,
)
from .select import STABLE_MIN_SEC, select_segments

logger = logging.getLogger("moim_scribe.speakers.enroll")

MIN_TOTAL_SEC_WARN = 20.0
MAX_SEGMENTS_PER_SPEAKER = 40      # --enroll-all: average the N longest ranges
MAX_SELECT_SEGMENTS = 400          # selection embeds ranges one by one; cap for long recordings
CONFIRM_MIN_SIMILARITY = 0.5       # see the module docstring - a sanity check, not a safety line


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


def _duplicate_refusals(mapping: dict[str, str], labels_in_file: set[str]) -> dict[str, str]:
    """{label: reason} for every pair the duplicate-name guard refuses."""
    refused: dict[str, str] = {}
    by_name: dict[str, list[str]] = {}
    for spk, name in mapping.items():
        by_name.setdefault(name, []).append(spk)
    for spk, name in mapping.items():
        others = [o for o in by_name[name] if o != spk]
        if others:
            refused[spk] = (
                f"{name} is also given to {', '.join(others)} in this command - one "
                "recording, two labels, one name"
            )
        elif name in labels_in_file and name != spk:
            refused[spk] = (
                f"this recording already has a label named {name} (identification "
                "put that name on another cluster)"
            )
    return refused


def _embed_all(wav, sr, segments, embed_fn):
    """The pre-selection behaviour: one average over the longest ranges."""
    valid = [(s, e) for s, e in segments if (e - s) >= MIN_SEGMENT_SEC]
    valid = sorted(valid, key=lambda se: se[1] - se[0], reverse=True)[:MAX_SEGMENTS_PER_SPEAKER]
    if not valid:
        return None, 0.0
    return embed_fn(wav, sr, valid)


def _embed_selected(wav, sr, segments, embed_fn, label: str):
    """Embed ranges one by one, then judge and select (speakers/select.py).

    Returns (embedding | None, seconds, selection-info). A "mixed" decision
    returns no embedding; "too_short" falls back to _embed_all.
    """
    valid = [(s, e) for s, e in segments if (e - s) >= MIN_SEGMENT_SEC]
    valid = sorted(valid, key=lambda se: se[1] - se[0], reverse=True)[:MAX_SELECT_SEGMENTS]
    embs, secs = [], []
    for s, e in valid:
        v, used = embed_fn(wav, sr, [(s, e)])
        if v is not None:
            embs.append(v)
            secs.append(float(used))
    if not embs:
        return None, 0.0, {"decision": "no_ranges"}
    info = select_segments(embs, secs)
    if info["decision"] == "too_short":
        logger.warning(
            f"{label}: only {info['n_stable']} ranges of {STABLE_MIN_SEC:g} s or more - too few to judge "
            "whether this label is one voice, so every range is enrolled as before"
        )
        emb, used = _embed_all(wav, sr, segments, embed_fn)
        return emb, used, info
    if info["decision"] == "mixed":
        return None, 0.0, info
    keep = info["keep"]
    if not keep:
        return None, 0.0, info
    emb = np.mean(np.stack([np.asarray(embs[i], dtype=float).flatten() for i in keep]), axis=0)
    return emb, sum(secs[i] for i in keep), info


def enroll(
    audio: str | Path,
    jsonl: str | Path,
    mapping: dict[str, str],
    db: SpeakerDB,
    model_dir: Path,
    *,
    allow_duplicate: bool = False,
    force: bool = False,
    enroll_all: bool = False,
    embed_fn: Callable | None = None,
) -> dict:
    """Embed each mapped speaker's ranges and upsert into the fingerprint DB.

    Returns {"enrolled": [names], "refused": [{"label", "name", "kind", "reason"}],
    "checks": [{"label", "name", "similarity"}], "selection": {label: info}}.
    A refused pair is skipped; the others still go in. The similarity is
    recorded even under --force, so "how different was it?" stays answerable.
    embed_fn(wav, sr, ranges) -> (embedding | None, seconds) is injectable for
    tests; it defaults to the ECAPA model in model_dir.
    """
    if embed_fn is None:
        def embed_fn(w, rate, ranges):
            return extract_embedding_from_segments(w, rate, ranges, model_dir)

    segs = load_segments(jsonl)
    if not segs:
        raise SystemExit("no segments in jsonl")
    result: dict = {"enrolled": [], "refused": [], "checks": [], "selection": {}}

    def refuse(spk: str, name: str, kind: str, reason: str, override: str) -> None:
        msg = f"{spk} -> {name} not enrolled ({kind}): {reason}. {override}"
        logger.error(msg)
        result["refused"].append({"label": spk, "name": name, "kind": kind, "reason": msg})

    by_speaker: dict[str, list[tuple[float, float]]] = {spk: [] for spk in mapping}
    for s in segs:
        spk = s.get("speaker")
        if spk in by_speaker:
            by_speaker[spk].append((float(s["start"]), float(s["end"])))

    accepted = dict(mapping)
    if not allow_duplicate:
        labels_in_file = {str(s.get("speaker")) for s in segs}
        for spk, reason in _duplicate_refusals(mapping, labels_in_file).items():
            refuse(spk, mapping[spk], "duplicate name", reason,
                   "If it really is one person split across two labels, rerun with "
                   "--allow-duplicate.")
            del accepted[spk]

    for spk, name in accepted.items():
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
    if not any(by_speaker[spk] for spk in accepted):
        return result

    logger.info("loading audio (16 kHz mono)…")
    wav = load_wav_mono16k(audio)
    sr = 16000
    logger.info(f"audio length {len(wav) / sr:.1f}s")

    source_label = Path(audio).stem

    for spk, name in accepted.items():
        segments = by_speaker[spk]
        if not segments:
            continue
        if enroll_all:
            logger.info(f"{spk} -> {name}: embedding (top {MAX_SEGMENTS_PER_SPEAKER} longest ranges)…")
            emb, used_sec = _embed_all(wav, sr, segments, embed_fn)
        else:
            logger.info(f"{spk} -> {name}: embedding ranges one by one for selection…")
            emb, used_sec, info = _embed_selected(wav, sr, segments, embed_fn, spk)
            result["selection"][spk] = info
            if info["decision"] == "mixed":
                refuse(spk, name, "label looks mixed",
                       f"{info['outlier_fraction'] * 100:.0f}% of its {info['n_stable']} longest "
                       "ranges do not resemble the rest (one voice is usually under 20%), so "
                       "part of it is probably someone else",
                       "Listen to the label. If it really is one person, rerun with "
                       "--enroll-all; otherwise enroll this person from another recording.")
                continue
            if info["decision"] == "selected":
                logger.info(
                    f"{spk}({name}): kept {len(info['keep'])}/{info['n_segments']} ranges "
                    f"({info['kept_sec']}s of {info['total_sec']}s) consistent with the "
                    "label's core"
                )
        if emb is None:
            logger.warning(f"{spk}({name}): no usable ranges (>=1s) — not enrolled")
            continue

        existing = db.get_embedding(name)
        if existing is not None:
            sim = cosine_similarity(np.asarray(emb), existing)
            result["checks"].append({"label": spk, "name": name, "similarity": round(sim, 3)})
            if sim < CONFIRM_MIN_SIMILARITY and not force:
                refuse(spk, name, "voice does not match",
                       f"compared the ranges about to be enrolled with {name}'s existing "
                       f"fingerprint: similarity {sim:.2f}, below {CONFIRM_MIN_SIMILARITY} "
                       "(the same person usually scores 0.7 or more). Check the label number",
                       "If you have listened and it is this person (phone audio and thin "
                       "fingerprints can score this low), rerun with --force.")
                continue

        entry = db.upsert_speaker(name, emb, source=source_label, used_sec=used_sec)
        result["enrolled"].append(name)
        logger.info(
            f"{name} enrolled — {used_sec:.1f}s from this source, "
            f"{entry['total_sec']}s total across {entry['n_samples']} samples"
        )

    logger.info(
        "enrollment done."
        + (f" {len(result['refused'])} refused - see above." if result["refused"] else "")
    )
    return result
