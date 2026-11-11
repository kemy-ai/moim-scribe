# moim-scribe

**A local-first voice pipeline built for recurring meetings — the more often the same people meet, the more accurate it gets.**

By default the summary step sends the transcript to Anthropic through Claude Code. Set `summarizer: ollama` or `summarizer: none` and everything stays on your machine.

*moim* (모임) is Korean for a small recurring gathering — a book club, a writing circle, a weekly team sync. moim-scribe watches a folder, and when a recording lands in it: transcribes (whisper) → separates speakers (pyannote) → **matches voices against a fingerprint database that grows with every session** → writes a summary. It does not ask what kind of recording it is: a meeting, a call and a book club all go into the same folder and come out in the same format. Voice fingerprints are always local. Two steps are yours to choose: transcription runs on your machine by default and can instead go to Soniox, and summarization can be local (Ollama, or none) or a cloud LLM (Claude).

> **Consent first.** moim-scribe is built for groups that record themselves with everyone's informed consent. Voice fingerprints are biometric data — never use this tool to record or identify people covertly. **This bites hardest in private relationships**: a conversation with a partner or a family member is where one side is most tempted to record without saying so, and precisely where you must not. In many jurisdictions that is illegal, not just impolite: [docs/privacy.md](docs/privacy.md).

[한국어 README](README.ko.md)

## How it works

```mermaid
flowchart TD
    subgraph machine["on your machine"]
        A["inbox/<br>recording lands"] --> B["transcribe + diarize<br>whisper (mlx / faster) + pyannote<br>(soniox: its text + pyannote boundaries)"]
        FP[("speaker fingerprints<br>grow every session")] <--> B
        B --> C["summarize<br>one summary lens for every recording<br>(+ your background pack, if any)"]
        C --> D["results/<br>summary md + diarized transcript"]
        V["enroll · verify commands"] -.-> FP
    end
    B -.->|"the audio itself, and only<br>with stt_backend: soniox"| STT["Soniox<br>cloud STT"]
    C -.->|"the transcript and a few facts, and only with a<br>cloud backend (claude_cli · anthropic_api)"| LLM["cloud LLM"]
```

Fingerprints never cross the dashed line. Two things can, each only if you choose it: the audio (`stt_backend: soniox`) and the transcript (a cloud summarizer). What the summarizer receives is the transcript, the recording's title and time, the speaker statistics, and your pack background when a pack matches; never the audio and never a fingerprint. With the defaults changed to `soniox` + `none`, the recording leaves and the transcript does not; with `mlx` + `ollama`, nothing leaves at all.

## What is in the box

**Core: works as soon as it is installed.**

| | what it does |
|---|---|
| **Speaker separation** | splits the recording by who spoke when (`SPEAKER_00`, `SPEAKER_01` …) |
| **Voice fingerprints** | you confirm who is who once; from the next recording on, those people are named automatically |
| **Summary** | one summary format for every recording, with attendees, talk share, what was discussed, decisions and action items when there are any. Needs a summarizer: Claude Code by default (sends the transcript to Anthropic), or Ollama (local), or `none` (diarized transcripts only, no summary) |

**Optional: off until you set it up, and the core does not depend on any of it.**

| | what it adds | where |
|---|---|---|
| Cloud transcription | Soniox instead of a local whisper model (uploads the audio) | [docs/setup-soniox.md](docs/setup-soniox.md) |
| Background packs | a recurring group's roster and running context, added to the summary prompt | [docs/packs.md](docs/packs.md) |
| Group tags | a log line when a voice match comes from a different group | "Teaching it who is who" below |
| Label verification | cross-check a finished transcript against fingerprints | [docs/speaker-verification.md](docs/speaker-verification.md) |
| Background service | launchd / systemd / Task Scheduler templates | [docs/deployment.md](docs/deployment.md) |

**Not included.** Publishing to Notion or other tools, chat notifications (Slack and the like), and phone-to-computer intake are deliberately left out. A cloud-synced folder (iCloud Drive, Google Drive, Dropbox) works as the inbox with no integration at all, and anything else can be built around `results/`, which is plain files.

## Why another whisper + pyannote script?

One-shot diarization scripts treat every recording as a room full of strangers. Recurring groups aren't: it's the same six people every other week. moim-scribe exploits that.

