# Speaker verification principles

These rules were established the hard way — from real mislabeling incidents
in production use. Follow them and the fingerprint DB stays trustworthy;
skip them and errors reinforce themselves.

## The principles

<!-- Deliberately uncounted in the heading. A stated count drifts the moment a
     principle is added — and it did, across three documents at once. -->


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

5. **Read a similarity list together with who could plausibly be there.** A
   fingerprint is compared against every enrolled speaker, so when the top
   matches are all people from your other groups, that is usually not evidence
   of a mixed-up cluster — it means nobody in the database is a real candidate,
   which is exactly what a not-yet-enrolled person looks like. Unenrolled voices
   land in the 0.5-0.6 range against more or less anyone. Tagging speakers at
   enrollment (`--group`, `--attends-all`) makes the run point this out for you,
   as a logged line and nothing more.

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

## What the tool checks for you, and what it does not

- **During processing**, a label whose long stretches split into two
  different-sounding groups is logged as "two voices suspected" and marked in
  `speakers.json`. It is a prompt to listen, never a relabel.
- **At enrollment**, the same name on two labels of one recording is refused
  (`--allow-duplicate`), and so is a voice below 0.5 similarity to that
  person's existing fingerprint (`--force`). Neither number is a safety line.
- **Not checked**: whether the label you are enrolling is one voice. An
  automatic test for that was built and measured on real recordings: of 29
  labels deliberately assembled from two people, it flagged 3. It was removed
  rather than shipped as reassurance. Listening is still the check.
