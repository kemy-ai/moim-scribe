"""moim-scribe command line interface.

    moim-scribe init [workspace]        create a workspace skeleton (or repair one)
    moim-scribe watch [workspace]       watch the inbox and process recordings
    moim-scribe scan-inbox [workspace]  one inbox pass (for cron / Task Scheduler)
    moim-scribe process AUDIO           process a single file (the original stays put)
    moim-scribe enroll AUDIO JSONL SPEAKER_00=Name ...   enroll fingerprints
    moim-scribe speakers [workspace]    list enrolled speakers
    moim-scribe verify AUDIO TXT        verify labels in a finished transcript
    moim-scribe doctor [workspace]      check the local setup
"""
from __future__ import annotations

import argparse
import importlib
import importlib.resources
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__
from . import preflight as _preflight
from .config import (
    CONFIG_NAME,
    Config,
    ConfigError,
    default_stt_backend,
    ensure_workspace_dirs,
    load_config,
    normalize_folder_name,
)

logger = logging.getLogger("moim_scribe")

CONFIG_TEMPLATE = """\
# moim-scribe workspace configuration
# docs: https://github.com/kemy-ai/moim-scribe

# Transcription language (ko / en / any whisper language code)
language: {language}

# STT backend: "mlx" (Apple Silicon, install moim-scribe[mlx])
#              "faster" (Windows / Linux, install moim-scribe[cross])
#              "soniox" (cloud): UPLOADS THE AUDIO to Soniox, which returns it
#                 already split by speaker. Needs no Hugging Face token and no
#                 local model. Fast and accurate when people take turns; in
#                 testing on a noisy conversation where several people talked
#                 over each other it merged speakers together, which is what
#                 hybrid_diarization below undoes when a token is available.
#                 Requires an API key, see docs/setup-soniox.md.
stt_backend: {stt_backend}
# stt_model: null            # override the backend's default whisper model
# soniox_region: us          # us | eu | jp (eu and jp need activation by Soniox)
# hybrid_diarization: true   # soniox only: keep Soniox's text but take pyannote's
#                            # speaker boundaries. Skipped silently with no Hugging
#                            # Face token, so it is safe to leave on. This is also
#                            # the only path on which a filename speaker count is
#                            # used with soniox.

# Summarizer backend: claude_cli | anthropic_api | ollama | none
#   claude_cli    your existing Claude Code login, sends the transcript to Anthropic
#   anthropic_api needs ANTHROPIC_API_KEY, sends the transcript to Anthropic
#   ollama        local model, fully offline (needs Ollama running)
#   none          stop after transcription, nothing leaves this machine
# Every recording gets the same summary, whatever kind of recording it is.
# The template is packs/lenses/summary.<language>.md; edit it to taste.
summarizer: claude_cli
# summarizer_model: null     # e.g. sonnet / claude-sonnet-5 / llama3.1
# summarizer_timeout_sec: 900

# Speaker count when neither the filename ("... 5명" / "... 5p") nor a spoken
# cue at the start of the recording gives one. 0 = auto-estimate.
default_speakers: 0
"""


# Everything the workspace .gitignore must list. init writes these into a new
# file and appends whichever are missing from an existing one, so a workspace
# created by an older version gains each new entry the next time init runs.
# packs/ holds rosters and private notes (docs/privacy.md: never publish them),
# glossary.txt holds names of people and places. .process-*/ is the private
# work folder of `moim-scribe process`; a run that is killed leaves transcript
# work products in it.
GITIGNORE_ENTRIES = (
    "speakers/", "*.npy", "inbox/", "results/", "models/", "packs/", "glossary.txt",
    "*.wav", "*.m4a", "*.mp3", "*.flac", "*.ogg", "*.aac", "*.opus",
    ".hf_token", ".soniox_key",
    "_history/", ".process-*/",
)

# Loggers that report every HTTP request at INFO (one line per request, 22 on a
# first run that downloads models). Kept for --verbose.
NOISY_LOGGERS = ("httpx", "httpx2")


