"""moim-scribe command line interface.

    moim-scribe init [workspace]        create a workspace skeleton + example packs
    moim-scribe watch [workspace]       watch the inbox and process recordings
    moim-scribe scan-inbox [workspace]  one inbox pass (for cron / Task Scheduler)
    moim-scribe process AUDIO --type T  process a single file in place
    moim-scribe enroll AUDIO JSONL SPEAKER_00=Name ...   enroll fingerprints
    moim-scribe speakers [workspace]    list enrolled speakers
    moim-scribe verify AUDIO TXT        verify labels in a finished transcript
    moim-scribe doctor [workspace]      check the local setup
"""
from __future__ import annotations

import argparse
import importlib.resources
import logging
import shutil
import sys
from pathlib import Path

from . import __version__
from .config import Config, ensure_workspace_dirs, load_config

logger = logging.getLogger("moim_scribe")

CONFIG_TEMPLATE = """\
# moim-scribe workspace configuration
# docs: https://github.com/kemy-ai/moim-scribe

# Transcription language (ko / en / any whisper language code)
language: {language}

# STT backend: "mlx" (Apple Silicon, install moim-scribe[mlx])
#              "faster" (Windows / Linux / Intel Mac, install moim-scribe[cross])
stt_backend: {stt_backend}
# stt_model: null            # override the backend's default whisper model

# Summarizer backend: claude_cli | anthropic_api | ollama | none
#   claude_cli    uses your existing Claude Code login (no key needed)
#   anthropic_api needs ANTHROPIC_API_KEY
#   ollama        local model, fully offline (needs Ollama running)
#   none          stop after transcription — no LLM involved
summarizer: claude_cli
# summarizer_model: null     # e.g. sonnet / claude-sonnet-5 / llama3.1
# summarizer_timeout_sec: 900

# inbox subfolder -> lens (template in packs/lenses/). null = transcribe only.
types:
  meeting: meeting
  gathering: gathering
  transcript: null

# Speaker count when neither the filename ("... 5명" / "... 5p") nor a spoken
# cue at the start of the recording gives one. 0 = auto-estimate.
default_speakers: 0
"""


def _cfg(ws: str | None) -> Config:
    return load_config(ws or ".")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="[%(name)s] %(message)s",
        stream=sys.stderr,
    )


def cmd_init(args) -> int:
    ws = Path(args.workspace or ".").expanduser().resolve()
    ws.mkdir(parents=True, exist_ok=True)
    cfg_path = ws / "config.yaml"
    if cfg_path.exists() and not args.force:
        print(f"config.yaml already exists in {ws} (use --force to overwrite)")
        return 1
    stt_backend = "mlx" if (sys.platform == "darwin") else "faster"
    cfg_path.write_text(
        CONFIG_TEMPLATE.format(language=args.language, stt_backend=stt_backend),
        encoding="utf-8",
    )
    cfg = load_config(ws)
    ensure_workspace_dirs(cfg)

    templates = importlib.resources.files("moim_scribe") / "templates"
    for sub in ("lenses", "styles"):
        src_dir = templates / sub
        dst_dir = cfg.packs_dir / sub
        for item in src_dir.iterdir():
            dst = dst_dir / item.name
            if not dst.exists():
                dst.write_text(item.read_text(encoding="utf-8"), encoding="utf-8")
    examples_dir = templates / "examples"
    for pack in examples_dir.iterdir():
        dst_pack = cfg.packs_dir / pack.name
        if dst_pack.exists():
            continue
        dst_pack.mkdir(parents=True)
        for item in pack.iterdir():
            (dst_pack / item.name).write_text(item.read_text(encoding="utf-8"), encoding="utf-8")

    # A workspace-local safety net in case the user ever runs `git init` here.
    gi = ws / ".gitignore"
    if not gi.exists():
        gi.write_text(
            "# moim-scribe workspace: private by default\n"
            "speakers/\n*.npy\ninbox/\nresults/\nmodels/\n"
            "*.wav\n*.m4a\n*.mp3\n*.flac\n*.ogg\n*.aac\n*.opus\n"
            ".hf_token\n_history/\n",
            encoding="utf-8",
        )

    print(f"workspace ready: {ws}")
    print("next steps:")
    print("  1. get a Hugging Face token + accept the pyannote model terms")
    print(f"     (docs/setup-pyannote.md) and save it to {ws}/.hf_token")
    print("  2. drop a recording into inbox/meeting/ (or gathering/, transcript/)")
    print(f"  3. run: moim-scribe watch {ws}")
    return 0


