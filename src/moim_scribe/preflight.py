"""Setup preflight: is everything this workspace's configuration needs present?

Runs before a single recording is claimed. A setup mistake (no Hugging Face
token, no ffmpeg, no summarizer tool) used to surface inside the per-file loop:
the first recording of every run went to _error, and a missing token raised
SystemExit and took `watch` down with it. Checked here first, a setup problem
stops the pass with every recording still where the user put it.

Every check is cheap and import-light on purpose (it runs on every pass of
`watch`): `importlib.util.find_spec` locates a package without executing it,
`shutil.which` looks at PATH, secrets are read from their files. Nothing heavy
(torch, pyannote, whisper) is imported here.

What each configuration needs (the SSOT; `doctor` reads the same list):

  always            ffmpeg and ffprobe on PATH (decoding, stability probes)
  stt mlx / faster  the backend package (mlx_whisper / faster_whisper),
                    pyannote.audio, and a Hugging Face token: local
                    diarization cannot run without any of them
  stt soniox        the Soniox API key. pyannote and the token are optional:
                    without them the hybrid step skips itself and Soniox's own
                    speaker labels are used
  speaker database  speechbrain, whenever speakers/index.json names anyone
                    (an empty or absent database is never compared against)
  summarizer        claude_cli: `claude` on PATH; anthropic_api:
                    ANTHROPIC_API_KEY; ollama and none: nothing checked here
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import shutil
import sys
from dataclasses import dataclass

from .config import Config
from .diarize import find_hf_token


@dataclass(frozen=True)
class Check:
    """One requirement of the configured setup and whether it is met."""

    label: str
    ok: bool
    # What to do when it is not met. Names the file or setting to touch.
    fix: str

    @property
    def problem(self) -> str:
        return f"{self.label}: {self.fix}"


def module_findable(name: str) -> bool:
    """Whether `import name` would find a module, without executing any of it.

    `importlib.util.find_spec("a.b")` imports the parent package `a` first,
    which runs its __init__. For a top-level name it only searches sys.path.
    So a dotted name is resolved by hand: locate the parent's spec, then look
    for the child in the parent's search locations with PathFinder, which
    reads the filesystem and runs nothing. Any error means "not findable"
    (a broken install is not a usable one).
    """
    if name in sys.modules:
        return True
    try:
        parts = name.split(".")
        spec = importlib.util.find_spec(parts[0])
        for part in parts[1:]:
            if spec is None or spec.submodule_search_locations is None:
                return False
            spec = importlib.machinery.PathFinder.find_spec(
                part, list(spec.submodule_search_locations)
            )
        return spec is not None
    except Exception:  # noqa: BLE001 (a probe must never take the caller down)
        return False


def _soniox_key_present(cfg: Config) -> bool:
    from .stt.soniox_client import get_api_key  # stdlib-only module

    try:
        return bool(get_api_key(cfg.workspace))
    except Exception:  # noqa: BLE001 (SonioxKeyMissing, or an unreadable file)
        return False


def _enrolled_count(cfg: Config) -> tuple[int | None, str]:
    """How many speakers the fingerprint database names.

    (count, "") when readable; (None, what is wrong with the file) otherwise.
    """
    path = cfg.speakers_dir / "index.json"
    if not path.exists():
        return 0, ""
    try:
        index = json.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        return None, f"cannot be read ({type(e).__name__}: {e})"
    except ValueError:
        return None, "is not valid JSON"
    if not isinstance(index, dict):
        # Valid JSON, wrong shape: the database maps each name to its entry.
        return None, (
            f"is valid JSON but holds a {type(index).__name__}, not the mapping of "
            "names the speaker database needs"
        )
    return len(index), ""


def checks(cfg: Config) -> list[Check]:
    """Every requirement of this configuration, met or not, in a stable order."""
    out = [
        Check("ffmpeg on PATH", shutil.which("ffmpeg") is not None,
              "install ffmpeg (brew install ffmpeg / winget install ffmpeg / apt install ffmpeg)"),
        Check("ffprobe on PATH", shutil.which("ffprobe") is not None,
              "install ffmpeg, which includes ffprobe"),
    ]

    if cfg.stt_backend == "soniox":
        out.append(Check(
            "Soniox API key", _soniox_key_present(cfg),
            f"set SONIOX_API_KEY or put the key in {cfg.soniox_key_path} "
            "(see docs/setup-soniox.md), or set stt_backend: mlx / faster in config.yaml",
        ))
    else:
        package, extra = {
            "mlx": ("mlx_whisper", "mlx"),
            "faster": ("faster_whisper", "cross"),
        }.get(cfg.stt_backend, ("faster_whisper", "cross"))
        out.append(Check(
            f"stt_backend {cfg.stt_backend}: {package} installed", module_findable(package),
            f'pip install "moim-scribe[{extra}]", or change stt_backend in config.yaml',
        ))
        out.append(Check(
            "pyannote.audio installed", module_findable("pyannote.audio"),
            "pip install moim-scribe (it is a core dependency of local transcription)",
        ))
        out.append(Check(
            "Hugging Face token", find_hf_token(cfg.workspace) is not None,
            f"set HF_TOKEN or put the token in {cfg.workspace / '.hf_token'} "
            "(see docs/setup-pyannote.md)",
        ))

    enrolled, what = _enrolled_count(cfg)
    index_path = cfg.speakers_dir / "index.json"
    if enrolled is None:
        out.append(Check(
            "speaker database readable", False,
            f"{index_path} {what}: restore it from a backup, or move it away "
            "to start with an empty speaker database",
        ))
    elif enrolled > 0:
        out.append(Check(
            "speechbrain installed (speakers are enrolled)", module_findable("speechbrain"),
            "pip install moim-scribe (speechbrain matches voices against "
            f"{index_path})",
        ))

    if cfg.summarizer == "claude_cli":
        out.append(Check(
            "claude CLI on PATH (summarizer: claude_cli)", shutil.which("claude") is not None,
            "install Claude Code, or set summarizer to anthropic_api / ollama / none in config.yaml",
        ))
    elif cfg.summarizer == "anthropic_api":
        out.append(Check(
            "ANTHROPIC_API_KEY set (summarizer: anthropic_api)",
            bool(os.environ.get("ANTHROPIC_API_KEY")),
            "export ANTHROPIC_API_KEY where moim-scribe runs, or set summarizer to "
            "claude_cli / ollama / none in config.yaml",
        ))
    return out


def preflight(cfg: Config) -> list[str]:
    """The setup problems of this workspace, one message each; empty = ready."""
    return [c.problem for c in checks(cfg) if not c.ok]
