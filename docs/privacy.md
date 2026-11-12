# Privacy: recordings, voice fingerprints, and consent

## Acceptable use

moim-scribe exists for one purpose: groups recording **themselves**, with
everyone's informed consent, to keep better notes of their own meetings.

Do not use it to record people covertly, to identify speakers who have not
agreed to voice enrollment, to process recordings you were not a party to,
or to analyse conversations with a partner or family member without their
knowledge — a private relationship is where covert recording is most
tempting and least defensible, which is why the README opens with that
warning rather than burying it. Beyond being a violation
of this project's intent, covert recording and non-consensual biometric
identification are criminal or actionable offenses in many jurisdictions.

moim-scribe processes two kinds of highly personal data:

- **Recordings** — private conversations of identifiable people.
- **Voice fingerprints** — numeric embeddings that identify a person by
  their voice. These are *biometric* personal data.

## Consent is required, not polite

Get explicit consent from **every participant** for both recording and
fingerprint enrollment, and tell them how long you keep the data.

- In South Korea, the Personal Information Protection Act (개인정보보호법)
  treats characteristic voice data used to identify an individual as
  sensitive biometric information (민감정보/생체인식정보), requiring
  separate, explicit consent — plain "recording consent" is not enough.
- The EU GDPR similarly classifies biometric identification data as a
  special category (Art. 9) with a high consent bar.
- Rules differ by country and context (some US states, e.g. Illinois BIPA,
  have private rights of action for biometric collection). When in doubt,
  ask before you record, and honor deletion requests immediately.

A practical script for the start of a session: "I'm recording this to
generate our notes. The tool also keeps a voice profile per member so notes
can name speakers automatically — is everyone OK with that?"

## How the design protects data

- **Local by default, and the two exceptions are yours to set.**
  Fingerprinting is always local: after the first model download that model is
  forced offline (`HF_HUB_OFFLINE`), so matching voices never touches the
  network. Two other steps can, and neither does unless you configure it:
  - `stt_backend: soniox` uploads the **audio itself** to Soniox for transcription,
    and, when the workspace has a `glossary.txt`, sends its text along as
    context (it usually holds names of people and places). Soniox then
    produces the transcript on its servers. Once a recording is transcribed,
    or its transcription failed, moim-scribe asks Soniox to delete the uploaded
    file and the transcription, and logs what to delete by hand if that
    request fails. The recording was on a third party's servers in the
    meantime, and their retention is governed by their terms, not this tool's.
    The default backends (`mlx`, `faster`) keep the audio on your machine.
  - `claude_cli` / `anthropic_api` send the **transcript** to Anthropic. The
    prompt holds the transcript, the recording's title and time, the speaker
    statistics, and your pack background when a pack matches; never the audio
    and never a fingerprint. `claude_cli` starts Claude Code so that it keeps
    no session on disk, so the prompt is not saved in its session history. `ollama` / `none` do not send it to Anthropic.
    (`ollama` stays on your machine unless `OLLAMA_HOST` points at another
    server; `moim-scribe doctor` prints where the audio and the transcript go.)

  These are independent, so state them separately when you ask for consent:
  "the recording is uploaded for transcription" is a different thing to agree
  to than "the transcript is sent to an AI service", and a setup can do
  either, both, or neither.
- **Workspace separation.** All personal data (fingerprints in `speakers/`,
  recordings in `inbox/`, rosters in `packs/`) lives in a folder you chose,
  outside any git repository, and `moim-scribe init` writes a defensive
  `.gitignore` into the workspace in case one is ever created there (it
  ignores speakers, recordings, models, `packs/`, `glossary.txt` and the
  token files). Running `init` again adds any entry an older workspace lacks.
- **The speaker model is a real copy.** `<workspace>/models/` holds the speaker
  model as ordinary files, not links into the Hugging Face cache, so the
  workspace keeps working offline after the cache is cleared. The gated
  pyannote models stay in the Hugging Face cache.
- **Head counts you give are kept in a small local record.** When a recording
  without a count in its name is waiting, the workspace holds
  `.moim-scribe-headcount/` with up to two tiny files per recording: the
  recording's path inside `inbox/` (which includes its file name), the time the
  program first saw it, and the count you gave. It stays on your machine and is
  covered by the workspace `.gitignore`. The files are deleted when the
  recording is picked up for processing, and any left for a recording that is no
  longer in `inbox/` are removed at the next pass over the inbox.
- **Deletion is simple.** Delete `<workspace>/speakers/<name>.npy` and the
  person's entry in `speakers/index.json` to forget a voice; delete the
  workspace to forget everything.
- **Third-party telemetry is off.** pyannote.audio 4.x sends anonymous usage
  metrics by default; moim-scribe disables this (`PYANNOTE_METRICS_ENABLED`)
  unless you explicitly export it as `true` to support their project stats.

## Where Claude Code can see your files

Two things in 0.2.1 are new and are not part of the pipeline's own data flow.

- **Opening Claude Code in the workspace folder.** The workspace holds
  recordings, transcripts, summaries and fingerprints. If you open Claude Code
  there (for example to tell Claude a head count), Claude can read those files,
  and whatever it reads is sent to Anthropic as part of that conversation.
  `moim-scribe init` writes a `CLAUDE.md` that tells Claude not to open them
  unless you ask. That is an instruction to the model, not a lock: nothing
  prevents a file from being read. If that is not acceptable, do not open
  Claude Code in the workspace; give counts with `moim-scribe count`. The
  `claude_cli` summarizer is unaffected: it runs from an empty temporary
  folder, not from the workspace.
- **Slack notifications.** moim-scribe has no Slack connection. If you attach
  one by the guide in [notify-slack.md](notify-slack.md), the message carries the
  waiting recording's file name to Slack and to everyone who reads that channel.

## What you must never publish

Fingerprint files (`*.npy`, `speakers/index.json`), recordings, transcripts
of real sessions, rosters (`pack.md`), and round history (`_history/`).
The repository's `.gitignore` blocks these patterns — keep it that way when
you fork.
