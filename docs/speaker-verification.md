# Speaker verification principles

These rules were established the hard way — from real mislabeling incidents
in production use. Follow them and the fingerprint DB stays trustworthy;
skip them and errors reinforce themselves.

## The four principles

1. **Attendee count ≠ cluster count.** One person can span several clusters
   and several people can share one. Never reason "5 attendees, 5 clusters,
   so the leftover cluster must be the leftover person".
2. **Never assign names by elimination.** An unidentified SPEAKER label gets
   a name only from transcript evidence (self-introduction, being addressed
   by name) or human confirmation — not because everyone else is accounted for.
3. **Un-enrolled speech scatters.** A speaker with no fingerprint doesn't
   collect into one tidy cluster — their lines can be sprinkled across ALL
   labels. After enrolling someone new, scan **every** label
   (`moim-scribe verify` with no `--axis`), not just the one you fixed.
4. **Never re-enroll corrected ranges.** Ranges you fixed based on the
   scanner's own similarity scores must not be fed back into enrollment —
   that's the model grading its own homework (circular reinforcement).
   Only human-confirmed ranges go into the DB.

Plus one practical rule: after a bulk label correction, re-read the lines at
the block boundaries — off-by-one edits at range edges are the most common
silent error.

## First session with new members — the procedure

1. Process the recording normally (labels will be SPEAKER_NN).
2. Confirm identities from the transcript (who introduces themselves, who is
   addressed by name) — a human does this, not the tool.
3. `moim-scribe enroll … SPEAKER_NN=Name` for the confirmed ranges only.
4. `moim-scribe verify AUDIO TXT --expected "Name1,Name2,…"` — roster names
   missing from the transcript signal scattering (principle 3); transcript
   names missing from the roster signal mislabeling.
5. Suspicious blocks: fix only with transcript evidence or human confirmation.
   A speaker who talked very little may simply not separate — write "not
   separated" honestly in the notes rather than guessing.

## Reading `verify` output

```
STRONG 812s-841s | current Alice 0.412 vs Bob 0.587 | "line preview…"
```

- STRONG (gap ≥ 0.15) — almost certainly mislabeled
- LIKELY (gap ≥ 0.08) — worth checking
- WEAK (gap ≥ margin) — borderline; check only if other evidence agrees

The verdict is always made by a human against the transcript. Similarity
scores locate suspects; they never convict.
