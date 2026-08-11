"""Flag fingerprint matches that came from a different group.

Voice fingerprints get compared against every enrolled speaker, and as the
database grows so does the chance that someone who was not in the room scores
highest. Most of those mistakes have a shape: the person belongs to a *different*
recurring group. Someone from your book club surfacing in a work meeting is not
impossible, but it is worth a second look.

This module says so out loud. It does not act on it.

🔴 **Deliberately a display, not a gate.** The obvious design — raise the
similarity threshold when the groups differ — was measured on real data and
rejected: cross-group matches were correct often enough that blocking them would
have silently dropped true identifications, and the score ranges overlapped so
there was no threshold that separated them. Worse, group tags go stale. People
join a second group, leave one, or start attending as a guest, and nobody
remembers to update a JSON file. A stale tag that only prints a warning costs one
noisy line; a stale tag that blocks turns a correct match into "unknown" with no
trace of why.

The group of a recording is the pack that matched it — the same pack that
supplies background context to the summary — so there is nothing extra to
configure. Speakers get their tags at enrollment time (`--group`).
"""
from __future__ import annotations

import json
import unicodedata
from pathlib import Path


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s or "")


def group_map(index: dict) -> tuple[dict, set]:
    """(name -> groups, names that attend every group).

    `attends_all` is for the person who is in every recording — usually whoever
    runs the group and holds the recorder. Tagging them with one group would make
    them look out of place in all the others.
    """
    groups, always = {}, set()
    for raw, entry in (index or {}).items():
        name = _nfc(raw)
        if not isinstance(entry, dict):
            continue
        if entry.get("groups"):
            groups[name] = [_nfc(g) for g in entry["groups"]]
        if entry.get("attends_all"):
            always.add(name)
    return groups, always


def off_group_matches(recording_group: str | None, matched_names: list[str],
                      index: dict) -> list[dict]:
    """Matches whose speaker belongs to some other group. Empty when nothing to say.

    Three cases are skipped on purpose, each one an over-report waiting to happen:
      - the recording has no group (no pack matched) — an unknown is not a suspicion
      - the speaker carries no tags — newly enrolled, or simply not categorized
      - the speaker is tagged `attends_all`
    """
    rg = _nfc(recording_group) if recording_group else ""
    if not rg:
        return []
    groups, always = group_map(index)
    if not groups:
        return []
    out = []
    for raw in matched_names:
        name = _nfc(raw)
        if name in always:
            continue
        g = groups.get(name)
        if not g or rg in g:
            continue
        out.append({"name": name, "groups": g, "recording_group": rg})
    return out


def format_note(off: list[dict]) -> str:
    """One line for the log. Empty string when there is nothing to report."""
    if not off:
        return ""
    who = ", ".join(f"{o['name']} ({'/'.join(o['groups'])})" for o in off)
    return (f"identified {len(off)} speaker(s) from another group — {who} "
            f"in a '{off[0]['recording_group']}' recording; worth a listen")


def check_stats_file(stats_path: str | Path, recording_group: str | None,
                     index: dict) -> str:
    """Read a speakers.json and return the note for its confident matches.

    🔴 Returns an empty string on any failure. This runs after transcription and
    identification are already done; a problem here must not cost the user a
    finished recording.
    """
    try:
        data = json.loads(Path(stats_path).read_text(encoding="utf-8"))
        names = [
            _nfc(s.get("label", ""))
            for s in data.get("speakers", [])
            if s.get("identified") and isinstance(s.get("match_score"), (int, float))
        ]
        return format_note(off_group_matches(recording_group, names, index))
    except Exception:  # noqa: BLE001 — a display must never break the pipeline
        return ""