def cmd_watch(args) -> int:
    from .watch import watch

    cfg = _cfg(args.workspace)
    ensure_workspace_dirs(cfg)
    watch(cfg, interval_sec=args.interval)
    return 0


def cmd_scan_inbox(args) -> int:
    from .pipeline import scan_inbox

    cfg = _cfg(args.workspace)
    ensure_workspace_dirs(cfg)
    counts = scan_inbox(cfg)
    print(counts)
    return 0


def cmd_process(args) -> int:
    from .pipeline import process_recording

    cfg = _cfg(args.workspace)
    ensure_workspace_dirs(cfg)
    src = Path(args.audio).expanduser().resolve()
    if not src.exists():
        print(f"not found: {src}")
        return 1
    if args.type not in cfg.types:
        print(f"unknown type '{args.type}' — config.yaml types: {', '.join(cfg.types)}")
        return 1
    # Work on a copy claimed into _processing; the user's original stays put.
    claimed = cfg.processing_dir / src.name
    shutil.copy2(src, claimed)
    try:
        out = process_recording(cfg, claimed, args.type)
        print(f"result: {out}")
        return 0
    finally:
        for leftover in cfg.processing_dir.glob(f"{claimed.stem}*"):
            leftover.unlink(missing_ok=True)


def cmd_enroll(args) -> int:
    from .speakers.db import SpeakerDB
    from .speakers.enroll import enroll, parse_mapping

    cfg = _cfg(args.workspace)
    try:
        mapping = parse_mapping(args.mapping)
    except ValueError as e:
        print(e)
        return 1
    enroll(args.audio, args.jsonl, mapping, SpeakerDB(cfg.speakers_dir), cfg.embed_model_dir)
    return 0


def cmd_speakers(args) -> int:
    from .speakers.db import SpeakerDB

    cfg = _cfg(args.workspace)
    index = SpeakerDB(cfg.speakers_dir).load_index()
    if not index:
        print("no speakers enrolled yet (moim-scribe enroll …)")
        return 0
    for name, entry in sorted(index.items()):
        print(
            f"{name}: {entry.get('total_sec', 0)}s across {entry.get('n_samples', 0)} recordings "
            f"(updated {str(entry.get('updated', ''))[:10]})"
        )
    return 0


def cmd_verify(args) -> int:
    from .speakers.db import SpeakerDB
    from .speakers.scan import scan

    cfg = _cfg(args.workspace)
    scan(
        args.audio,
        args.transcript,
        SpeakerDB(cfg.speakers_dir),
        cfg.embed_model_dir,
        axis=args.axis,
        expected=args.expected,
        margin=args.margin,
    )
    return 0


