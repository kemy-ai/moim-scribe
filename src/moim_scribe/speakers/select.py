"""Choose which segments of a label go into a fingerprint.

Enrolling every segment of a label assumes the label is one person. When a
cluster swallowed part of someone else, enrolling it averages the other voice
into the fingerprint, and from then on that fingerprint pulls the other person's
speech toward this name. This module decides, per label, whether to enroll and
which segments.

Two measures, two different jobs, and the order is fixed
  (1) **Group membership** answers "is this label one voice?". Each stable long
      segment is compared with the mean of all the others (leave-one-out); a
      segment below MEMBERSHIP_MIN_SIM is an outlier. Measured upstream, real
      single-speaker labels had 12-20% outliers and deliberately mixed labels
      (five two-person and one three-person combinations) had 100%, with no
      overlap. It is a good *mixing* test and a bad *selection* tool: it
      measures length as much as purity. Even a single speaker's 3-5 s segments
      came out 93% "outliers", and the correlation between segment length and
      membership was positive for every speaker measured. Used to select, it
      throws away the short segments and leaves a thin fingerprint. So it runs
      only on the stable long segments, and only to decide mixed / not mixed.
  (2) **Core comparison** answers "which segments belong?". The core is the
      mean of the stable long segments; each segment is kept if its similarity
      to the core is at least CORE_MIN_SIM. Its length bias is small (upstream:
      the speaker's own segments 0.73-0.78 against the core, other people's
      0.17-0.40, and the highest excluded own segment 0.495). But if the label
      is already mixed, the core is pulled toward the intruder and the result
      flips: in an upstream test with eleven long intruding segments, the core
      caught one of the eleven and discarded all 118 of the real speaker's
      segments.
  Hence (1) first and (2) only if (1) says "one voice". Swapping or skipping
  (1) is exactly the failure (2) cannot detect on its own.

Everything here takes embeddings that are already computed. No model, no audio.
"""
from __future__ import annotations

import numpy as np

STABLE_MIN_SEC = 8.0          # a segment this long or longer counts as "stable"
STABLE_MAX_SEGMENTS = 40      # the longest N stable segments build the judgement and core
MIN_JUDGE_SEGMENTS = 5        # with fewer stable segments, neither measure means anything
MEMBERSHIP_MIN_SIM = 0.60     # leave-one-out similarity below this = outlier
MIXED_OUTLIER_FRACTION = 0.5  # outlier share at or above this = label looks mixed
CORE_MIN_SIM = 0.50           # a segment is kept at this similarity to the core or more


def _unit_rows(embs: list) -> np.ndarray:
    E = np.stack([np.asarray(v, dtype=float).flatten() for v in embs])
    norms = np.linalg.norm(E, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return E / norms


def stable_indices(durations: list[float]) -> list[int]:
    """Indices of the longest STABLE_MAX_SEGMENTS segments of >= STABLE_MIN_SEC."""
    idx = [i for i, d in enumerate(durations) if d >= STABLE_MIN_SEC]
    idx.sort(key=lambda i: -durations[i])
    return idx[:STABLE_MAX_SEGMENTS]


def membership_outlier_fraction(E: np.ndarray) -> float:
    """Share of rows whose cosine to the mean of the other rows is below the floor.

    E must be L2-normalised rows, at least two of them.
    """
    n = len(E)
    total = E.sum(axis=0)
    outliers = 0
    for i in range(n):
        others = (total - E[i]) / (n - 1)
        norm = float(np.linalg.norm(others)) or 1.0
        if float(E[i] @ others) / norm < MEMBERSHIP_MIN_SIM:
            outliers += 1
    return outliers / n


def select_segments(embs: list, durations: list[float]) -> dict:
    """Judge one label and pick its segments.

    embs[i] is the embedding of a segment lasting durations[i] seconds.
    Returns a dict with "decision":
      "selected"   - one voice; "keep" lists the indices to enroll
      "mixed"      - looks like more than one voice; enroll nothing
      "too_short"  - fewer than MIN_JUDGE_SEGMENTS stable segments; the caller
                     decides (neither measure can be trusted on so little)
    plus the numbers behind the decision.
    """
    stable = stable_indices(durations)
    out: dict = {"n_segments": len(embs), "n_stable": len(stable)}
    if len(stable) < MIN_JUDGE_SEGMENTS:
        return {**out, "decision": "too_short", "keep": []}
    E = _unit_rows(embs)
    S = E[stable]
    frac = membership_outlier_fraction(S)
    out["outlier_fraction"] = round(frac, 3)
    if frac >= MIXED_OUTLIER_FRACTION:
        return {**out, "decision": "mixed", "keep": []}
    core = S.mean(axis=0)
    core /= (np.linalg.norm(core) or 1.0)
    sims = E @ core
    keep = [i for i in range(len(embs)) if float(sims[i]) >= CORE_MIN_SIM]
    kept = set(keep)
    dropped = [float(sims[i]) for i in range(len(embs)) if i not in kept]
    return {
        **out,
        "decision": "selected",
        "keep": keep,
        "kept_sec": round(sum(durations[i] for i in keep), 1),
        "total_sec": round(sum(durations), 1),
        "max_dropped_sim": round(max(dropped), 3) if dropped else None,
    }
