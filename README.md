# moim-scribe

**A local voice pipeline built for recurring meetings — the more often the same people meet, the more accurate it gets.**

*moim* (모임) is Korean for a small recurring gathering — a book club, a writing circle, a weekly team sync. moim-scribe watches a folder, and when a recording lands in it: transcribes (whisper) → separates speakers (pyannote) → **matches voices against a fingerprint database that grows with every session** → writes structured notes using the template for that type of meeting. Audio, diarization, and voice fingerprints never leave your machine; the final summarization step is your choice — fully local (Ollama, or none) or a cloud LLM (Claude).

> **Consent first.** moim-scribe is built for groups that record themselves with everyone's informed consent. Voice fingerprints are biometric data — never use this tool to record or identify people covertly. In many jurisdictions that is illegal, not just impolite: [docs/privacy.md](docs/privacy.md).

[한국어 README](README.ko.md)

## Why another whisper + pyannote script?

One-shot diarization scripts treat every recording as a room full of strangers. Recurring groups aren't: it's the same six people every other week. moim-scribe exploits that.

- **Accumulating voice fingerprints.** Confirm who's who once, and each speaker's ECAPA embedding is stored and refined (speech-seconds weighted average) every time they appear. `SPEAKER_03` becomes a real name automatically from the second session on.
- **Merge repair.** pyannote's classic failure — two similar voices fused into one cluster — is detected by re-checking segments against enrolled fingerprints, and the transcript is re-labeled. Unenrolled guests are never touched.
- **Anti-hallucination transcription.** Whisper's repetition loops (the same phrase 17 times at exactly 1.0s intervals) are suppressed at decode time and collapsed deterministically afterwards.
- **Per-group templates (packs).** A meeting produces minutes; a book club produces per-participant discussion notes; your group's roster and running context ride along in the prompt. See [docs/packs.md](docs/packs.md).
- **Honest labels.** Verification tooling (`moim-scribe verify`) cross-checks finished transcripts against fingerprints and your attendee roster, following rules learned from real mislabeling incidents. See [docs/speaker-verification.md](docs/speaker-verification.md).

## Platform support

| | STT backend | status |
|---|---|---|
| macOS (Apple Silicon) | `mlx` (mlx-whisper) | tier 1 — this is the config the pipeline was built and field-tested on |
| Windows / Linux / Intel Mac | `faster` (faster-whisper) | supported by design, CI-tested at unit level — field reports welcome |

pyannote and speechbrain are PyTorch and run everywhere (CUDA, MPS, CPU). The watcher uses `watchdog` (native events on all three OSes).

## Install

Requirements: Python 3.11+, [ffmpeg](https://ffmpeg.org) on PATH (`brew install ffmpeg` / `winget install ffmpeg` / `apt install ffmpeg`).

```bash
# macOS Apple Silicon
pip install "moim-scribe[mlx] @ git+https://github.com/kemy-ai/moim-scribe"

# Windows / Linux / Intel Mac
pip install "moim-scribe[cross] @ git+https://github.com/kemy-ai/moim-scribe"
```

One-time: pyannote's diarization model is gated — create a free Hugging Face token and accept the model terms. Takes two minutes: [docs/setup-pyannote.md](docs/setup-pyannote.md).

## Quickstart

```bash
moim-scribe init ~/MyMeetings          # creates the workspace (add --language en)
# save your HF token:
echo "hf_..." > ~/MyMeetings/.hf_token

moim-scribe doctor ~/MyMeetings        # checks ffmpeg, backends, token

# drop a recording into a type folder:
cp recording.m4a ~/MyMeetings/inbox/gathering/

moim-scribe watch ~/MyMeetings         # or scan-inbox for a single pass
```

Results land in `results/` as `YYMMDD_title_type.md` plus the full diarized transcript. Tips that remove guesswork:

- put the attendee count in the filename — `book club 5명.m4a` / `standup 4p.m4a`
- or say it at the start of the recording ("참석자 다섯 명입니다" / "five participants today")

## Teaching it who's who

```bash
# after the first processed session, map labels to names once:
moim-scribe enroll inbox/_done/rec.m4a inbox/_done/rec_transcript_diarized.jsonl \
    SPEAKER_00="Kim Haneul" SPEAKER_02="Lee Bada" --workspace ~/MyMeetings

moim-scribe speakers ~/MyMeetings      # see accumulated fingerprints

# from now on those two are auto-identified; verify a finished transcript anytime:
moim-scribe verify rec.m4a rec_transcript_diarized.txt \
    --expected "Kim Haneul,Lee Bada,Park Gureum" --workspace ~/MyMeetings
```

Only enroll ranges a human has confirmed — the discipline that keeps the database trustworthy is in [docs/speaker-verification.md](docs/speaker-verification.md).

## Summarizer backends

| backend | needs | transcript leaves your machine? |
|---|---|---|
| `claude_cli` (default) | Claude Code installed | yes — sent to Anthropic via your existing login |
| `anthropic_api` | `ANTHROPIC_API_KEY` | yes — sent to Anthropic's Messages API |
| `ollama` | Ollama running locally | no — fully offline pipeline |
| `none` | nothing | no — stops at the diarized transcript |

Switch in `config.yaml`. Lenses ship in Korean and English (`--language`).

## Privacy and consent — read this

Voice fingerprints are **biometric personal data**, and recordings capture private conversation.

- **Record and enroll only with every participant's consent.** In many jurisdictions this is a legal requirement, not a courtesy — e.g. under Korea's Personal Information Protection Act, voiceprints used to identify a person are sensitive biometric information requiring separate explicit consent.
- Audio processing, diarization, and fingerprinting stay local; after the first model download the fingerprint step runs offline by design. Only the optional summarization step can send the transcript to a cloud LLM — and you pick that backend.
- The workspace keeps fingerprints (`speakers/`), recordings, and rosters outside the repo, and `init` drops a defensive `.gitignore` in the workspace. Never publish a workspace.

Details: [docs/privacy.md](docs/privacy.md).

## Running it in the background

launchd (macOS), systemd (Linux), and Task Scheduler (Windows) templates: [docs/deployment.md](docs/deployment.md). Cloud-synced folders (Drive/Dropbox) work — the pipeline double-checks that files are fully downloaded and decodable before touching them.

## Maintenance

Best-effort: issues and PRs are read, response time is not guaranteed. Forks welcome.

## Roadmap (not in v0.1)

- Publishing plugins (Notion etc.) and completion notifications
- English field-testing of the `faster` backend at tier-1 level

## License

[MIT](LICENSE). Depends on openly licensed projects (whisper implementations MIT, pyannote.audio MIT, speechbrain and watchdog Apache-2.0); the gated pyannote **model weights** have their own access terms you accept on Hugging Face — they are downloaded to your machine, never redistributed here.