- **Accumulating voice fingerprints.** Confirm who's who once, and each speaker's ECAPA embedding is stored and refined (speech-seconds weighted average) every time they appear. `SPEAKER_03` becomes a real name automatically from the second session on.
- **Merge repair.** pyannote's classic failure — two similar voices fused into one cluster — is detected by re-checking segments against enrolled fingerprints, and the transcript is re-labeled. Unenrolled guests are never touched. Relabeling only applies when every anchor shows segment-level evidence of actually being in the recording; otherwise the repair is withheld and the reason is recorded (a wrong name is worse than no name).
- **Two-voice warning.** After identification, each speaker's long stretches of speech (8 seconds or more, at least five of them) are split two ways. If the smaller part holds 10% or more of the time and the two parts sound different, the log says "two voices suspected" and `speakers.json` marks the label. Labels are never changed by this; it tells you which label to listen to before enrolling it. A label with one second of sound or less that matched nobody is treated as noise: it stays in the transcript but is not counted as an attendee, and the summary is told so.
- **Anti-hallucination transcription.** Whisper's repetition loops (the same phrase 17 times at exactly 1.0s intervals) are suppressed at decode time and collapsed deterministically afterwards.
- **One summary, no sorting.** You never tell it what kind of recording this is. The summary keeps the sections that have something in them (decisions, action items, open items) and drops the rest, so a meeting reads like minutes and a catch-up reads like a catch-up. Optionally, a recurring group's roster and running context ride along in the prompt: [docs/packs.md](docs/packs.md).
- **Honest labels.** Verification tooling (`moim-scribe verify`) cross-checks finished transcripts against fingerprints and your attendee roster, following rules learned from real mislabeling incidents. See [docs/speaker-verification.md](docs/speaker-verification.md).

## Platform support

| | STT backend | status |
|---|---|---|
| macOS (Apple Silicon) | `mlx` (mlx-whisper) | tier 1 — this is the config the pipeline was built and field-tested on |
| Windows / Linux / Intel Mac | `faster` (faster-whisper) | supported by design, CI-tested at unit level — field reports welcome |
| any platform | `soniox` (cloud) | no local model, no Hugging Face token — but the audio is uploaded. See below |

pyannote and speechbrain are PyTorch and run everywhere (CUDA, MPS, CPU). The watcher uses `watchdog` (native events on all three OSes).

## Transcription backends

| backend | where it runs | speaker separation | notes |
|---|---|---|---|
| `mlx` (default on Apple Silicon) | your machine | pyannote, locally | takes a speaker count from the filename or a spoken cue |
| `faster` (default elsewhere) | your machine | pyannote, locally | same |
| `soniox` | Soniox's servers — **the audio is uploaded** | Soniox, or pyannote locally if you have a token | no model download required. Needs an API key and is billed per hour of audio |

`soniox` is fast and accurate **when people take turns speaking**; on a noisy recording where several people talked over each other, testing found it fusing distinct speakers into one. If a Hugging Face token is present, moim-scribe fixes that by keeping Soniox's text and re-deriving the speaker boundaries with pyannote locally — on by default, skipped silently without a token, and the only way a speaker count in the filename does anything on this path. Soniox alone accepts no count: diarization is a single on/off switch that detects up to 15 speakers by itself. A single file is capped at 300 minutes.

Setup and the quota you have to manage: [docs/setup-soniox.md](docs/setup-soniox.md).

## Install

