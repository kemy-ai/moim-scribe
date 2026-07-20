"""Workspace configuration.

A workspace is one visible folder holding everything moim-scribe touches:

    MyMeetings/
    ├── config.yaml        this file
    ├── glossary.txt       optional whisper prompt (domain proper nouns)
    ├── inbox/             drop recordings into a type subfolder
    │   ├── meeting/
    │   ├── gathering/
    │   └── transcript/
    ├── results/           summaries + transcripts land here
    ├── packs/             lenses/ styles/ and per-series packs
    ├── speakers/          voice-fingerprint DB (biometric — never share)
    └── models/            local model caches

Keeping user data in a plain folder the user chose (instead of inside a
repo clone) is itself a privacy feature: there is nothing to accidentally
commit.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_TYPES = {"meeting": "meeting", "gathering": "gathering", "transcript": None}

CONFIG_NAME = "config.yaml"

AUDIO_EXTS = (".m4a", ".mp3", ".wav", ".flac", ".ogg", ".aac", ".opus")


@dataclass
class Config:
    workspace: Path
    language: str = "ko"
    stt_backend: str = "mlx"
    stt_model: str | None = None
    summarizer: str = "claude_cli"
    summarizer_model: str | None = None
    summarizer_timeout_sec: int = 900
    # inbox subfolder name -> lens name (packs/lenses/<lens>.<language>.md).
    # A None lens means transcribe-only.
    types: dict = field(default_factory=lambda: dict(DEFAULT_TYPES))
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
        "summarizer",
        "summarizer_model",
        "summarizer_timeout_sec",
        "default_speakers",
    ):
        if key in raw and raw[key] is not None:
            setattr(cfg, key, raw[key])
    if isinstance(raw.get("types"), dict):
        cfg.types = raw["types"]
    return cfg


def ensure_workspace_dirs(cfg: Config) -> None:
    """Create the workspace skeleton (idempotent)."""
    for d in (
        cfg.inbox_dir,
        cfg.results_dir,
        cfg.packs_dir / "lenses",
        cfg.packs_dir / "styles",
        cfg.speakers_dir,
        cfg.models_dir,
        cfg.processing_dir,
        cfg.done_dir,
        cfg.error_dir,
    ):
        d.mkdir(parents=True, exist_ok=True)
    for type_folder in cfg.types:
        (cfg.inbox_dir / type_folder).mkdir(parents=True, exist_ok=True)