class WorkspaceMissing(Exception):
    """The workspace has no config.yaml. The message says what to run."""


def _cfg(ws: str | None) -> Config:
    """Load the workspace config, refusing a folder that was never initialized.

    Every command except init comes through here, before it creates anything:
    a mistyped path must not grow an empty workspace (and a default config
    that would send transcripts to a cloud summarizer).
    """
    path = Path(ws or ".").expanduser().resolve()
    if not (path / CONFIG_NAME).is_file():
        raise WorkspaceMissing(
            f'no {CONFIG_NAME} in {path}: run "moim-scribe init {path}" first'
        )
    return load_config(path)


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="[%(name)s] %(message)s",
        stream=sys.stderr,
    )
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.NOTSET if verbose else logging.WARNING)


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


# --- init ---------------------------------------------------------------------

def _repair_gitignore(ws: Path) -> None:
    """Write the workspace .gitignore, or append the entries it is missing."""
    gi = ws / ".gitignore"
    if not gi.exists():
        gi.write_text(
            "# moim-scribe workspace: private by default\n"
            + "".join(f"{n}\n" for n in GITIGNORE_ENTRIES),
            encoding="utf-8",
        )
        return
    # Leave the user's own lines alone; only add what is missing.
    existing = gi.read_text(encoding="utf-8")
    lines = {ln.strip() for ln in existing.splitlines()}
    missing = [n for n in GITIGNORE_ENTRIES if n not in lines]
    if missing:
        sep = "" if existing.endswith("\n") or not existing else "\n"
        gi.write_text(existing + sep + "".join(f"{n}\n" for n in missing), encoding="utf-8")
        print(f"  added to .gitignore: {', '.join(missing)}")


def _copy_shipped_files(packs_dir: Path, announce: bool, examples: bool) -> None:
    """Copy the shipped lens files that are not there yet, and the example packs.

    The lens files are needed by every recording, so a missing one is always
    restored. The example packs are only a demonstration: they are installed
    on the first init and never again, so a user who deleted one keeps it
    deleted.
    """
    templates = importlib.resources.files("moim_scribe") / "templates"
    dst_dir = packs_dir / "lenses"
    dst_dir.mkdir(parents=True, exist_ok=True)
    for item in templates.joinpath("lenses").iterdir():
        dst = dst_dir / item.name
        if not dst.exists():
            dst.write_text(item.read_text(encoding="utf-8"), encoding="utf-8")
            if announce:
                print(f"  added {dst.relative_to(packs_dir.parent)}")
    if not examples:
        return
    # An example pack is copied as a whole, only when its folder is absent: a
    # user who edited it keeps their version.
    for pack in templates.joinpath("examples").iterdir():
        dst_pack = packs_dir / pack.name
        if dst_pack.exists():
            continue
        dst_pack.mkdir(parents=True)
        for item in pack.iterdir():
            (dst_pack / item.name).write_text(item.read_text(encoding="utf-8"), encoding="utf-8")


def _free_backup_name(cfg_path: Path) -> Path:
    """config.yaml.bak, or the first of .bak.2, .bak.3, ... that does not exist.

    A backup is never overwritten: resetting twice must not lose the config
    the user had before the first reset.
    """
    bak = cfg_path.with_name(f"{cfg_path.name}.bak")
    n = 2
    while bak.exists() or bak.is_symlink():
        bak = cfg_path.with_name(f"{cfg_path.name}.bak.{n}")
        n += 1
    return bak


