"""How many people are in a recording, when the file name does not say.

A recording dropped into inbox/ without a count in its name waits a short
while (config `speaker_count_wait_sec`) before it is processed, so that the
user can give the count. This module keeps the two facts that wait needs:
when the program first saw the recording, and the count the user gave.

Storage: one folder in the workspace, `.moim-scribe-headcount/`, holding up
to two small JSON files per recording:

  <key>.seen   {"path": ..., "first_seen": t, "written_at": t}
  <key>.count  {"path": ..., "speakers": n, "written_at": t}

<key> is a hash of the recording's path relative to inbox/, NFC-normalized
with "/" separators. A hash and not the name: a name of decomposed Hangul
can be three times its NFC length, and the file name limit on Linux is 255
bytes. NFC: phones and macOS hand over NFD names, a user types NFC, and both
must find the same record.

Why two files and no lock: each file has exactly one kind of writer. `.seen`
is written only by the inbox scan, which holds the pipeline lock; `.count`
only by the command that records a count. Nobody reads a file, changes it and
writes it back, so there is no update to lose, and every write is a whole-file
replace (temp file + os.replace), which is atomic on POSIX and on NTFS. That
avoids file locks, whose behaviour differs between macOS, Linux and Windows.

The recording itself is never renamed or moved by anything here.

Every failure to read or write a record degrades to 0.2.0 behaviour (process
now, estimate the count). A broken record must never keep a recording waiting.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path

from .config import Config

logger = logging.getLogger("moim_scribe.headcount")

HEADCOUNT_DIR_NAME = ".moim-scribe-headcount"
SEEN_SUFFIX = ".seen"
COUNT_SUFFIX = ".count"
TMP_SUFFIX = ".tmp"
# Same range a count in the file name may have (pipeline.FILENAME_COUNT_RANGE).
COUNT_RANGE = (1, 30)
# A temp file this old belongs to a writer that died mid-write.
STALE_TMP_SEC = 3600.0
# On Windows a replace fails while another process has the target open, and
# an open can fail while a replace is under way. Both last milliseconds.
_RETRY_ATTEMPTS = 20
_RETRY_PAUSE_SEC = 0.025


class HeadcountError(ValueError):
    """A count could not be recorded. The message says why, for the user."""


class NoSuchRecording(HeadcountError):
    pass


class AmbiguousRecording(HeadcountError):
    def __init__(self, message: str, candidates: list[Path]):
        super().__init__(message)
        self.candidates = candidates


@dataclass(frozen=True)
class WaitingRecording:
    """A recording in inbox/ that is waiting for its speaker count."""

    path: Path                 # where it is now
    name: str                  # path relative to inbox/, NFC, "/" separators
    first_seen: float | None   # None: the scan has not reached it yet
    seconds_left: float        # until it is processed with an estimated count


@dataclass(frozen=True)
class RecordedCount:
    """What record_count wrote."""

    path: Path
    name: str
    speakers: int
    # The file name already carries a count. It wins over this one, so the
    # recorded count will not be used; the caller should say so.
    filename_count: int | None


def headcount_dir(cfg: Config) -> Path:
    return cfg.workspace / HEADCOUNT_DIR_NAME


def recording_name(cfg: Config, recording: Path) -> str:
    """The recording's path relative to inbox/, NFC, with "/" separators."""
    try:
        rel = Path(recording).relative_to(cfg.inbox_dir)
    except ValueError:
        rel = Path(Path(recording).name)
    return "/".join(unicodedata.normalize("NFC", part) for part in rel.parts)


def record_key(name: str) -> str:
    return hashlib.sha256(unicodedata.normalize("NFC", name).encode("utf-8")).hexdigest()[:32]


# --- low-level file access -------------------------------------------------

