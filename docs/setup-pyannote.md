# One-time pyannote setup (Hugging Face token + gated model terms)

The diarization model is free but *gated*: Hugging Face requires you to
accept its terms once per account before it can be downloaded. This is the
single most common place setups fail — do these four steps in order.

> Needed for the local backends (`mlx`, `faster`) only. With
> `stt_backend: soniox` the speaker split happens server-side, so there is no
> token and no gated model to accept — at the cost of uploading the recording.
> See [setup-soniox.md](setup-soniox.md).

1. Create a free account at [huggingface.co](https://huggingface.co).
2. Open all three model pages **and click "Agree and access repository" on each**:
   - <https://huggingface.co/pyannote/speaker-diarization-3.1>
   - <https://huggingface.co/pyannote/segmentation-3.0>
   - <https://huggingface.co/pyannote/speaker-diarization-community-1>

   The third one is easy to miss because moim-scribe never names it: the code
   asks for `speaker-diarization-3.1`, and pyannote.audio 4.x pulls that
   repository's PLDA assets in as well. It is gated too, so skipping it fails
   the same way (observed on pyannote.audio 4.0.7, 2026-08-03).
3. Create a token: Settings → Access Tokens → New token, type **read**.
4. Give the token to moim-scribe (either way works):

```bash
# option A — a file in your workspace (macOS / Linux)
echo "hf_xxxxxxxxxxxx" > ~/MyMeetings/.hf_token
# Windows PowerShell (echo ... > writes UTF-16 there):
#   Set-Content -Path $HOME\MyMeetings\.hf_token -Value "hf_xxxxxxxxxxxx" -Encoding ascii

# option B — environment variable
export HF_TOKEN=hf_xxxxxxxxxxxx        # Windows PowerShell: $env:HF_TOKEN="hf_..."
```

Then check (run it inside the workspace, or pass its path):

```bash
moim-scribe doctor --online ~/MyMeetings
```

`doctor` also prints where your audio and transcript go. `--online` is the only
part that uses the network.

## Troubleshooting

- **401 / "gated repo" errors during the first run** — step 2 was skipped for
  one of the three models, or the token belongs to a different account. Run
  `moim-scribe doctor --online`: it asks Hugging Face whether your token can
  open each of the three pages and names the one that still needs "Agree and
  access repository". If it prints "could not check", the network or
  `huggingface_hub` was unavailable; that is not a verdict on the token.
- **It downloads every run** — the models cache under your Hugging Face cache
  directory; if that gets wiped (CI containers, cleanup tools), downloads recur.
- The speaker-fingerprint model (speechbrain ECAPA) is **not** gated and needs
  no token. It is copied into `<workspace>/models/` as real files (not links
  into the Hugging Face cache), so the workspace keeps working offline even
  after the cache is cleared, and fingerprinting never touches the network once
  it is there. The first `enroll` downloads it (about 170 MB).
