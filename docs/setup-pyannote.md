# One-time pyannote setup (Hugging Face token + gated model terms)

The diarization model is free but *gated*: Hugging Face requires you to
accept its terms once per account before it can be downloaded. This is the
single most common place setups fail — do these four steps in order.

> Needed for the local backends (`mlx`, `faster`) only. With
> `stt_backend: soniox` the speaker split happens server-side, so there is no
> token and no gated model to accept — at the cost of uploading the recording.
> See [setup-soniox.md](setup-soniox.md).

1. Create a free account at [huggingface.co](https://huggingface.co).
2. Open both model pages **and click "Agree and access repository" on each**:
   - <https://huggingface.co/pyannote/speaker-diarization-3.1>
   - <https://huggingface.co/pyannote/segmentation-3.0>
3. Create a token: Settings → Access Tokens → New token, type **read**.
4. Give the token to moim-scribe (either way works):

```bash
# option A — a file in your workspace (created by `moim-scribe init`)
echo "hf_xxxxxxxxxxxx" > ~/MyMeetings/.hf_token

# option B — environment variable
export HF_TOKEN=hf_xxxxxxxxxxxx        # Windows PowerShell: $env:HF_TOKEN="hf_..."
```

Then check:

```bash
moim-scribe doctor ~/MyMeetings
```

## Troubleshooting

- **401 / "gated repo" errors during the first run** — step 2 was skipped for
  one of the two models, or the token belongs to a different account.
- **It downloads every run** — the models cache under your Hugging Face cache
  directory; if that gets wiped (CI containers, cleanup tools), downloads recur.
- The speaker-fingerprint model (speechbrain ECAPA) is **not** gated and needs
  no token; it caches into `<workspace>/models/` and afterwards runs offline
  by design (fingerprinting never touches the network once cached).
