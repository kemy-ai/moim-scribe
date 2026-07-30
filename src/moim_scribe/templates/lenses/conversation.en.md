# Deliverable: conversation record

For recurring private conversations with the same people, kept so they can be
looked back on. The whole point is what was said, in what order, and in whose
actual words — this lens does not diagnose a relationship or give advice. It
assumes nothing about the number of people or the kind of relationship (it may
be two people or several).

## First — label confidence

If **any** of the following holds in the "Speaker stats" data above, state it in
one paragraph at the very top of the deliverable. If none holds, omit this
section entirely.

- some speaker has `identified: false` → "Real names were not established for these speakers this time: (labels). The per-speaker record below reflects voice separation only; who is who has not been confirmed."
- a filename attendee count was given and differs from `n_speakers` → "Voices were split into N, but the stated count is M. One person may have been split across labels, or several people merged into one."
- some speaker has a `match_score` between 0.6 and 0.7 → "(name) was matched with low confidence."
- `merge_reassign` is present → "Labels were reassigned this time by re-checking voice fingerprints. If any enrolled person was not actually in the room, their name may have been attached to someone else."

This paragraph is not a warning — it is **what the reader needs to decide how
much to trust the record below**. Keep it short and factual.

## Structure

```
# Conversation record — [date]

## Overview
- Covered: (topics, comma-separated)
- Talk share: from the "Speaker stats" share values — Name NN% · Name NN%
- Flow: (one arrow line for how topics shifted)

## What was said, by topic
Per topic: how it started and where it went, with verbatim quotes from those present:
- **[topic]**: where it started → what was said → where it stopped
  - Name: "…"
  - Name: "…"

## Wording record
Per speaker, the words they actually used, verbatim. Quotes only — no reading between the lines, no summarizing:
- **Name**
  - "…" (at NN min, during [topic])
  - "…"

## What came up again
Write this **only if the background block above actually contains earlier rounds**. Otherwise omit this section entirely.
Do not summarize a pattern into a sentence — put the earlier round and this one side by side:
- **[topic]** — earlier: (round/date) "quote" / this time: "quote"

## Agreed and left open
- Agreed: (only where those present said the same thing)
- Left open: (moved past without a conclusion)
```

## Hard rules for this lens

- **Never write who was right or what the problem is.** Do not infer the cause of a disagreement and do not propose fixes. The reader of this record is someone looking at their own words again.
- **Never assert emotion or tone.** A transcript is text: it has no volume, no intonation, no silence. "angrily", "aggressively" cannot be known from a transcript, so do not write them. What you can write is "these were the words used".
- **Quotes are verbatim.** Never tidy or condense something and put it in quotation marks. Trim long quotes and mark the cut with "…".
- **Never attribute an unidentified label's (SPEAKER_NN) speech to a named person by guessing.** Keep the label and record it under the label.
- Choosing what to quote is already editing. **Do not collect only one person's words** — keep the balance across everyone present within each topic.
