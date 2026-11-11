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
import platform
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .stt.base import DEFAULT_MODELS as _STT_MODELS
from .summarize.base import DEFAULT_MODELS as _SUMMARIZER_MODELS

logger = logging.getLogger("moim_scribe.config")

# The one summary template every recording goes through
# (packs/lenses/<SUMMARY_LENS>.<language>.md). There used to be one lens per
# recording type, chosen by inbox subfolder; sorting a recording into a type
# before it could be processed was a decision the user had to make every time
# and the pipeline gained little from it.
SUMMARY_LENS = "summary"

CONFIG_NAME = "config.yaml"

# Valid config values, derived from the backend registries so a new backend
# only has to be registered once. "soniox" is dispatched separately in the
# pipeline (it is not a whisper backend), "none" skips summarizing.
KNOWN_STT_BACKENDS = tuple(_STT_MODELS) + ("soniox",)
KNOWN_SUMMARIZERS = tuple(_SUMMARIZER_MODELS) + ("none",)

# Every top-level key load_config understands (anything else gets a warning).
_SCALAR_KEYS = (
    "language",
    "stt_backend",
    "stt_model",
    "soniox_region",
    "hybrid_diarization",
    "summarizer",
    "summarizer_model",
    "summarizer_timeout_sec",
    "default_speakers",
)
_KNOWN_KEYS = frozenset(_SCALAR_KEYS) | {"stt", "types"}

SONIOX_REGIONS = ("us", "eu", "jp")

# What a bare `types:` (the key with nothing after it) meant in 0.1: the
# built-in map. Two folders were summarized, `transcript/` was transcribed only.
LEGACY_DEFAULT_TYPES = {"meeting": "meeting", "gathering": "gathering", "transcript": None}


class ConfigError(ValueError):
    """config.yaml is unusable. The message says what to fix; the CLI prints it."""


def default_stt_backend() -> str:
    """mlx only where mlx-whisper can run (macOS on Apple Silicon), else faster."""
    if sys.platform == "darwin" and platform.machine() == "arm64":
        return "mlx"
    return "faster"


def normalize_folder_name(name: str) -> str:
    """Key for comparing inbox folder names with names from a legacy `types:` block.

    NFC + casefold, so `Private` matches `private` and a composed Hangul name
    matches the decomposed form some filesystems report. Callers must pass the
    folder name through this same function before testing membership in
    `Config.legacy_type_folders` / `Config.transcript_only_folders`.
    """
    return unicodedata.normalize("NFC", str(name)).casefold()


AUDIO_EXTS = (".m4a", ".mp3", ".wav", ".flac", ".ogg", ".aac", ".opus")


@dataclass
class Config:
    workspace: Path
    language: str = "ko"
    # "mlx" / "faster" transcribe locally. "soniox" uploads the audio to
    # Soniox, which returns it already diarized — a different pipeline, not
    # just a different transcriber, and the only option that sends audio off
    # the machine. See docs/setup-soniox.md.
    stt_backend: str = field(default_factory=default_stt_backend)
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
    # Both sets hold normalize_folder_name() forms; normalize a folder name the
    # same way before comparing.
    transcript_only_folders: frozenset = frozenset()
    # None = config.yaml has no `types:` block (0.2 behaviour: root + direct
    # subfolders). A set (possibly empty) = the block is there: 0.1 scanned
    # only these folders, and the upgrade must not change what is processed.
    legacy_type_folders: frozenset | None = None
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


def _bad(msg: str) -> ConfigError:
    return ConfigError(f"{CONFIG_NAME}: {msg}")


def _hint(value: str, known: tuple) -> str:
    folded = value.strip().casefold()
    for k in known:
        if folded == k:
            return f" (did you mean '{k}'? values are case-sensitive)"
    return ""


def _validate(cfg: Config) -> None:
    """Reject values that would only fail later, mid-recording."""
    if not isinstance(cfg.language, str) or not cfg.language.strip():
        raise _bad(f"language must be a short text such as 'en' or 'ko', got {cfg.language!r}")
    if not isinstance(cfg.stt_backend, str) or cfg.stt_backend not in KNOWN_STT_BACKENDS:
        hint = _hint(cfg.stt_backend, KNOWN_STT_BACKENDS) if isinstance(cfg.stt_backend, str) else ""
        raise _bad(
            f"unknown stt_backend {cfg.stt_backend!r}{hint}: expected one of "
            f"{', '.join(KNOWN_STT_BACKENDS)}"
        )
    if not isinstance(cfg.summarizer, str) or cfg.summarizer not in KNOWN_SUMMARIZERS:
        hint = _hint(cfg.summarizer, KNOWN_SUMMARIZERS) if isinstance(cfg.summarizer, str) else ""
        raise _bad(
            f"unknown summarizer {cfg.summarizer!r}{hint}: expected one of "
            f"{', '.join(KNOWN_SUMMARIZERS)}"
        )
    spk = cfg.default_speakers
    if isinstance(spk, bool) or not isinstance(spk, int) or spk < 0:
        raise _bad(f"default_speakers must be a whole number >= 0, got {spk!r}")
    t = cfg.summarizer_timeout_sec
    if isinstance(t, bool) or not isinstance(t, (int, float)) or t <= 0:
        raise _bad(f"summarizer_timeout_sec must be a number > 0, got {t!r}")
    # A quoted "false" is a non-empty string, so it would read as true.
    if not isinstance(cfg.hybrid_diarization, bool):
        raise _bad(
            f"hybrid_diarization must be true or false (without quotes), got "
            f"{cfg.hybrid_diarization!r}"
        )
    if not isinstance(cfg.soniox_region, str) or cfg.soniox_region not in SONIOX_REGIONS:
        hint = _hint(cfg.soniox_region, SONIOX_REGIONS) if isinstance(cfg.soniox_region, str) else ""
        raise _bad(
            f"unknown soniox_region {cfg.soniox_region!r}{hint}: expected one of "
            f"{', '.join(SONIOX_REGIONS)}"
        )