def cmd_init(args) -> int:
    """Create a workspace, or repair an existing one without touching config.yaml.

    config.yaml holds the user's decisions (which backend, which summarizer,
    so what leaves the machine). Re-running init must never reset them: only
    --reset-config writes a fresh one, after keeping the old one as .bak.
    """
    ws = Path(args.workspace or ".").expanduser().resolve()
    force = getattr(args, "force", False)
    reset = getattr(args, "reset_config", False)
    language_given = getattr(args, "language_given", True)
    if args.language is None:
        args.language = "ko"
    ws.mkdir(parents=True, exist_ok=True)
    cfg_path = ws / CONFIG_NAME
    existed = cfg_path.exists()

    if force and not reset:
        print("note: --force no longer resets config.yaml; use --reset-config for that")

    wrote = False
    if cfg_path.exists() and not reset:
        print(f"{CONFIG_NAME} already exists in {ws}: left untouched")
    else:
        if cfg_path.exists():
            bak = _free_backup_name(cfg_path)
            shutil.copy2(cfg_path, bak)
            print(f"old {CONFIG_NAME} saved as {bak}")
        stt_backend = default_stt_backend()
        cfg_path.write_text(
            CONFIG_TEMPLATE.format(language=args.language, stt_backend=stt_backend),
            encoding="utf-8",
        )
        wrote = True
        if existed and not language_given:
            # A reset without --language would otherwise quietly turn an
            # English workspace into a Korean one.
            print(f"note: the fresh {CONFIG_NAME} uses language {args.language} (the default); "
                  "pass --language to choose it, e.g. --reset-config --language en")
        print(f"wrote {cfg_path}: language {args.language}, stt_backend {stt_backend} "
              "(audio stays on this machine), summarizer claude_cli (sends the transcript "
              "to Anthropic)")
        print("  to change: rerun with --reset-config --language en, or edit config.yaml "
              "(stt_backend, summarizer: ollama or none keeps everything local)")

    # The skeleton and the safety net only need paths, so they are repaired
    # even when the existing config.yaml turns out to be unusable below.
    skeleton = Config(workspace=ws)
    ensure_workspace_dirs(skeleton)
    _copy_shipped_files(skeleton.packs_dir, announce=existed, examples=not existed)
    # A workspace-local safety net in case the user ever runs `git init` here.
    _repair_gitignore(ws)
    # Raises ConfigError for a config.yaml that cannot be used: the one the
    # user already had, or (should the template and the validator ever drift
    # apart) the one just written. main() prints it.
    load_config(ws)

    print(f"workspace ready: {ws}")
    if not wrote:
        return 0
    print("next steps:")
    print("  1. get a Hugging Face token + accept the pyannote model terms")
    print(f"     (docs/setup-pyannote.md) and save it to {ws / '.hf_token'}")
    print("     (or set stt_backend: soniox in config.yaml to transcribe in the")
    print("      cloud instead, which needs no token but uploads the audio)")
    print("  2. drop a recording into inbox/")
    print(f"  3. run: moim-scribe watch {ws}")
    return 0


# --- watch / scan-inbox / process -----------------------------------------------

def cmd_watch(args) -> int:
    cfg = _cfg(args.workspace)
    from .watch import watch

    ensure_workspace_dirs(cfg)
    watch(cfg, interval_sec=args.interval)
    return 0


def cmd_scan_inbox(args) -> int:
    from .pipeline import scan_inbox

    cfg = _cfg(args.workspace)
    ensure_workspace_dirs(cfg)
    counts = scan_inbox(cfg)
    problems = counts.get("setup_problems") or []
    summary = {k: counts[k] for k in ("processed", "failed", "skipped")}
    if problems:
        print(f"{summary}: stopped by {len(problems)} setup problem(s), see the messages "
              "above; every recording was left where it was")
    else:
        print(summary)
    # Non-zero so that cron / Task Scheduler / a wrapper script notices.
    # "another run is active" is not a failure: that run does the work.
    return 1 if problems or counts["failed"] else 0


def _inside(path: Path, folder: Path) -> bool:
    """True if `path` lies anywhere below `folder`, decided by file identity.

    Comparing path strings is not enough: on a case-insensitive or
    normalization-insensitive filesystem (macOS, Windows) `WS/INBOX/a.m4a`
    and a workspace path typed with different case or Unicode form name the
    same folder while the strings differ.
    """
    for parent in path.parents:
        try:
            if os.path.samefile(parent, folder):
                return True
        except OSError:
            continue
    return False


