"""Render speakers.json for a summarizer prompt, minus the diagnostic fields.

speakers.json carries two kinds of field. One describes what happened in this
recording. The other exists to reconstruct a mislabeling afterwards — and that
kind can name people who were **not** in the room: `anchors` and `suspects` are
built by comparing every cluster against the whole enrolment database, so any
enrolled name can appear regardless of who attended.

That matters because of where this block lands. The prompt header above it says
the attendee list comes from this data, so passing the file through unfiltered
puts absent people's names in front of the summarizer as though they had been
present. Upstream this leaked several such names per file before it was caught.

Nothing is lost by filtering here: every diagnostic consumer reads the file on
disk, and this function's only output is the prompt.

One field survives from the reassignment block — `triggered`. It is a boolean,
so it names nobody, and a lens needs it: when labels were reassigned by
fingerprint re-checking, an enrolled person who was absent can have their name
attached to someone else, and the deliverable should say so.

Leftover noise labels (diarize._mark_leftover_noise) are a third case: not
diagnostic, but not people either. Their entries are removed, the label-count
field that still includes them is dropped, and one note names the excluded
labels - the transcript still carries those labels, and without the note a
summarizer reading it would count them back in as attendees.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .diarize import LEFTOVER_NOISE_MAX_SEC

logger = logging.getLogger("moim_scribe.speaker_stats")

# Diagnostic-only keys inside each speakers[] entry. mixed_suspect_diag is a
# boolean, but "part of this label may be someone else" is for the human to
# check, not for a summarizer to act on - listed by name as well as by suffix.
DIAG_ENTRY_KEYS = frozenset({"match_score_full_db", "mixed_suspect_diag"})

# Diagnostic-only top-level blocks. mixed_check_diag carries the labels already
# on the flagged clusters, and those can be names identification got wrong.
DIAG_TOP_KEYS = frozenset({"mixed_check_diag"})

# Leftover noise handling (see the module docstring).
NOISE_ENTRY_FLAG = "leftover_noise"
NOISE_TOP_KEYS = frozenset({"n_labels_total"})
NOISE_NOTE_KEY = "excluded_noise_labels"

# Keys of the top-level merge_reassign block that may reach a prompt. Everything
# else in that block is dropped. Inverted deliberately — an allow-list here means
# a newly added diagnostic field is excluded by default rather than by memory.
MERGE_REASSIGN_PROMPT_KEYS = frozenset({"triggered"})

# Suffix convention for future diagnostic fields: anything ending in _diag is
# filtered without touching this module. It is only a convention with no
# enforcement, which is exactly why the allow-list above exists as well — the
# field that leaked upstream was one nobody had thought to name that way.
DIAG_SUFFIX = "_diag"


def _is_diag(key: str) -> bool:
    return key.endswith(DIAG_SUFFIX)


def strip_diagnostic_keys(data: dict) -> dict:
    """Return a copy of a speakers.json dict safe to place in a prompt."""
    out: dict = {}
    for key, value in data.items():
        if _is_diag(key) or key in DIAG_TOP_KEYS or key in NOISE_TOP_KEYS:
            continue
        if key == "merge_reassign":
            if isinstance(value, dict):
                out[key] = {
                    k: v for k, v in value.items() if k in MERGE_REASSIGN_PROMPT_KEYS
                }
            continue
        if key == "speakers" and isinstance(value, list):
            cleaned, noise = [], []
            for entry in value:
                if not isinstance(entry, dict):
                    cleaned.append(entry)
                elif entry.get(NOISE_ENTRY_FLAG):
                    noise.append(str(entry.get("label", "")))
                else:
                    cleaned.append({
                        k: v for k, v in entry.items()
                        if k not in DIAG_ENTRY_KEYS and not _is_diag(k) and k != NOISE_ENTRY_FLAG
                    })
            out[key] = cleaned
            if noise:
                out[NOISE_NOTE_KEY] = (
                    f"Transcript label(s) {', '.join(noise)} hold {LEFTOVER_NOISE_MAX_SEC:g} s or "
                    f"less of sound each ({len(noise)} label(s)); they are noise, not people, "
                    "and are excluded from n_speakers"
                )
            continue
        out[key] = value
    return out


def render_speaker_stats_block(path: Path) -> str:
    """Filtered speakers.json as prompt text, or "" if it cannot be read.

    Never raises: a missing or corrupt stats file must not stop a summary. The
    caller logs the omission, because a silently absent block would make the
    summary guess the attendee count from the conversation while the run still
    looks completely normal.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        # Deliberately does not include the file contents — this module exists to
        # keep those names out of places they don't belong, logs included.
        logger.warning(f"speakers.json unreadable ({type(e).__name__}), omitting speaker stats")
        return ""
    if not isinstance(data, dict):
        logger.warning("speakers.json is not an object, omitting speaker stats")
        return ""
    return json.dumps(strip_diagnostic_keys(data), ensure_ascii=False, indent=2)