def _read(path: Path) -> dict | None:
    """The record at path, or None when it is absent or unreadable."""
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            text = path.read_text(encoding="utf-8")
            break
        except PermissionError:
            if attempt == _RETRY_ATTEMPTS - 1:
                return None
            time.sleep(_RETRY_PAUSE_SEC)
        except (OSError, UnicodeDecodeError):
            return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _write(path: Path, data: dict) -> None:
    """Replace path with data in one step. Raises OSError on failure."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}{TMP_SUFFIX}")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        for attempt in range(_RETRY_ATTEMPTS):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == _RETRY_ATTEMPTS - 1:
                    raise
                time.sleep(_RETRY_PAUSE_SEC)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:  # Windows: open elsewhere. prune() retries later.
        logger.debug(f"could not delete {path.name}: {e}")


def _valid_count(value) -> int | None:
    lo, hi = COUNT_RANGE
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        return None
    return value


# --- used by the inbox scan ------------------------------------------------

def given_count(cfg: Config, name: str) -> int | None:
    """The count the user gave for this recording, or None."""
    rec = _read(headcount_dir(cfg) / f"{record_key(name)}{COUNT_SUFFIX}")
    return _valid_count(rec.get("speakers")) if rec else None


def first_seen(cfg: Config, name: str) -> float | None:
    """When the scan first saw this recording, or None (never, or unreadable)."""
    rec = _read(headcount_dir(cfg) / f"{record_key(name)}{SEEN_SUFFIX}")
    if not rec:
        return None
    t = rec.get("first_seen")
    if isinstance(t, bool) or not isinstance(t, (int, float)):
        return None
    return float(t)


def mark_seen(cfg: Config, name: str, now: float) -> tuple[float | None, bool]:
    """Return (first_seen, created_now) for this recording.

    Creates the record the first time. A record that exists but cannot be
    read is reported as (None, False): the caller treats that as "the wait is
    over" rather than restarting it, so a broken file cannot hold a recording
    back. Raises OSError when the record cannot be written.
    """
    path = headcount_dir(cfg) / f"{record_key(name)}{SEEN_SUFFIX}"
    if path.exists():
        return first_seen(cfg, name), False
    _write(path, {"path": name, "first_seen": now, "written_at": now})
    return now, True


def take(cfg: Config, name: str) -> dict:
    """Remove this recording's records and return what they held.

    Called once the recording has been claimed, so that a later recording of
    the same name starts with a clean slate. restore() puts them back when
    the claim is undone.
    """
    d = headcount_dir(cfg)
    key = record_key(name)
    held = {"first_seen": first_seen(cfg, name), "speakers": given_count(cfg, name)}
    _unlink(d / f"{key}{SEEN_SUFFIX}")
    _unlink(d / f"{key}{COUNT_SUFFIX}")
    return held


def restore(cfg: Config, name: str, held: dict, now: float) -> None:
    """Put back what take() removed. Best effort: a failure only costs a re-wait."""
    d = headcount_dir(cfg)
    key = record_key(name)
    try:
        if held.get("first_seen") is not None:
            _write(d / f"{key}{SEEN_SUFFIX}",
                   {"path": name, "first_seen": held["first_seen"], "written_at": now})
        if held.get("speakers") is not None:
            _write(d / f"{key}{COUNT_SUFFIX}",
                   {"path": name, "speakers": held["speakers"], "written_at": now})
    except OSError as e:
        logger.warning(f"could not keep the speaker count record of {name}: {e}")


def prune(cfg: Config, present: set[str], listed_at: float, now: float | None = None) -> None:
    """Delete records of recordings no longer in inbox/.

    `present` holds the record keys of every file in the scanned folders,
    listed at `listed_at`. A record written after that moment is kept even if
    its recording is not in the list: the file may have arrived after the
    listing, with its count given right away.
    """
    d = headcount_dir(cfg)
    try:
        entries = list(d.iterdir())
    except OSError:
        return
    now = time.time() if now is None else now
    for p in entries:
        if p.name.endswith(TMP_SUFFIX):
            try:
                if now - p.stat().st_mtime > STALE_TMP_SEC:
                    _unlink(p)
            except OSError:
                pass
            continue
        if not p.name.endswith((SEEN_SUFFIX, COUNT_SUFFIX)):
            continue
        key = p.name.rsplit(".", 1)[0]
        if key in present:
            continue
        rec = _read(p)
        written = rec.get("written_at") if rec else None
        if isinstance(written, (int, float)) and not isinstance(written, bool) and written >= listed_at:
            continue
        _unlink(p)


# --- used by commands ------------------------------------------------------

def inbox_recordings(cfg: Config) -> list[Path]:
    """Recordings the scan would look at, in the order it looks at them."""
    from .pipeline import _ENGINE_TEMP_NAME, classify_audio, inbox_folders

    found: list[Path] = []
    for folder in inbox_folders(cfg):
        try:
            entries = sorted(folder.iterdir())
        except OSError:
            continue
        for f in entries:
            if (f.is_file() and not f.name.startswith(".")
                    and not _ENGINE_TEMP_NAME.fullmatch(f.name) and classify_audio(f)):
                found.append(f)
    return found


def _filename_count(cfg: Config, recording: Path) -> int | None:
    from .pipeline import filename_speaker_hint

    return filename_speaker_hint(recording.stem, cfg.language)[0]


def waits_for_count(cfg: Config, recording: Path) -> bool:
    """Whether this recording is held back until a count is given or time runs out."""
    return (
        cfg.speaker_count_wait_sec > 0
        and not cfg.default_speakers
        and _filename_count(cfg, recording) is None
        and given_count(cfg, recording_name(cfg, recording)) is None
    )


def waiting_recordings(cfg: Config, now: float | None = None) -> list[WaitingRecording]:
    """Recordings in inbox/ that wait for a count, most recently seen last.

    One the scan has not reached yet (dropped in seconds ago) is listed too,
    with first_seen None and the full wait left: it will wait once reached.
    """
    now = time.time() if now is None else now
    wait = float(cfg.speaker_count_wait_sec)
    out: list[WaitingRecording] = []
    for f in inbox_recordings(cfg):
        if not waits_for_count(cfg, f):
            continue
        name = recording_name(cfg, f)
        seen = first_seen(cfg, name)
        if seen is None and (headcount_dir(cfg) / f"{record_key(name)}{SEEN_SUFFIX}").exists():
            continue  # unreadable record: the scan processes it without waiting
        left = wait if seen is None else max(0.0, wait - (now - seen))
        if seen is not None and seen > now:
            left = 0.0  # clock went back: the scan treats it as expired
        out.append(WaitingRecording(path=f, name=name, first_seen=seen, seconds_left=left))
    # Never-seen ones are the newest arrivals.
    out.sort(key=lambda w: (w.first_seen is None, w.first_seen or 0.0, w.name))
    return out


def latest_waiting(cfg: Config, now: float | None = None) -> WaitingRecording | None:
    """The waiting recording that arrived last ("the one I just put in")."""
    waiting = waiting_recordings(cfg, now)
    return waiting[-1] if waiting else None


def find_recording(cfg: Config, query: str) -> Path:
    """The one recording in inbox/ whose name contains `query`.

    Matching ignores case and Unicode normalization. An exact name (with or
    without extension) wins over partial matches. Raises NoSuchRecording or
    AmbiguousRecording.
    """
    q = unicodedata.normalize("NFC", query).strip().casefold()
    if not q:
        raise NoSuchRecording("no recording name given")
    recs = inbox_recordings(cfg)

    def norm(s: str) -> str:
        return unicodedata.normalize("NFC", s).casefold()

    exact = [f for f in recs if q in (norm(f.name), norm(f.stem), norm(recording_name(cfg, f)))]
    if len(exact) == 1:
        return exact[0]
    partial = exact or [f for f in recs if q in norm(recording_name(cfg, f))]
    if not partial:
        raise NoSuchRecording(f"no recording in {cfg.inbox_dir} matches {query!r}")
    if len(partial) > 1:
        raise AmbiguousRecording(
            f"{len(partial)} recordings match {query!r}: "
            + ", ".join(recording_name(cfg, f) for f in partial),
            partial,
        )
    return partial[0]


def record_count(cfg: Config, recording: Path, speakers: int,
                 now: float | None = None) -> RecordedCount:
    """Record that `recording` (a file in inbox/) has `speakers` people.

    The next scan processes it at once with that count. Raises HeadcountError
    for a count out of range or a file not in inbox/, OSError when the record
    cannot be written.
    """
    lo, hi = COUNT_RANGE
    if _valid_count(speakers) is None:
        raise HeadcountError(f"the speaker count must be a whole number from {lo} to {hi}, "
                             f"got {speakers!r}")
    recording = Path(recording)
    if not recording.is_file():
        raise NoSuchRecording(f"{recording} is not in {cfg.inbox_dir} (already processed?)")
    try:
        recording.relative_to(cfg.inbox_dir)
    except ValueError:
        raise NoSuchRecording(f"{recording} is not in {cfg.inbox_dir}") from None
    now = time.time() if now is None else now
    name = recording_name(cfg, recording)
    _write(headcount_dir(cfg) / f"{record_key(name)}{COUNT_SUFFIX}",
           {"path": name, "speakers": speakers, "written_at": now})
    return RecordedCount(path=recording, name=name, speakers=speakers,
                         filename_count=_filename_count(cfg, recording))


def clear_count(cfg: Config, recording: Path) -> bool:
    """Forget a count given for `recording`. True if there was one."""
    path = headcount_dir(cfg) / f"{record_key(recording_name(cfg, Path(recording)))}{COUNT_SUFFIX}"
    existed = path.exists()
    _unlink(path)
    return existed and not path.exists()
