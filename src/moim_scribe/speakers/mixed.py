"""Two-voice suspicion: flag a raw cluster that looks like two people. Display only.

Why this exists
  When pyannote puts two people into one cluster, cluster-mean identification
  names the cluster after whoever holds the larger share, and every word the
  other person said goes out under that name. Merge detection
  (diarize.detect_cluster_merges) only catches the case where the cluster's
  segments split between **two or more enrolled** people. Two shapes slip past
  it, both observed upstream:
    - an enrolled person plus a not-yet-enrolled one (the newcomer's segments
      fall nearest to the same enrolled person, so there is only one "winner");
    - three people whose shares each sit under the 25% merge threshold.
  Cluster-mean similarity does not reveal either: upstream, a cluster that was
  roughly 55/45 two people still matched its majority speaker at 0.84.

What it measures
  Per raw cluster, take the **long blocks** only (runs of the same label,
  >= 8 s of speech), embed each, split them two ways (average-linkage on
  cosine), and flag the cluster when
      minority share (by seconds) >= 0.10  AND  centroid similarity <= 0.70.
  Calibrated upstream on 28 recordings / 99 clusters: every known mixed
  cluster was caught, and the extra clusters flagged at the 0.10 share all
  looked like real contamination on inspection. Two details matter:
    - Long blocks only. Measured over every block >= 4 s, a known mixed cluster
      lost its signal (the minority shrank to a single outlier).
    - 0.70, not tighter. Two similar voices in a known mixed cluster sat at
      0.65 apart; 0.65 would have missed it.
  The share floor was lowered from 0.15 to 0.10 after a live case with four
  voices in one cluster came out at 0.147.

What it does not do
  - **It never changes a label.** It returns a dict; the caller logs it and
    writes it to speakers.json as a diagnostic. A judgement that is still being
    observed belongs in a display, not in a gate that silently renames people.
  - It never raises. Any failure ends up in result["error"] and the run goes on.
  - Cost: one embedding per long block (tens to a few hundred per recording;
    about a minute for a long one upstream).

Return schema (written as the top-level `mixed_check_diag` block; the names it
can contain are only the labels already on those clusters):
  {"checked": int, "suspects": {raw: {"label", "n_long_blocks", "minor_share",
   "minor_sec", "centroid_sim", "sub_own_sim": [major, minor] | None}},
   "skipped": {raw: reason}, "metrics": {raw: {...}}, "error": str | None,
   "params": {...}}
"""
from __future__ import annotations

from collections.abc import Callable

import numpy as np

# Gate constants - see the module docstring for where each value comes from.
MIXED_LONG_BLOCK_SEC = 8.0      # a "long block" is at least this much speech
MIXED_MIN_LONG_BLOCKS = 5       # fewer than this and a two-way split means nothing
MIXED_MIN_CLUSTER_SEC = 60.0    # clusters with less total speech are skipped
MIXED_MINOR_SHARE_MIN = 0.10    # minority group's share of seconds
MIXED_CENTROID_SIM_MAX = 0.70   # at or below this the two centroids are two voices
BLOCK_GAP_SEC = 2.0             # same-label segments closer than this form one block
SEG_CAP_SEC = 15.0              # per-segment cap, matching the calibration

EmbedFn = Callable[[object, int, list], tuple]


def _blocks_for_label(merged: list, raw_labels: list, label: str) -> list:
    """Group consecutive same-label segments (gap <= BLOCK_GAP_SEC) into blocks.

    Each segment end is capped at start + SEG_CAP_SEC so embedding lengths
    match the calibration.
    """
    blocks, cur = [], None
    for m, raw in zip(merged, raw_labels, strict=False):
        if raw != label:
            if cur:
                blocks.append(cur)
                cur = None
            continue
        s = float(m["start"])
        e = min(float(m["end"]), s + SEG_CAP_SEC)
        if e <= s:
            continue
        if cur and s - cur["e"] <= BLOCK_GAP_SEC:
            cur["segs"].append((s, e))
            cur["e"] = e
        else:
            if cur:
                blocks.append(cur)
            cur = {"s": s, "e": e, "segs": [(s, e)]}
    if cur:
        blocks.append(cur)
    for b in blocks:
        b["dur"] = sum(e - s for s, e in b["segs"])
    return blocks