def cmd_doctor(args) -> int:
    cfg = _cfg(args.workspace)
    ok = True

    def check(label: str, passed: bool, hint: str = "") -> None:
        nonlocal ok
        mark = "OK " if passed else "FAIL"
        print(f"[{mark}] {label}" + (f" — {hint}" if hint and not passed else ""))
        ok = ok and passed

    check("ffmpeg on PATH", shutil.which("ffmpeg") is not None, "install ffmpeg (brew/winget/apt)")
    check("ffprobe on PATH", shutil.which("ffprobe") is not None, "comes with ffmpeg")

    if cfg.stt_backend == "mlx":
        try:
            import mlx_whisper  # noqa: F401
            check("stt backend mlx-whisper importable", True)
        except ImportError:
            check("stt backend mlx-whisper importable", False, 'pip install "moim-scribe[mlx]" (Apple Silicon only)')
    else:
        try:
            import faster_whisper  # noqa: F401
            check("stt backend faster-whisper importable", True)
        except ImportError:
            check("stt backend faster-whisper importable", False, 'pip install "moim-scribe[cross]"')

    try:
        import pyannote.audio  # noqa: F401
        check("pyannote.audio importable", True)
    except ImportError:
        check("pyannote.audio importable", False, "pip install moim-scribe")
    try:
        import speechbrain  # noqa: F401
        check("speechbrain importable", True)
    except ImportError:
        check("speechbrain importable", False, "pip install moim-scribe")

    import os
    token_present = bool(os.environ.get("HF_TOKEN")) or (cfg.workspace / ".hf_token").exists()
    check(
        "Hugging Face token (HF_TOKEN or <workspace>/.hf_token)",
        token_present,
        "see docs/setup-pyannote.md — token + model terms acceptance are required once",
    )

    if cfg.summarizer == "claude_cli":
        check("claude CLI on PATH", shutil.which("claude") is not None,
              "install Claude Code or switch summarizer in config.yaml")
    elif cfg.summarizer == "anthropic_api":
        check("ANTHROPIC_API_KEY set", bool(os.environ.get("ANTHROPIC_API_KEY")),
              "export ANTHROPIC_API_KEY or switch summarizer")
    elif cfg.summarizer == "ollama":
        print("[i  ] summarizer=ollama — ensure Ollama is running and the model is pulled")
    elif cfg.summarizer == "none":
        print("[i  ] summarizer=none — pipeline stops at transcripts")

    print("\nall good" if ok else "\nsome checks failed — see hints above")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="moim-scribe", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"moim-scribe {__version__}")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create a workspace skeleton")
    p.add_argument("workspace", nargs="?", default=".")
    p.add_argument("--language", default="ko", help="ko / en (default: ko)")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("watch", help="watch the inbox and process recordings")
    p.add_argument("workspace", nargs="?", default=".")
    p.add_argument("--interval", type=float, default=60.0, help="fallback scan interval seconds")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("scan-inbox", help="one inbox pass (cron friendly)")
    p.add_argument("workspace", nargs="?", default=".")
    p.set_defaults(func=cmd_scan_inbox)

    p = sub.add_parser("process", help="process one audio file in place")
    p.add_argument("audio")
    p.add_argument("--type", required=True, help="a type from config.yaml (e.g. meeting)")
    p.add_argument("--workspace", default=".")
    p.set_defaults(func=cmd_process)

    p = sub.add_parser("enroll", help="enroll speaker fingerprints from a diarized recording")
    p.add_argument("audio")
    p.add_argument("jsonl", help="the *_transcript_diarized.jsonl next to the recording")
    p.add_argument("mapping", nargs="+", help="SPEAKER_NN=Name …")
    p.add_argument("--workspace", default=".")
    p.set_defaults(func=cmd_enroll)

    p = sub.add_parser("speakers", help="list enrolled speakers")
    p.add_argument("workspace", nargs="?", default=".")
    p.set_defaults(func=cmd_speakers)

    p = sub.add_parser("verify", help="verify speaker labels in a finished transcript")
    p.add_argument("audio")
    p.add_argument("transcript")
    p.add_argument("--axis", default=None, help="scan from these speakers' viewpoint (comma-separated)")
    p.add_argument("--expected", default=None, help="attendee roster (comma-separated) to cross-check")
    p.add_argument("--margin", type=float, default=0.03)
    p.add_argument("--workspace", default=".")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("doctor", help="check the local setup")
    p.add_argument("workspace", nargs="?", default=".")
    p.set_defaults(func=cmd_doctor)

    args = ap.parse_args(argv)
    _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
