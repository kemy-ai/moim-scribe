# Packs: types, styles, and per-series context

Three layers decide how a recording becomes notes:

| layer | what it decides | chosen by | file |
|---|---|---|---|
| 1. type | which pipeline template (lens) runs | inbox subfolder name | `packs/lenses/<lens>.<lang>.md` |
| 2. style | shared output format for a family of groups | `style` key in match.json | `packs/styles/<style>.md` |
| 3. pack | one recurring series: match rules, roster, context | filename keywords | `packs/<name>/` |

A type without a lens (`transcript: null` in config.yaml) stops at the
diarized transcript.

One further lens ships **inactive**: `conversation`, for recurring private
conversations, where the record is the wording itself rather than decisions or
per-person catch-up. It is not in the default `types:` — turn it on only after
reading `packs/conversation/README.md`, which opens with a consent notice.

## Pack directory anatomy

```
packs/my-book-club/
├── match.json    {"type": "gathering", "title_contains": ["scifi club"], "style": "bookclub"}
├── pack.md       curated background: what the group is, ROSTER, running context
├── session/      optional extra material (the newest .md is auto-loaded)
└── _history/     auto-appended round cards — the pipeline only ever appends
```

When a recording titled "scifi club week 12" lands in `inbox/gathering/`,
the pack matches (keyword substring, longest keyword wins across packs),
and the summary prompt gains: the style template + pack.md + the newest
session file + strict usage rules (background is reference only — quotes
and attribution still need transcript evidence).

## Round cards: accumulate, then curate

After each successful summary, a *round card* (date, title, decisions/next-time
sections if present) is appended to `_history/`. The pipeline **never edits
pack.md** — merging history back into the curated background is deliberately
a human step, so automated output can't silently rewrite your source of truth.

## Onboarding a new series — checklist

1. Create `packs/<name>/` with `match.json` + `pack.md` (include the roster).
2. Pick a style, or add a new one under `packs/styles/`.
3. Check keyword collisions: a broad keyword can steal matches from other
   packs (longest-match helps, but avoid one-word generic keywords).
4. After the first session, enroll the new voices from human-confirmed
   ranges, then run `moim-scribe verify --expected <roster>` — see
   [speaker-verification.md](speaker-verification.md).