def cmd_process(args) -> int:
    """Process one file through a private copy; the original stays where it is.

    The copy lives in its own temporary folder inside the workspace (not under
    inbox/, so the watcher never sees it), and the engines write their work
    products next to the audio they are given, so they land there too. Nothing
    is shared with inbox/_processing, where a running watcher keeps the only
    copy of the recordings it has claimed, and the cleanup removes exactly
    that one folder.
    """
    from .pipeline import process_recording

    cfg = _cfg(args.workspace)
    src = Path(args.audio).expanduser().resolve()
    if not src.is_file():
        _err(f"not found: {src}")
        return 1
    if _inside(src, cfg.inbox_dir):
        _err(f"{src} is in the workspace inbox/: recordings there are handled by "
             f'"moim-scribe watch" / "moim-scribe scan-inbox". To process it by hand, '
             "move it out of inbox/ first")
        return 1
    problems = _preflight.preflight(cfg)
    if problems:
        for p in problems:
            _err(f"setup problem: {p}")
        return 1
    ensure_workspace_dirs(cfg)

    summarize = True
    if args.type:
        # Accepted so that pre-0.2 scripts keep running, and said out loud so
        # that nobody expects a type-specific result from it. The one thing a
        # type still decides is "no summary", for the same reason the folders
        # do (config.transcript_only_folders).
        if normalize_folder_name(args.type) in cfg.transcript_only_folders:
            summarize = False
            print(f"note: --type {args.type} is transcript-only in this workspace's "
                  "`types:` block, so no summary is written")
        else:
            print(f"note: --type {args.type} is ignored: every recording gets the same summary")

    work = Path(tempfile.mkdtemp(prefix=".process-", dir=cfg.workspace))
    try:
        claimed = work / src.name
        shutil.copy2(src, claimed)
        try:
            out = process_recording(cfg, claimed, summarize=summarize)
        except SystemExit as e:
            # get_hf_token and friends refuse with SystemExit. The preflight
            # above should have caught it; if not, report it the same way.
            _err(f"setup problem: {e}")
            return 1
        except Exception as e:  # noqa: BLE001 - a one-off command should end in one line, not a traceback
            # What was already written is kept: the transcript lands in results/
            # before the summary is attempted, so a summarizer failure here does
            # not cost the transcription.
            _err(f"failed: {type(e).__name__}: {e}")
            _err("your original file was not touched; if a transcript was produced it is in results/")
            return 1
        print(f"result: {out}")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --- speakers -------------------------------------------------------------------

def cmd_enroll(args) -> int:
    from .speakers.db import SpeakerDB
    from .speakers.enroll import enroll, parse_mapping

    cfg = _cfg(args.workspace)
    try:
        mapping = parse_mapping(args.mapping)
    except ValueError as e:
        print(e)
        return 1
    db = SpeakerDB(cfg.speakers_dir)
    result = enroll(args.audio, args.jsonl, mapping, db, cfg.embed_model_dir,
                    allow_duplicate=args.allow_duplicate, force=args.force)
    # Tagging is a separate write on purpose: enroll() merges embeddings and
    # knows nothing about groups, and a failed tag must not read as a failed
    # enrollment (the fingerprint is the part that is expensive to redo).
    if args.group or args.attends_all:
        index = db.load_index()
        tagged = []
        for name in sorted(set(result["enrolled"])):
            entry = index.get(name)
            if entry is None:
                continue
            if args.group:
                keep = [g for g in entry.get("groups", []) if g != args.group]
                entry["groups"] = [*keep, args.group]
            if args.attends_all:
                entry["attends_all"] = True
            tagged.append(name)
        if tagged:
            db.save_index(index)
            print(f"tagged {', '.join(tagged)}: {args.group or 'attends-all'}")
    # Non-zero when anything was refused, so a script cannot mistake a partial
    # enrollment for a complete one. The reasons are in the log above.
    return 1 if result["refused"] else 0


