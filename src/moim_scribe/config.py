"""Workspace configuration.

A workspace is one visible folder holding everything moim-scribe touches:

    MyMeetings/
    ├── config.yaml        this file
    ├── glossary.txt       optional whisper prompt (domain proper nouns)
    ├── inbox/             drop recordings here (any kind: they are all
    │                      handled the same way; subfolders are allowed and
    │                      mean nothing to the pipeline)
    ├── results/           summaries + transcripts land here
    ├── packs/             lenses/ (the summary template) and optional
    │                      per-series background packs
    ├── speakers/          voice-fingerprint DB (biometric — never share)
    └── models/            local model caches

Keeping user data in a plain folder the user chose (instead of inside a
repo clone) is itself a privacy feature: there is nothing to accidentally
commit.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

logger = logging.getLogger("moim_scribe.config")

# The one summary template every recording goes through
# (packs/lenses/<SUMMARY_LENS>.<language>.md). There used to be one lens per
# recording type, chosen by inbox subfolder; sorting a recording into a type
# before it could be processed was a decision the user had to make every time
# and the pipeline gained little from it.
SUMMARY_LENS = "summary"

CONFIG_NAME = "config.yaml"

AUDIO_EXTS = (".m4a", ".mp3", ".wav", ".flac", ".ogg", ".aac", ".opus")


@dataclass
class Config:
    workspace: Path
    language: str = "ko"
    # "mlx" / "faster" transcribe locally. "soniox" uploads the audio to
    # Soniox, which returns it already diarized — a different pipeline, not
    # just a different transcriber, and the only option that sends audio off
    # the machine. See docs/setup-soniox.md.
    stt_backend: str = "mlx"
    stt_model: str | None = None
    # Soniox region: us, eu or jp. eu and jp need activation by Soniox before
    # they will answer, so the default stays us.
    soniox_region: str = "us"
    # On the Soniox path, re-run pyannote locally and take its speaker
    # boundaries while keeping Soniox's text (see diarize.relabel_by_turns).
    # Default on, but it costs nothing to leave on: without a Hugging Face
    # token the step skips itself, so a Soniox-only setup is unaffected, and
    # having a token means pyannote is already installed and set up. Set false
    # to keep Soniox's own labels even when pyannote is available.
    hybrid_diarization: bool = True
    summarizer: str = "claude_cli"
    summarizer_model: str | None = None
    summarizer_timeout_sec: int = 900
    # Not a setting. Filled from a pre-0.2 `types:` block: the inbox subfolders
    # it mapped to null (transcribe only). They stay transcribe-only, because a
    # folder someone chose precisely so that its transcripts would not be sent
    # to a summarizer must not start sending them on upgrade.
    transcript_only_folders: frozenset = frozenset()
    # Default speaker count when neither the filename nor a spoken cue gives
    # one. 0 = let pyannote estimate (best for 3+ person meetings); set 2 for
    # workspaces that are mostly 1:1 conversations.
    default_speakers: int = 0

    @property
    def inbox_dir(self) -> Path:
        return self.workspace / "inbox"

    @property
    def results_dir(self) -> Path:
        return self.workspace / "results"

    @property
    def packs_dir(self) -> Path:
        return self.workspace / "packs"

    @property
    def speakers_dir(self) -> Path:
        return self.workspace / "speakers"

    @property
    def models_dir(self) -> Path:
        return self.workspace / "models"

    @property
    def embed_model_dir(self) -> Path:
        return self.models_dir / "spkrec-ecapa-voxceleb"

    @property
    def glossary_path(self) -> Path:
        return self.workspace / "glossary.txt"

    @property
    def soniox_key_path(self) -> Path:
        return self.workspace / ".soniox_key"

    @property
    def processing_dir(self) -> Path:
        return self.inbox_dir / "_processing"

    @property
    def done_dir(self) -> Path:
        return self.inbox_dir / "_done"

    @property
    def error_dir(self) -> Path:
        return self.inbox_dir / "_error"


def load_config(workspace: str | Path) -> Config:
    """Load <workspace>/config.yaml over defaults. Missing file = defaults."""
    ws = Path(workspace).expanduser().resolve()
    cfg_path = ws / CONFIG_NAME
    raw: dict = {}
    if cfg_path.exists():
        loaded = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            raw = loaded

    cfg = Config(workspace=ws)
    for key in (
        "language",
        "stt_backend",
        "stt_model",
        "soniox_region",
        "hybrid_diarization",
        "summarizer",
        "summarizer_model",
        "summarizer_timeout_sec",
        "default_speakers",
    ):
        if key in raw and raw[key] is not None:
            setattr(cfg, key, raw[key])
    if "types" in raw:
        # Workspaces created before 0.2 carry a `types:` map. Say it is no
        # longer read rather than ignoring it in silence: a user who mapped a
        # folder to "transcribe only" would otherwise start getting summaries
        # with no hint as to why.
        legacy = raw["types"] if isinstance(raw["types"], dict) else {}
        cfg.transcript_only_folders = frozenset(
            str(name) for name, lens in legacy.items() if lens is None
        )
        kept = (
            f" Folders it mapped to null ({', '.join(sorted(cfg.transcript_only_folders))}) "
            "stay transcript-only for as long as the block is there."
            if cfg.transcript_only_folders else ""
        )
        logger.warning(
            "config.yaml: `types:` is no longer used: every recording in inbox/ "
            "(and its subfolders) gets the same summary." + kept + " For transcripts "
            "only everywhere, set `summarizer: none` and delete the `types:` block."
        )
    return cfg


def ensure_workspace_dirs(cfg: Config) -> None:
    """Create the workspace skeleton (idempotent)."""
    for d in (
        cfg.inbox_dir,
        cfg.results_dir,
        cfg.packs_dir / "lenses",
        cfg.speakers_dir,
        cfg.models_dir,
        cfg.processing_dir,
        cfg.done_dir,
        cfg.error_dir,
    ):
        d.mkdir(parents=True, exist_ok=True)