def load_config(workspace: str | Path) -> Config:
    """Load <workspace>/config.yaml over defaults. Missing file = defaults.

    Raises ConfigError for a file that cannot be used (not a mapping, invalid
    YAML, conflicting or invalid values) so a typo fails before any recording
    is touched instead of silently falling back to cloud defaults.
    """
    ws = Path(workspace).expanduser().resolve()
    cfg_path = ws / CONFIG_NAME
    raw: dict = {}
    if cfg_path.exists():
        try:
            loaded = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise _bad(f"not valid YAML: {str(exc).splitlines()[0] if str(exc) else exc}") from exc
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            raise _bad(
                "must be a mapping of `key: value` lines, got "
                f"{type(loaded).__name__}; see the config.yaml that `moim-scribe init` writes"
            )
        raw = loaded

    unknown = sorted(str(k) for k in raw if k not in _KNOWN_KEYS)
    if unknown:
        logger.warning(
            "config.yaml: unknown key(s) ignored: %s (typo? known keys: %s)",
            ", ".join(unknown), ", ".join(sorted(_KNOWN_KEYS - {"types"})),
        )

    if raw.get("stt") is not None:
        if raw.get("stt_backend") is not None and raw["stt"] != raw["stt_backend"]:
            raise _bad(
                f"`stt: {raw['stt']}` and `stt_backend: {raw['stt_backend']}` disagree: "
                "they are the same setting, keep only stt_backend"
            )
        if raw.get("stt_backend") is None:
            # The loader before 0.2 ignored `stt:`. Honoring it now can change
            # what leaves the machine after an upgrade, so it is said out loud.
            upload = (
                " This UPLOADS the audio of every recording to Soniox."
                if raw["stt"] == "soniox" else ""
            )
            logger.warning(
                "config.yaml: `stt` is read as `stt_backend`: %s.%s Rename the key to "
                "stt_backend to make this explicit.", raw["stt"], upload,
            )
        raw = {**raw, "stt_backend": raw["stt"]}

    cfg = Config(workspace=ws)
    for key in _SCALAR_KEYS:
        if key in raw and raw[key] is not None:
            setattr(cfg, key, raw[key])
    if "summarizer" in raw and (raw["summarizer"] is None or raw["summarizer"] is False):
        # YAML turns a bare `none`-ish intent into null/false; a user writing
        # that means "no summarizer", not "the default cloud one".
        cfg.summarizer = "none"
        logger.info("config.yaml: summarizer is %s, treated as 'none' (transcripts only)", raw["summarizer"])
    _validate(cfg)

    if "types" in raw:
        # Workspaces created before 0.2 carry a `types:` map. 0.1 scanned only
        # the folders it names, so while the block is there so do we, and
        # folders mapped to null stay transcript-only (a folder someone chose
        # so that its transcripts are not sent to a summarizer must not start
        # sending them on upgrade). A bare `types:` meant 0.1's built-in map,
        # so it is read as that map and not as "no block".
        legacy = LEGACY_DEFAULT_TYPES if raw["types"] is None else raw["types"]
        if not isinstance(legacy, dict):
            raise _bad("`types:` must be a mapping of folder names (it is a leftover of 0.1)")
        cfg.legacy_type_folders = frozenset(normalize_folder_name(n) for n in legacy)
        cfg.transcript_only_folders = frozenset(
            normalize_folder_name(n) for n, lens in legacy.items() if lens is None
        )
        kept = (
            f" Folders it mapped to null ({', '.join(sorted(cfg.transcript_only_folders))}) "
            "stay transcript-only for as long as the block is there."
            if cfg.transcript_only_folders else ""
        )
        logger.warning(
            "config.yaml: `types:` is a 0.1 setting. While it is there only the folders "
            "it names are scanned (as in 0.1) and every recording gets the same summary."
            + kept + " Delete the `types:` block to use the flat inbox; for transcripts "
            "only everywhere, set `summarizer: none`."
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
