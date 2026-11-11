# Packs: optional background for a recurring group

Packs are optional. Without any, every recording still gets the same summary;
a pack only adds background that the model cannot hear in the audio.

There is one summary format for every recording
(`packs/lenses/summary.<language>.md`, with shared rules in
`_common.<language>.md`). A pack does not change that format. It adds:

- who the regulars are and how their names are spelled (the roster)
- what the group's jargon and abbreviations mean
- what was decided or left open in earlier rounds

## Pack directory anatomy

```
packs/my-book-club/
├── match.json    {"title_contains": ["scifi club"]}
├── pack.md       curated background: what the group is, ROSTER, running context
├── session/      optional extra material (the newest .md is auto-loaded)
└── _history/     auto-appended round cards (the pipeline only ever appends)
```

When a recording titled "scifi club week 12" lands in `inbox/`, the pack
matches (keyword substring of the title, longest keyword wins across packs),
and the summary prompt gains: pack.md + the newest session file + strict usage
rules (background is reference only; quotes and attribution still need
transcript evidence). A recording whose title matches no pack is summarized
with no background at all.

The pack name doubles as the group name for speaker tags
(`moim-scribe enroll … --group my-book-club`).

## Round cards: accumulate, then curate

After each successful summary, a *round card* (date, title, and the
Decisions / Action items / Open items sections when the summary has them) is
appended to `_history/`. The pipeline **never edits pack.md**: merging history
back into the curated background is deliberately a human step, so automated
output cannot silently rewrite your source of truth.

## Changing what every summary looks like

Edit `packs/lenses/summary.<language>.md` in your workspace. If the file is not
there (a workspace created before 0.2), the built-in copy is used. Run
`moim-scribe init <workspace>` again: it adds any missing lens files and never
touches `config.yaml`, so you can then edit the copy it created.

If you rename the Decisions / Action items / Open items headings, round cards
stop collecting those sections: they are harvested by heading.

## Onboarding a new series: checklist

1. Create `packs/<name>/` with `match.json` + `pack.md` (include the roster).
2. Check keyword collisions: a broad keyword can steal matches from other
   packs (longest-match helps, but avoid one-word generic keywords).
3. After the first session, enroll the new voices from human-confirmed
   ranges, then run `moim-scribe verify --expected <roster>`. See
   [speaker-verification.md](speaker-verification.md).

## Packs written before 0.2

`"type"` and `"style"` in `match.json` are ignored, and `packs/styles/` is no
longer read. Nothing needs to be deleted.
