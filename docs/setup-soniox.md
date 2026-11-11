# Soniox (cloud transcription) — setup

Optional. The default backends transcribe on your machine and need no account;
this one sends the recording to [Soniox](https://soniox.com) and gets back text
that is already split by speaker.

## Whether you want it

**Reasons to choose it**

- No pyannote, no Hugging Face token, no model download, no GPU — the part of
  the local setup that people get stuck on disappears.
- Fast, and accurate on recordings where people take turns speaking:
  interviews, one-to-one calls, structured meetings.
- Works the same on any platform, including machines where the local model
  stack is painful to install.

**Reasons not to**

- 🔴 **The audio is uploaded.** This is the only setting in moim-scribe that
  sends the recording itself off your machine. If you record private
  conversations, read [privacy.md](privacy.md) first and say so when you ask
  the other people for consent — "the recording is uploaded for transcription"
  is a separate thing to agree to from "the transcript is sent to an AI".
- **Speakers can be merged on noisy recordings.** In testing, a conversation
  where several people talked over each other came back with distinct speakers
  fused into one. Where turn-taking is clean it did well. If you have a Hugging
  Face token, the hybrid step below fixes exactly this and is on by default;
  without one, the local path with an explicit speaker count is the safer
  choice for a crowded recording that matters.
- **Soniox itself cannot be told how many speakers there are.** Diarization is
  a single on/off switch and it detects up to 15 speakers by itself. A count in
  the filename reaches pyannote when the hybrid step runs, and is dropped
  otherwise — the log says which happened.
- It costs money and has quotas you have to keep an eye on (below).

## Get a key

1. Sign up at [console.soniox.com](https://console.soniox.com).
2. Create an API key.
3. Save it into your workspace:

```bash
echo "YOUR_KEY" > ~/MyMeetings/.soniox_key
```

`moim-scribe init` adds `.soniox_key` to the workspace `.gitignore`. If your
workspace predates this file, run `moim-scribe init <workspace>` again: it
appends the missing entries, leaves the rest of your `.gitignore` alone, and
never touches `config.yaml`. `SONIOX_API_KEY`
in the environment works too and takes precedence.

## Turn it on

In `<workspace>/config.yaml`:

```yaml
stt_backend: soniox
# soniox_region: us     # us | eu | jp
```

Then check the setup:

```bash
moim-scribe doctor ~/MyMeetings
```

With `soniox` selected, `doctor` stops requiring pyannote and the Hugging Face
token, but still checks `speechbrain`, because matching voices to names runs
locally on both paths.

## Better speaker separation, if you already have pyannote

Soniox transcribes well and separates speakers poorly. On one 5-person meeting
it produced 56k characters with proper nouns intact, and collapsed those five
people into two or three blocks; pyannote on the same file separated all five
and lost the names. A second recording reproduced it. The two engines fail in
opposite directions, so moim-scribe runs both and keeps the better half of
each — **Soniox's text with pyannote's speaker boundaries**.

This is on by default and costs nothing to leave on:

```yaml
hybrid_diarization: true    # default
```

- **No Hugging Face token → the step skips itself**, silently, and you get
  Soniox's own labels exactly as before. Nothing to configure, nothing to turn
  off. `doctor` says which of the two is happening.
- **With a token**, pyannote runs locally on your machine after the transcript
  comes back. The audio has already been uploaded at that point, so this adds
  no new privacy question — but it does add local processing time, roughly what
  a local diarization run would cost, and the first run downloads the pyannote
  models if you have never used the local path. `moim-scribe doctor` fails on a
  missing `pyannote.audio` when a token is present, so you find that out before
  a recording does.
- A speaker count in the filename (`team meeting 5p.m4a`) reaches pyannote here.
  It is the only way that hint does anything on this path; Soniox takes no
  count of its own.

Set it to `false` to keep Soniox's labels even when pyannote is available.

🔴 Every failure of this step is absorbed — no token, no model, no GPU, an
unreadable file — and the run finishes with Soniox's labels. The transcription
has already been paid for by then and cannot be repeated, so an improvement is
never allowed to cost it.

## Limits worth knowing before you rely on it

| | |
|---|---|
| Model | `stt-async-v5` |
| Price | $0.10 per hour of audio (async transcription) |
| Length | **300 minutes per file, fixed.** moim-scribe refuses a longer file before uploading it, so you do not pay for the rejection |
| Storage | 🔴 **Uploads are not deleted automatically.** The organization cap is 10 GB / 1,000 files |
| Queue | 100 pending transcriptions, 2,000 total |
| Regions | `us`, `eu`, `jp` — but eu and jp must be activated for your account by Soniox before they answer, so the default is `us` |
| Retries | A failed transcription is not retried by Soniox; the documented remedy is to submit a new one |

### The storage cap is the one that bites

Because uploads persist until deleted, moim-scribe deletes both the file and
the transcription at the end of every run — including when the run fails. That
is not tidiness; without it the account fills up and transcription stops
working.

If a deletion fails, the log says exactly what to remove and where:

```
DELETE FAILED (HTTP 409) — remove transcription <id> by hand at
https://console.soniox.com, or it stays on their servers and counts
against your quota
```

Do that when you see it. A line like this is also worth searching for
occasionally if you have been running unattended:

```bash
grep -i "DELETE FAILED" /path/to/your/log
```

## Going back to local

Set `stt_backend` to `mlx` (Apple Silicon) or `faster` (elsewhere). You will
then need the Hugging Face token and the pyannote terms acceptance described in
[setup-pyannote.md](setup-pyannote.md). Nothing else changes: both paths write
the same three files, use the same fingerprint database, and produce the same
notes — so you can switch per recording by editing one line, and enrolled
speakers carry over.
