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
  fused into one. Where turn-taking is clean it did well. If a recording
  matters and it is the crowded kind, the local path with an explicit speaker
  count is the safer one.
- **You cannot tell it how many speakers there are.** Diarization is a single
  on/off switch and it detects up to 15 speakers by itself. A count in the
  filename — which the local backends use — is ignored here; the log says so
  each time it drops one.
- It costs money and has quotas you have to keep an eye on (below).

## Get a key

1. Sign up at [console.soniox.com](https://console.soniox.com).
2. Create an API key.
3. Save it into your workspace:

```bash
echo "YOUR_KEY" > ~/MyMeetings/.soniox_key
```

`moim-scribe init` adds `.soniox_key` to the workspace `.gitignore`. If your
workspace predates this file, run `moim-scribe init --force` — it appends the
missing entry and leaves the rest of your `.gitignore` alone. `SONIOX_API_KEY`
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

With `soniox` selected, `doctor` stops asking for pyannote and the Hugging Face
token — neither is used — but still checks `speechbrain`, because matching
voices to names runs locally on both paths.

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