def agglo2(E: np.ndarray) -> list[list[int]]:
    """Two groups by average-linkage merging on cosine similarity.

    E must be L2-normalised row-wise (E @ E.T is used as cosine). Pure numpy -
    the input is at most a few hundred rows. Returns [[idx...], [idx...]].
    """
    n = len(E)
    if n < 2:
        return [list(range(n))]
    S = E @ E.T
    clusters = [[i] for i in range(n)]
    while len(clusters) > 2:
        best, bi, bj = -2.0, -1, -1
        for a in range(len(clusters)):
            for b in range(a + 1, len(clusters)):
                v = float(S[np.ix_(clusters[a], clusters[b])].mean())
                if v > best:
                    best, bi, bj = v, a, b
        clusters[bi] = clusters[bi] + clusters[bj]
        del clusters[bj]
    return clusters


def _unit(v) -> np.ndarray | None:
    v = np.asarray(v, dtype=float).flatten()
    n = float(np.linalg.norm(v))
    if n == 0 or not np.isfinite(n):
        return None
    return v / n


def _params() -> dict:
    return {
        "long_block_sec": MIXED_LONG_BLOCK_SEC,
        "min_long_blocks": MIXED_MIN_LONG_BLOCKS,
        "min_cluster_sec": MIXED_MIN_CLUSTER_SEC,
        "minor_share_min": MIXED_MINOR_SHARE_MIN,
        "centroid_sim_max": MIXED_CENTROID_SIM_MAX,
    }


def check_mixed_clusters(
    wav,
    sr: int,
    merged: list,
    raw_labels: list,
    embed_fn: EmbedFn,
    id_mapping: dict | None = None,
    db: dict | None = None,
) -> dict:
    """Judge every raw cluster for two voices. Labels are never touched.

    embed_fn(wav, sr, segments) -> (embedding | None, seconds used). Injected so
    tests can run without a model.
    id_mapping {raw: name} and db {name: embedding} are optional and only add
    the "sub_own_sim" diagnostic (each half's similarity to the name's
    fingerprint).
    """
    result: dict = {"checked": 0, "suspects": {}, "skipped": {}, "metrics": {},
                    "error": None, "params": _params()}
    try:
        id_mapping = id_mapping or {}
        db = db or {}
        if not merged or not raw_labels or len(merged) != len(raw_labels):
            result["error"] = "empty input or merged/raw_labels length mismatch"
            return result
        totals: dict = {}
        for m, raw in zip(merged, raw_labels, strict=True):
            totals[raw] = totals.get(raw, 0.0) + max(0.0, float(m["end"]) - float(m["start"]))
        for raw, tot in totals.items():
            _check_one(result, wav, sr, merged, raw_labels, raw, tot, embed_fn, id_mapping, db)
    except Exception as e:  # noqa: BLE001 - a display step must never stop a run
        result["error"] = f"{type(e).__name__}: {e}"
    return result