You need Python 3.11, 3.12 or 3.13, `git`, and [ffmpeg](https://ffmpeg.org) (it provides `ffmpeg` and `ffprobe`). Each block below starts from nothing: it creates a virtual environment first, so the `moim-scribe` command exists only while that environment is active (activate it again in every new terminal). Use `[mlx]` on an Apple Silicon Mac and `[cross]` everywhere else.

**What to expect.** Installing takes a couple of minutes and about 1.6 GB of disk for the virtual environment (measured on Apple Silicon with Python 3.12: 134 s). The first recording downloads about 1.6 GB of models once (measured: about 3 minutes), and the first `enroll` downloads the speaker model (about 170 MB). The first `doctor` can sit silent for up to a minute while it imports the heavy packages.

**macOS, Apple Silicon**

```bash
brew install git ffmpeg
python3 -m venv ~/moim-scribe-env
source ~/moim-scribe-env/bin/activate
pip install "moim-scribe[mlx] @ git+https://github.com/kemy-ai/moim-scribe"
moim-scribe --version
```

**macOS, Intel**

```bash
brew install git ffmpeg
python3 -m venv ~/moim-scribe-env
source ~/moim-scribe-env/bin/activate
pip install "moim-scribe[cross] @ git+https://github.com/kemy-ai/moim-scribe"
moim-scribe --version
```

**Windows (PowerShell)**

```powershell
winget install Git.Git
winget install Gyan.FFmpeg
py -m venv $HOME\moim-scribe-env
& $HOME\moim-scribe-env\Scripts\Activate.ps1
pip install "moim-scribe[cross] @ git+https://github.com/kemy-ai/moim-scribe"
moim-scribe --version
```

Open a new PowerShell window after the winget installs so `git` and `ffmpeg` are on PATH.

**Linux (Debian / Ubuntu shown; use your package manager)**

```bash
sudo apt install git ffmpeg python3-venv
python3 -m venv ~/moim-scribe-env
source ~/moim-scribe-env/bin/activate
pip install "moim-scribe[cross] @ git+https://github.com/kemy-ai/moim-scribe"
moim-scribe --version
```

One-time: pyannote's diarization model is gated, so you need a free Hugging Face token and you must accept the terms of three model pages. It takes a few minutes: [docs/setup-pyannote.md](docs/setup-pyannote.md). Not needed with `stt_backend: soniox`.

## Quickstart

With the virtual environment active:

```bash
moim-scribe init ~/MyMeetings --language en   # creates the workspace (default language: ko)
cd ~/MyMeetings
```

Save your Hugging Face token into the workspace, then check the setup. macOS / Linux:

```bash
echo "hf_..." > .hf_token
```

Windows PowerShell (`echo ... >` writes UTF-16 there; this does not):

```powershell
Set-Content -Path .hf_token -Value "hf_..." -Encoding ascii
```

```bash
moim-scribe doctor --online
```

`doctor` checks ffmpeg, the backend packages and the token, and prints where your audio and your transcript will go. With `--online` it also asks Hugging Face whether your token can open each of the three gated model pages, and names the page that still needs "Agree and access repository". Without `--online` it never touches the network.

Then drop a recording into the inbox (any kind of recording) and start the watcher:

```bash
cp recording.m4a inbox/
moim-scribe watch
```

Use `moim-scribe scan-inbox` instead of `watch` for a single pass. Commands take the workspace folder as an argument, or use the current folder when you run them inside it. Every command except `init` refuses a folder that has no `config.yaml`.

Results land in `results/` as `YYMMDD_title_summary.md` plus the full diarized transcript. Want transcripts only? Set `summarizer: none`. Tips that remove guesswork:

- put the attendee count at the end of a word in the filename: `book club 5명.m4a` (language `ko`) or `standup 4p.m4a` (other languages)
- or say it at the start of the recording ("참석자 다섯 명입니다" / "five participants today")

The filename count is recognized only in these forms: with `language: ko`, a number of one or two digits followed by `명` or `인` (`5명`, `3인`) that is not followed by another Korean syllable; with any other language, a number followed by `p`, `ppl`, `people` or `speakers` as a whole word (`4p`, `4 people`, `4 speakers`). Only 1 to 30 counts. A date such as `1103`, a year, or a word such as `3인칭` or `인터뷰` is not read as a count, and a title that holds no count is left untouched.

If setup is incomplete (no token, no ffmpeg, a missing backend package, no summarizer tool), `watch`, `scan-inbox` and `process` say what is missing and leave every recording where it is; nothing is claimed or moved until the setup is complete. `scan-inbox` exits with status 1 on a setup problem or when a recording failed.

## Teaching it who is who

The examples use one recording, saved as `weekly sync 2p.m4a`. After it has been processed, find the real names with `ls results` and `ls inbox/_done`: the results carry the title without the count hint (`results/YYMMDD_weekly sync_diarized.jsonl`, `..._transcript.txt`, `..._summary.md`), while the original files in `inbox/_done/` keep the full name (`weekly sync 2p.m4a`, `weekly sync 2p_transcript_diarized.txt`, `weekly sync 2p_speakers.json`). Names with spaces need quotes. Run these from the workspace folder:

```bash
# after the first processed session, map labels to names once.
# enroll reads the .jsonl (segment start/end live only there), kept in results/;
# the original audio is in inbox/_done/. Replace YYMMDD with the date in the file name:
moim-scribe enroll "inbox/_done/weekly sync 2p.m4a" "results/YYMMDD_weekly sync_diarized.jsonl" SPEAKER_00="Kim Haneul" SPEAKER_02="Lee Bada"

# optional: say which group someone belongs to, using the pack name.
# --attends-all is for whoever is in every recording (usually you).
moim-scribe enroll "inbox/_done/weekly sync 2p.m4a" "results/YYMMDD_weekly sync_diarized.jsonl" SPEAKER_00="Kim Haneul" --group book-club
moim-scribe enroll "inbox/_done/weekly sync 2p.m4a" "results/YYMMDD_weekly sync_diarized.jsonl" SPEAKER_01="Park Gureum" --attends-all

moim-scribe speakers      # see accumulated fingerprints and their groups

# from the next recording on those people are named automatically; verify a finished transcript anytime (this one reads the .txt):
moim-scribe verify "inbox/_done/weekly sync 2p.m4a" "inbox/_done/weekly sync 2p_transcript_diarized.txt" --expected "Kim Haneul,Lee Bada,Park Gureum"
```

Verifying the very recording you enrolled from still shows `SPEAKER_NN` labels: enrollment does not rewrite that transcript, and names appear from the next recording on.

Only enroll ranges a human has confirmed — the discipline that keeps the database trustworthy is in [docs/speaker-verification.md](docs/speaker-verification.md).

`enroll` refuses two things, because a wrong enrollment does not fail, it makes every later recording slightly more wrong:

- **One name on two labels of the same recording.** Usually a label number copied from a different run. If one person really was split into two labels, add `--allow-duplicate`.
- **A voice that scores below 0.5 against that person's existing fingerprint.** Add `--force` after listening. 0.5 is not a safety line: the same person can score lower on phone audio or against a thin fingerprint.

A label that is not in the jsonl is refused too. The others in the same command are still enrolled, and the command exits with status 1 when anything was refused.

If a name repeats ("weekly sync.m4a" every week), nothing is overwritten: the second one is kept as `weekly sync (2).m4a` in `inbox/_done/`, and its results as `YYMMDD_weekly sync (2)_…` when both land on the same day. Use the name the log printed, or `ls`, when you run `enroll` or `verify`.

**Group tags are a hint, not a rule.** A fingerprint is compared against everyone enrolled, so as the database grows a name from another recurring group can win a match. When that happens the run logs a line naming who and from where — and changes nothing else. Blocking on it was measured and rejected: people do join a second group or visit as guests, the score ranges overlap, and a tag nobody remembered to update would silently turn a correct match into an unknown. Untagged speakers, unmatched recordings, and anyone marked `--attends-all` are skipped.

## Summarizer backends

| backend | needs | transcript leaves your machine? |
|---|---|---|
| `claude_cli` (default) | Claude Code installed | yes — sent to Anthropic via your existing login |
| `anthropic_api` | `ANTHROPIC_API_KEY` | yes — sent to Anthropic's Messages API |
| `ollama` | Ollama running locally | no — nothing leaves at this step |
| `none` | nothing | no — stops at the diarized transcript |

With `claude_cli`, the CLI is started with no tools, no MCP servers, and neither your global instructions nor your user settings, and it runs from an empty temporary directory: the transcript is only ever read as text, and the summary does not depend on how that machine's Claude Code is set up. The summary is written in the language of the transcript. A Claude Code version too old for these flags is refused with a message to update it or choose another summarizer; it is not retried without them.

Switch in `config.yaml`. The summary template ships in Korean and English (`--language`) and lives in your workspace at `packs/lenses/summary.<language>.md`; edit it to change what every summary looks like. This table is about the transcript only — whether the *audio* leaves is the separate `stt_backend` choice above.

## Privacy and consent — read this

Voice fingerprints are **biometric personal data**, and recordings capture private conversation.

- **Record and enroll only with every participant's consent.** In many jurisdictions this is a legal requirement, not a courtesy — e.g. under Korea's Personal Information Protection Act, voiceprints used to identify a person are sensitive biometric information requiring separate explicit consent.
- Fingerprinting is always local, and after the first model download that step is forced offline. Two steps can reach the network, and only if you configure them to: `stt_backend: soniox` uploads the **audio**, and a cloud summarizer sends the **transcript** (with the recording title and time, speaker statistics, and your pack background when a pack matches). The defaults keep the audio home.
- The workspace keeps fingerprints (`speakers/`), recordings, and rosters outside the repo, and `init` drops a defensive `.gitignore` in the workspace (it also covers `packs/` and `glossary.txt`). Never publish a workspace.

Details: [docs/privacy.md](docs/privacy.md).

## Running it in the background

launchd (macOS), systemd (Linux), and Task Scheduler (Windows) templates: [docs/deployment.md](docs/deployment.md). Cloud-synced folders (iCloud Drive, Google Drive, Dropbox) work as the inbox: before touching a file the pipeline checks that it decodes and, on macOS, that it is physically on disk or reads back whole. A file that is still downloading reports its final size and plays from the start, so size and a decode probe alone let a partly downloaded recording through.

## When something goes wrong

- **Setup problems stop before touching your recordings.** No token, no ffmpeg, a missing backend package or a missing summarizer tool: `watch` / `scan-inbox` / `process` print what is missing and leave every recording in the inbox. Fix it and the next pass picks them up. `watch` keeps running and checks again each pass.
- **`inbox/_error/`** holds a recording that failed on its own (it could not be decoded, the filename was too long, a leftover from a run that stopped halfway). Read the log line for the reason, fix it, and move the file back into `inbox/` to retry.
- **A failed summary does not lose the transcript.** The diarized transcript is written to `results/` before the summary is attempted, so you keep it even when the summarizer failed. Move the recording back to `inbox/` to try the summary again.
- **A file that stays in `inbox/` and logs a warning** is either still downloading or not a valid audio file. If it is not downloading, remove it from `inbox/`.
- `scan-inbox` exits with status 1 when a recording failed or the setup is incomplete, so a scheduler can notice.

## Maintenance

Best-effort: issues and PRs are read, response time is not guaranteed. Forks welcome.

## Upgrading from 0.1

0.2 removes recording types, but an upgrade changes nothing about which files are processed or summarized until you opt in:

- **If `config.yaml` still has a `types:` block**, exactly the subfolders it names are scanned, as in 0.1: not `inbox/` itself and not other subfolders. Folder names are compared ignoring case and Unicode form (`Private` matches `private`). A folder mapped to `null` (by default `inbox/transcript/`) stays transcript-only, so those transcripts never start going to a summarizer. The block no longer selects a lens, and a log line says so. If recordings sit in `inbox/` itself or in other subfolders while the block is there, every pass logs one warning with their count and the remedy: delete the `types:` block from `config.yaml` to use the flat inbox. For transcripts only everywhere, set `summarizer: none`.
- **Without a `types:` block** (a new 0.2 workspace, or after you delete it), `inbox/` and its direct subfolders are scanned, and recordings are processed the same wherever they sit.
- **Lenses.** `meeting`, `gathering` and `conversation` are gone, along with `packs/styles/`. Run `moim-scribe init <workspace>` again: it adds the missing lens files and never touches `config.yaml`. A workspace with no `packs/lenses/summary.<language>.md` uses the built-in one meanwhile. Old lens files left in `packs/lenses/` are harmless and unused.
- **Packs.** `"type"` and `"style"` in `match.json` are ignored; the title keyword alone decides.
- **File names.** Summaries are now `YYMMDD_title_summary.md`.
- **`moim-scribe process`** no longer needs `--type` (it is accepted and ignored). It works on a private copy and refuses a file that is inside the workspace `inbox/`.
- **`init` is safe to run again.** It never overwrites `config.yaml`, it adds missing lens files and `.gitignore` entries, and `--force` is now a no-op. `--reset-config` writes a fresh config after saving the old one as `config.yaml.bak`.
- **`config.yaml` is validated when loaded.** An unknown key is reported, a wrong value is an error before anything is transcribed, `summarizer: null` means `none`, and `stt` is accepted as another spelling of `stt_backend`.

## Roadmap (not in v0.2)

- Publishing plugins (Notion etc.) and completion notifications
- English field-testing of the `faster` backend at tier-1 level

## License

[MIT](LICENSE). Depends on openly licensed projects (whisper implementations MIT, pyannote.audio MIT, speechbrain and watchdog Apache-2.0); the gated pyannote **model weights** have their own access terms you accept on Hugging Face — they are downloaded to your machine, never redistributed here.