def cmd_speakers(args) -> int:
    from .speakers.db import SpeakerDB

    cfg = _cfg(args.workspace)
    index = SpeakerDB(cfg.speakers_dir).load_index()
    if not index:
        print("no speakers enrolled yet (moim-scribe enroll ...)")
        return 0
    for name, entry in sorted(index.items()):
        where = ""
        if entry.get("attends_all"):
            where = " [all groups]"
        elif entry.get("groups"):
            where = f" [{'/'.join(entry['groups'])}]"
        print(
            f"{name}{where}: {entry.get('total_sec', 0)}s across "
            f"{entry.get('n_samples', 0)} recordings "
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


# --- doctor ---------------------------------------------------------------------

LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", "0.0.0.0", "")


def data_flow(cfg: Config) -> list[str]:
    """Where the audio and the transcript go under this config, in plain words.

    Each line has to be literally true for every combination of STT backend
    and summarizer. With Soniox the transcript is made on Soniox's servers, so
    it does not "stay on this machine" even with summarizer: none.
    """
    cloud = cfg.stt_backend == "soniox"
    lines = []
    if cloud:
        lines.append(
            f"audio: UPLOADED to Soniox (stt_backend: soniox, region {cfg.soniox_region}); "
            "once a recording is transcribed (or fails), this tool asks Soniox to delete "
            "the upload and the transcription"
        )
        if cfg.glossary_path.exists():
            lines.append("glossary.txt: sent to Soniox with each recording, as context")
    else:
        lines.append(f"audio: stays on this machine (stt_backend: {cfg.stt_backend})")

    # Where the summary step sends the transcript; None = nowhere off this machine.
    if cfg.summarizer in ("claude_cli", "anthropic_api"):
        sent_to = f"Anthropic for the summary ({cfg.summarizer})"
        local_note = None
    elif cfg.summarizer == "ollama":
        from .summarize.ollama import DEFAULT_HOST

        raw = (os.environ.get("OLLAMA_HOST") or DEFAULT_HOST).strip()
        host = urlsplit(raw if "://" in raw else f"http://{raw}").hostname or ""
        if host in LOCAL_HOSTS:
            sent_to, local_note = None, "ollama"
        else:
            sent_to = f"the Ollama server at {raw} for the summary (OLLAMA_HOST)"
            local_note = None
    else:
        sent_to, local_note = None, "summarizer: none"

    if cloud:
        made = ("made by Soniox on its servers (deletion there requested as above), "
                "saved in results/")
        if sent_to:
            tail = f"then sent to {sent_to}"
        elif cfg.summarizer == "ollama":
            tail = "summarized on this machine (ollama)"
        else:
            tail = "no summary (summarizer: none)"
        lines.append(f"transcript: {made}; {tail}")
    elif sent_to:
        lines.append(f"transcript: made on this machine, then sent to {sent_to}")
    else:
        lines.append(f"transcript: stays on this machine ({local_note})")
    return lines


def _import_error(name: str) -> str | None:
    """None if `import name` works, else the exception class and its first line.

    Any Exception, not only ImportError: a broken torch or a binary mismatch
    raises OSError, RuntimeError or AttributeError at import time, and doctor
    is the one place that must report it instead of dying with a traceback.
    """
    try:
        importlib.import_module(name)
        return None
    except Exception as e:  # noqa: BLE001
        first = (str(e).splitlines() or [""])[0][:200]
        return f"{type(e).__name__}" + (f": {first}" if first else "")


def _online_hf_access(token: str | None, mark) -> None:
    """Ask the Hub whether this token may download each gated model."""
    from .secrets import GATED_HF_REPOS

    if not token:
        mark("i", "--online: no Hugging Face token, nothing to ask the Hub")
        return
    try:
        from huggingface_hub import auth_check
    except Exception as e:  # noqa: BLE001
        for repo in GATED_HF_REPOS:
            mark("?", f"could not check {repo}: {type(e).__name__}")
        return
    for repo in GATED_HF_REPOS:
        try:
            auth_check(repo, token=token)
        except Exception as e:  # noqa: BLE001
            if type(e).__name__ == "GatedRepoError":
                mark("FAIL", f"{repo}: this token has no access (terms not accepted yet, or the token itself is wrong). Open "
                     f'https://huggingface.co/{repo} and click "Agree and access repository"')
            else:
                mark("?", f"could not check {repo}: {type(e).__name__}")
        else:
            mark("OK", f"{repo}: this token has access")


def cmd_doctor(args) -> int:
    from .diarize import find_hf_token
    from .secrets import read_secret_file

    # The first line on purpose: importing torch and friends the first time can
    # take a minute of silence, which reads as a hang.
    print("checking the setup (the import check can take up to a minute on the first run)",
          flush=True)
    cfg = _cfg(args.workspace)
    ok = True

    def mark(kind: str, text: str) -> None:
        nonlocal ok
        if kind == "FAIL":
            ok = False
        print(f"[{kind:<4}] {text}", flush=True)

    def check(label: str, passed: bool, hint: str = "") -> None:
        mark("OK" if passed else "FAIL", label + (f": {hint}" if hint and not passed else ""))

    print(f"workspace: {cfg.workspace}")
    print(f"language: {cfg.language}")
    for line in data_flow(cfg):
        print(line)
    print()

    # The requirements come from the same list the pipeline checks before it
    # claims a recording, so doctor and the pipeline cannot disagree.
    reqs = _preflight.checks(cfg)
    for c in reqs:
        check(c.label, c.ok, c.fix)
    findable = {c.label: c.ok for c in reqs}

    cloud = cfg.stt_backend == "soniox"
    if cloud:
        check(
            f"soniox_region is one of us/eu/jp (got {cfg.soniox_region!r})",
            cfg.soniox_region in ("us", "eu", "jp"),
            "eu and jp also need activation by Soniox before they answer",
        )

    # Secret files are checked for content, not existence: an empty file, or
    # one a Windows shell wrote in an encoding the reader cannot use, counts as
    # no secret at all. An environment variable takes priority over the file,
    # so the file only matters when the variable is not set.
    hf_file = cfg.workspace / ".hf_token"
    if not os.environ.get("HF_TOKEN") and hf_file.exists():
        check(f"{hf_file} has content", read_secret_file(hf_file) is not None,
              "the file is empty or unreadable: paste the token (one line) into it")
    if cloud and not os.environ.get("SONIOX_API_KEY") and cfg.soniox_key_path.exists():
        check(f"{cfg.soniox_key_path} has content",
              read_secret_file(cfg.soniox_key_path) is not None,
              "the file is empty or unreadable: paste the key (one line) into it")
    hf_token = find_hf_token(cfg.workspace)
    if hf_token and not hf_token.startswith("hf_"):
        mark("WARN", "the Hugging Face token does not start with hf_: check that the whole "
             "token was copied")

    # pyannote is required to split speakers locally and optional on the cloud
    # path, where it only sharpens the boundaries Soniox has drawn. A token on
    # the cloud path means the user opted into that hybrid step, so a broken
    # pyannote is a real finding there: the step would fail on every recording
    # and say so only in the log. No token is the ordinary Soniox-only setup.
    want_pyannote = not cloud or (cfg.hybrid_diarization and hf_token is not None)
    if cloud and cfg.hybrid_diarization and hf_token is None:
        mark("i", "hybrid speaker boundaries idle: no Hugging Face token, so "
             "Soniox's own speaker labels are used")

    # Deeper than the preflight: actually import the heavy packages. speechbrain
    # is checked on both paths, since the fingerprint matching that turns
    # SPEAKER_00 into a name runs locally either way.
    heavy: list[str] = []
    if not cloud:
        heavy.append({"mlx": "mlx_whisper"}.get(cfg.stt_backend, "faster_whisper"))
    if want_pyannote:
        heavy.append("pyannote.audio")
    heavy.append("speechbrain")
    for name in heavy:
        if not _preflight.module_findable(name):
            if any(name in label and not passed for label, passed in findable.items()):
                mark("--", f"{name} importable: skipped, not installed (see above)")
            else:
                check(f"{name} importable", False, f"{name} is not installed: pip install moim-scribe")
            continue
        err = _import_error(name)
        check(f"{name} importable", err is None,
              f"import failed with {err}; reinstall it or run with -v for details")

    if cfg.summarizer == "ollama":
        mark("i", "summarizer=ollama: ensure Ollama is running and the model is pulled")
    elif cfg.summarizer == "none":
        mark("i", "summarizer=none: the pipeline stops at transcripts")

    if getattr(args, "online", False):
        print()
        _online_hf_access(hf_token, mark)

    print("\nall good" if ok else "\nsome checks failed: see the hints above")
    return 0 if ok else 1


# --- entry point ----------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="moim-scribe", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"moim-scribe {__version__}")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create a workspace skeleton, or repair an existing one")
    p.add_argument("workspace", nargs="?", default=".")
    p.add_argument("--language", default=None, help="ko / en (default: ko)")
    p.add_argument("--force", action="store_true",
                   help="no longer resets config.yaml (kept so old scripts run); "
                        "see --reset-config")
    p.add_argument("--reset-config", action="store_true",
                   help="write a fresh config.yaml, keeping the old one as config.yaml.bak "
                        "(or .bak.2, .bak.3, ... when that exists: no backup is overwritten)")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("watch", help="watch the inbox and process recordings")
    p.add_argument("workspace", nargs="?", default=".")
    p.add_argument("--interval", type=float, default=60.0, help="fallback scan interval seconds")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("scan-inbox", help="one inbox pass (cron friendly)")
    p.add_argument("workspace", nargs="?", default=".")
    p.set_defaults(func=cmd_scan_inbox)

    p = sub.add_parser("process", help="process one audio file (outside inbox/)")
    p.add_argument("audio")
    p.add_argument("--type", default=None, help=argparse.SUPPRESS)  # pre-0.2, ignored
    p.add_argument("--workspace", default=".")
    p.set_defaults(func=cmd_process)

    p = sub.add_parser("enroll", help="enroll speaker fingerprints from a diarized recording")
    p.add_argument("audio")
    p.add_argument("jsonl", help="the *_transcript_diarized.jsonl next to the recording")
    p.add_argument("mapping", nargs="+", help="SPEAKER_NN=Name ...")
    p.add_argument("--workspace", default=".")
    p.add_argument("--group", help="tag these speakers as belonging to a group "
                                   "(use the pack name, e.g. --group book-club)")
    p.add_argument("--attends-all", action="store_true",
                   help="this person is in every recording (you, usually): never "
                        "reported as being from another group")
    p.add_argument("--allow-duplicate", action="store_true",
                   help="allow one name on two labels of this recording (one person "
                        "split into two clusters). Refused by default: the usual cause "
                        "is a label number from a different run")
    p.add_argument("--force", action="store_true",
                   help="enroll even when the voice scores below 0.5 against the name's "
                        "existing fingerprint. 0.5 is not a safety line - the same person "
                        "can score lower (phone audio, thin fingerprint). Listen to the "
                        "label before forcing")
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
    p.add_argument("--online", action="store_true",
                   help="also ask the Hugging Face Hub whether the token may download "
                        "each gated model (network)")
    p.set_defaults(func=cmd_doctor)

    args = ap.parse_args(argv)
    if args.command == "init":
        args.language_given = args.language is not None
    _setup_logging(args.verbose)
    try:
        return args.func(args)
    except WorkspaceMissing as e:
        _err(str(e))
        return 1
    except ConfigError as e:
        _err(f"config error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