def _check_one(result, wav, sr, merged, raw_labels, raw, tot, embed_fn, id_mapping, db) -> None:
    """Measure one cluster and record it in result (metrics, maybe suspects)."""
    if tot < MIXED_MIN_CLUSTER_SEC:
        result["skipped"][raw] = f"{tot:.0f}s of speech < {MIXED_MIN_CLUSTER_SEC:.0f}s"
        return
    blocks = [b for b in _blocks_for_label(merged, raw_labels, raw)
              if b["dur"] >= MIXED_LONG_BLOCK_SEC]
    if len(blocks) < MIXED_MIN_LONG_BLOCKS:
        result["skipped"][raw] = f"{len(blocks)} long blocks < {MIXED_MIN_LONG_BLOCKS}"
        return
    embs, durs = [], []
    for b in blocks:
        v, used = embed_fn(wav, sr, b["segs"])
        u = _unit(v) if v is not None else None
        if u is None:
            continue
        embs.append(u)
        durs.append(float(used))
    if len(embs) < MIXED_MIN_LONG_BLOCKS:
        result["skipped"][raw] = f"{len(embs)} embeddable long blocks < {MIXED_MIN_LONG_BLOCKS}"
        return
    E, D = np.stack(embs), np.asarray(durs)
    result["checked"] += 1
    groups = agglo2(E)
    if len(groups) != 2:
        return
    g0, g1 = groups
    s0, s1 = float(D[g0].sum()), float(D[g1].sum())
    minor_share = min(s0, s1) / max(s0 + s1, 1e-9)
    c0 = E[g0].mean(axis=0)
    c1 = E[g1].mean(axis=0)
    c0 /= (np.linalg.norm(c0) or 1.0)
    c1 /= (np.linalg.norm(c1) or 1.0)
    csim = float(c0 @ c1)
    name = id_mapping.get(raw)
    # Kept for every checked cluster, flagged or not: "nearly flagged" is what
    # a later re-calibration needs, and it would otherwise leave no trace.
    result["metrics"][raw] = {"label": name or raw, "n_long_blocks": len(embs),
                              "minor_share": round(minor_share, 3), "centroid_sim": round(csim, 3)}
    if minor_share >= MIXED_MINOR_SHARE_MIN and csim <= MIXED_CENTROID_SIM_MAX:
        sub_own = None
        fp = db.get(name) if name else None
        f = _unit(fp) if fp is not None else None
        if f is not None:
            major, minor = (c0, c1) if s0 >= s1 else (c1, c0)
            sub_own = [round(float(major @ f), 3), round(float(minor @ f), 3)]
        result["suspects"][raw] = {
            "label": name or raw,
            "n_long_blocks": len(embs),
            "minor_share": round(minor_share, 3),
            "minor_sec": round(min(s0, s1), 1),
            "centroid_sim": round(csim, 3),
            "sub_own_sim": sub_own,
        }


def format_suspect_lines(info: dict | None) -> list[str]:
    """Human-readable log lines for a check result."""
    lines: list[str] = []
    if not info:
        return lines
    suspects = info.get("suspects") or {}
    if info.get("error"):
        # Partial failure: whatever was found before it is still printed below.
        lines.append(f"two-voice check failed (display only, run continues; "
                     f"{len(suspects)} suspect(s) before the failure): {info['error']}")
    else:
        skipped = info.get("skipped") or {}
        lines.append(f"two-voice check: {info.get('checked', 0)} cluster(s) checked, "
                     f"{len(suspects)} suspect(s)"
                     + (f", {len(skipped)} skipped (short / too few long blocks)" if skipped else ""))
    for raw, m in (info.get("metrics") or {}).items():
        if raw not in suspects:
            lines.append(f"  {raw} (label {m['label']}): {m['n_long_blocks']} long blocks, "
                         f"minority {m['minor_share'] * 100:.0f}%, centroid similarity "
                         f"{m['centroid_sim']:.3f} - not flagged")
    if suspects and info.get("reassigned"):
        lines.append("  note: segment-level reassignment ran on this file - the check "
                     "works on raw clusters, so a suspect below may already be fixed")
    for raw, d in suspects.items():
        own = ""
        if d.get("sub_own_sim"):
            own = (f", fingerprint similarity majority {d['sub_own_sim'][0]:.2f} / "
                   f"minority {d['sub_own_sim'][1]:.2f}")
        lines.append(f"two voices suspected: {raw} (label {d['label']}) - "
                     f"{d['n_long_blocks']} long blocks split into two voices: minority "
                     f"{d['minor_share'] * 100:.0f}% (~{d['minor_sec']:.0f}s), centroid "
                     f"similarity {d['centroid_sim']:.3f}{own}. Some of this label's speech "
                     "may be someone else; listen before enrolling it. Labels unchanged.")
    return lines
