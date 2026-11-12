# moim-scribe workspace

This folder is a moim-scribe workspace. Recordings dropped into `inbox/` are turned into summaries in `results/`. Run `moim-scribe` commands from this folder.

## When the user says how many people were in a recording

A recording whose file name has no head count waits in `inbox/` for a few minutes so the user can give one. If the user says something like "the one I just added had 4 people":

1. Run `moim-scribe waiting`. It lists the recordings that are waiting, one per line: the file name, a tab, then the time left (`3:00 left`), or `not scanned yet` for a file the inbox check has not reached yet (its wait starts when it does). It prints nothing when none is waiting.
2. Run `moim-scribe count 4`. If two or more are waiting, `count` does not record anything: it prints the list and exits with an error. Ask the user which recording it is, then add part of its file name: `moim-scribe count 4 "part of the name"`. Do the same when the user named a recording.
3. Tell the user the result in one line. If `count` ended with an error (exit status 1), do not say it worked: pass on what it printed.

If it is not clear which recording they mean, ask before running `count`. If nothing is waiting, say so; do not guess.

## Do not open the user's data unless they ask

This folder holds recordings, transcripts, summaries and voice fingerprints of real people. Do not open, read, search or summarize anything in these places unless the user asks you to:

- `inbox/` (including `_done/`, `_error/`, `_processing/`), `results/`, `speakers/`, `packs/`, `models/`, `.moim-scribe-headcount/`
- `glossary.txt`, `.hf_token`, `.soniox_key`

`moim-scribe waiting` and `moim-scribe count` are fine to run: they print file names, not what is in the recordings.
