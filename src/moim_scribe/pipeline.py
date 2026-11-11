"""Inbox processing pipeline: stabilize -> claim -> diarize -> summarize -> results.

Reliability rules inherited from the original production pipeline:
  - Files are claimed by an atomic move into inbox/_processing before any
    work starts, so a crashed run can never double-process.
  - Leftovers found in _processing at startup are quarantined to _error
    instead of retried (a file that crashed once must not loop forever).
  - Cloud-synced folders lie about file readiness: size is sampled twice
    and the file must actually decode (ffmpeg probe) before claiming.
  - Summarization failing never destroys work — the transcript is always
    preserved in results/ even when the summary step errors out.
"""
from __future__ import annotations

import errno
import importlib.resources
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import preflight as _preflight
from .config import AUDIO_EXTS, SUMMARY_LENS, Config, normalize_folder_name
from .diarize import PID as ENGINE_PID
from .diarize import SetupProblem, get_hf_token, run_diarization
from .packs import build_block, save_card
from .speaker_stats import render_speaker_stats_block
from .speakers.db import SpeakerDB
from .stt import get_backend
from .summarize import get_summarizer

logger = logging.getLogger("moim_scribe.pipeline")

MIN_FILE_BYTES = 10_000
STABILIZE_WAIT_SEC = 3.0
PREFETCH_CHUNK_BYTES = 1 << 20
PREFETCH_SLOW_SEC = 5.0  # above this, say so: the whole pass was held meanwhile
MIN_ON_DISK_RATIO = 0.95  # see on_disk_ratio(): complete files measure 1.00
PROBE_TIMEOUT_SEC = 60
RESULT_SUFFIXES = ("_transcript.txt", "_diarized.jsonl", "_summary.md")

PROMPT_HEADERS = {
    "ko": {
        "rec_title": (
            "=== 녹음 제목 (저장할 때 붙인 이름 — 사람 이름은 전사 인식보다 이쪽이 정본) ===\n"
            "{title}\n"
            "※ 산출물의 제목(H1)은 렌즈 지시대로 따로 만든다. 이 값을 그대로 제목으로 쓰라는 뜻이 아니다."
        ),
        "rec_info": "=== 녹음 정보 (메타데이터 실측) ===",
        "rec_line": "녹음 일시: {dt}  ※ 정리의 '일시'에 이 값을 쓸 것(전사에 별도 날짜 언급이 없으면)",
        "spk_stats": "=== 화자 통계 (diarize 실측 — 참석 인원·발화 비율은 이 데이터 기준) ===",
        "spk_fixed": "※ 파일명 지정 참석 인원: {n}명 (이 값이 확정값)",
        "transcript": "=== 전사본 ===",
    },
    "en": {
        "rec_title": (
            "=== Recording title (the name given at save time — for people's names, trust this over the transcript) ===\n"
            "{title}\n"
            "Note: build the deliverable's own H1 as the lens instructs; this is context, not the title to copy."
        ),
        "rec_info": "=== Recording info (from file metadata) ===",
        "rec_line": "Recorded at: {dt}  (use this for the summary's date unless the transcript says otherwise)",
        "spk_stats": "=== Speaker stats (measured — use these for attendee count and talk shares) ===",
        "spk_fixed": "Note: attendee count fixed by filename: {n} (authoritative)",
        "transcript": "=== Transcript ===",
    },
}

_HEADING_MARKER = re.compile(r"#{1,6}\s")
# A count is one or two digits that do not continue a longer number, so
# "2025 ..." and "1103 ..." never read as a count of 25 or 3.
#   명 counts people in any position ("5명", "5명 회의"), but not as the first
#     syllable of a word ("2025 명절").
#   인 is also a prefix that builds nouns ("3인칭", "1인 가구", "1인 기업"), so
#     it counts only where no Hangul word follows it, after optional spaces:
#     "회의 3인" and "전략회의 3인 final" do, "1인 가구 인터뷰" does not.
_KO_FILENAME_COUNT = re.compile(
    r"(?<![0-9])([0-9]{1,2})\s*(?:명(?![가-힣])|인(?!\s*[가-힣]))"
)
_EN_FILENAME_COUNT = re.compile(
    r"(?<![0-9])\b([0-9]{1,2})\s*(?:p|ppl|people|speakers?)\b", re.IGNORECASE
)
FILENAME_COUNT_RANGE = (1, 30)


def filename_speaker_hint(title: str, language: str) -> tuple[int | None, str]:
    """Extract an attendee-count hint from the filename and strip it.

    Naming the count in the filename ("book club 5명", "standup 4p") is the
    most reliable source — set at save time, never misheard. Filenames from
    phones are frequently NFD, so normalize first.

    The count is forced on the speaker separation, so a false hit is worse
    than none: "1103 인터뷰" used to become 1103 speakers and the title "터뷰".
    Only a count of 1 to 30 is taken, and when nothing qualifies the title is
    returned untouched.
    """
    t = unicodedata.normalize("NFC", title)
    pattern = _KO_FILENAME_COUNT if language == "ko" else _EN_FILENAME_COUNT
    lo, hi = FILENAME_COUNT_RANGE
    for m in pattern.finditer(t):
        count = int(m.group(1))
        if not lo <= count <= hi:
            continue
        cleaned = re.sub(r"\s+", " ", t[: m.start()] + " " + t[m.end():]).strip()
        return count, (cleaned or "untitled")
    return None, t


def _headers(language: str) -> dict:
    return PROMPT_HEADERS.get(language, PROMPT_HEADERS["en"])


def _recording_time_block(audio_path: Path, language: str) -> str:
    """Read the container creation_time and render it in local time.

    Lets the summary carry an accurate date even when nobody says the date
    out loud in the recording."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format_tags=creation_time",
             "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path)],
            capture_output=True, text=True, timeout=60, check=False,
        )
        raw = r.stdout.strip().splitlines()[0].strip() if r.stdout.strip() else ""
        if not raw:
            return ""
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        local = dt.astimezone()
        h = _headers(language)
        return f"{h['rec_info']}\n{h['rec_line'].format(dt=local.strftime('%Y-%m-%d (%a) %H:%M'))}\n"
    except Exception as e:  # noqa: BLE001 — metadata is a bonus, never fatal
        logger.debug(f"creation_time unavailable: {e}")
        return ""


def _shipped_lens(lens: str, language: str) -> str | None:
    """The copy of a lens that ships inside the package, or None.

    Language order: the workspace language, then English. English is the
    fallback because a workspace can be set to any whisper language code while
    only two translations of the lens exist.
    """
    shipped = importlib.resources.files("moim_scribe") / "templates" / "lenses"
    for candidate in (f"{lens}.{language}.md", f"{lens}.en.md"):
        item = shipped / candidate
        if item.is_file():
            return item.read_text(encoding="utf-8")
    return None


def _load_lens(cfg: Config, lens: str) -> str:
    """Load a lens: the workspace copy first, the shipped copy otherwise.

    Workspace order is <lens>.<lang>.md, then <lens>.md, so a user's edited
    lens always wins. Falling back to the package matters in two cases that
    would otherwise fail the recording outright: a workspace created before
    this lens existed (it has the old lens files and not this one), and a
    workspace whose language has no translation of the lens.
    """
    lenses_dir = cfg.packs_dir / "lenses"
    for candidate in (f"{lens}.{cfg.language}.md", f"{lens}.md"):
        p = lenses_dir / candidate
        if p.is_file():
            return p.read_text(encoding="utf-8")
    shipped = _shipped_lens(lens, cfg.language)
    if shipped is not None:
        logger.info(
            f"no {lens} lens in {lenses_dir}; using the built-in one "
            f"(copy it there to customize)"
        )
        return shipped
    raise FileNotFoundError(f"lens not found: {lenses_dir}/{lens}[.{cfg.language}].md")


def _load_common_lens(cfg: Config) -> str:
    lenses_dir = cfg.packs_dir / "lenses"
    for candidate in (f"_common.{cfg.language}.md", "_common.md"):
        p = lenses_dir / candidate
        if p.is_file():
            return p.read_text(encoding="utf-8")
    # Same fallback as _load_lens, and for a sharper reason: these are the
    # rules that forbid guessing who a speaker is. A workspace missing the
    # file used to get a prompt without them, with nothing to show for it.
    return _shipped_lens("_common", cfg.language) or ""


def _strip_preamble(summary: str) -> str:
    """Drop anything before the first markdown heading.

    Lenses mandate deliverables that begin with an H1, so leading lines are
    CLI noise — wrapper banners, "Here is the summary:" chatter. If there is
    no heading at all, the output is returned untouched (never destroy a
    summary over formatting).
    """
    lines = summary.splitlines()
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            return "\n".join(lines[i:])
        # Noise can also bleed into the heading's own line instead of sitting on
        # its own ("<junk># Minutes — topic"). Skipping such a line loses the
        # title outright: the next heading found is an H2, so the deliverable
        # arrives with no H1 at all. Cutting at the marker recovers it.
        # First line only, and only where a real heading marker follows (a
        # "#" with whitespace after it), so prose like "see #1" is left alone.
        if i == 0:
            m = _HEADING_MARKER.search(line)
            if m and m.start() > 0:
                return "\n".join([line[m.start():]] + lines[1:])
    return summary


def classify_audio(path: Path) -> bool:
    """Decide whether a file in an inbox folder is audio we should process.

    Filtering by extension alone has a failure mode worse than skipping: a file
    whose extension got stripped never enters the candidate list, so there is no
    SKIP line, no error, nothing — the pipeline does not know it exists.
    Upstream that left a recording sitting unprocessed for hours. So the scan
    walks everything in the folder and this gate decides:

      - a known extension passes with no probe at all (normal runs cost nothing)
      - empty files are out
      - anything else is probed for an audio stream, so notes, images and our
        own .txt/.json outputs fall out quietly
      - a cloud placeholder fails the probe and gets picked up on a later pass,
        which is a delay, not the permanent silence above
    """
    if path.suffix.lower() in AUDIO_EXTS:
        return True
    try:
        if path.stat().st_size == 0:
            return False
    except OSError:
        return False
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=60, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if probe.returncode == 0 and probe.stdout.strip() == "audio":
        # This log line is the reason the function exists: the user has to learn
        # that an extension went missing, or it happens again.
        logger.info(f"audio with no usable extension — processing anyway: {path.name}")
        return True
    return False


def _prefetch(path: Path) -> int | None:
    """Make a cloud-sync provider materialize an online-only placeholder.

    Returns the number of bytes read, or None when the read failed.

    Google Drive, iCloud and Dropbox keep "online-only" files as stubs whose
    metadata already looks complete. Reading the bytes is what triggers the
    actual download. Without that, the stub's size is stable on every pass and
    the decode probe below keeps failing — so the file is skipped forever rather
    than fetched, and nothing in the log says why.

    🔴 This blocks until the download finishes, and scan_inbox holds
    PipelineLock for the entire pass, so a large placeholder stalls the whole
    pipeline for that long. Accepted because the alternative is never processing
    the recording at all — but it is why a slow prefetch is logged rather than
    passing silently.

    Reads in chunks and discards them, so memory stays flat regardless of size.
    """
    t0 = time.time()
    n_read = 0
    try:
        with path.open("rb") as fh:
            while chunk := fh.read(PREFETCH_CHUNK_BYTES):
                n_read += len(chunk)
    except OSError as e:
        # Not fatal: the checks below decide. A stub that cannot be read yet just
        # gets skipped this pass, exactly as before.
        logger.info(f"prefetch skipped ({type(e).__name__}) — continuing: {path.name}")
        return None
    elapsed = time.time() - t0
    if elapsed > PREFETCH_SLOW_SEC:
        logger.info(
            f"prefetch of {path.name} took {elapsed:.0f}s (cloud download) — "
            "the pipeline was held for that time"
        )
    return n_read


def on_disk_ratio(path: Path) -> float | None:
    """Share of the file's logical size that is physically on disk, or None
    when this platform cannot say.

    🔴 This is the only check that sees a partial cloud download. A file that a
    sync client is still fetching reports its *final* size from the start, the
    first 0.1s decodes fine, and reading it can stop short without an error,
    so the size check, the decode probe and the prefetch all pass. Upstream, a
    recording that was 64.5% downloaded went through exactly that way and was
    transcribed as if it were whole: 24 minutes of a 37-minute meeting, in a
    deliverable that looked finished. Allocated blocks are the one number that
    was different (64.5% there; 100.0% on every one of 25 complete files).

    macOS only. That is where the placeholder mechanism measured above lives,
    and on other systems the same arithmetic misfires: a compressing or
    deduplicating filesystem legitimately stores fewer blocks than the logical
    size, which would turn this guard into a permanent skip. Windows has no
    st_blocks at all.
    """
    if sys.platform != "darwin":
        return None
    try:
        st = path.stat()
    except OSError:
        return None
    blocks = getattr(st, "st_blocks", None)
    if blocks is None or st.st_size <= 0:
        return None
    return blocks * 512 / st.st_size


def is_stable_audio(path: Path) -> bool:
    """True when the file is fully present and decodable.

    Cloud placeholders report a final size while content is still syncing,
    so a metadata check is not enough: the file must actually decode, and
    (where the platform can tell) actually be on disk.
    """
    n_read = _prefetch(path)
    try:
        s1 = path.stat().st_size
    except OSError:
        return False
    time.sleep(STABILIZE_WAIT_SEC)
    try:
        s2 = path.stat().st_size
    except OSError:
        return False
    if s1 != s2:
        logger.info(f"SKIP still being written (size {s1} -> {s2} bytes): {path.name}")
        return False
    if s1 < MIN_FILE_BYTES:
        # Stable and tiny: a cloud placeholder reports its final size from the
        # start, so this is not a download in progress. It is skipped on every
        # pass, so say plainly what it is and what to do.
        logger.warning(
            f"SKIP {path.name}: {s1} bytes is too small to be a recording "
            f"(minimum {MIN_FILE_BYTES}): remove it from inbox/"
        )
        return False
    ratio = on_disk_ratio(path)
    if ratio is not None and ratio < MIN_ON_DISK_RATIO:
        # Few blocks has two readings. A download in progress is one; the other
        # is a complete file that simply occupies less than its size (a
        # compressed file measured 0.008 here, and some network volumes report
        # no blocks at all). Skipping the second kind would skip it on every
        # pass, forever. What tells them apart is whether the whole file could
        # be read just now: in the partial-download incident the read failed.
        if n_read is not None and n_read >= s2:
            logger.info(
                f"{ratio:.0%} of {path.name} is on disk by block count, but the whole "
                "file read back: treating it as complete (compressed file or network volume)"
            )
        else:
            logger.warning(
                f"SKIP only {ratio:.0%} of the file is on disk and it could not be "
                f"read in full: {path.name}: still downloading, or not readable: "
                "if it is not downloading, remove it from inbox/"
            )
            return False
    try:
        probe = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(path), "-t", "0.1", "-f", "null", "-"],
            capture_output=True, timeout=PROBE_TIMEOUT_SEC, check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        # A stalled cloud read can hold ffmpeg past the timeout. Escaping from
        # here would end the pass (and `watch`): call it not ready instead.
        logger.warning(
            f"SKIP {path.name}: the decode check did not finish ({type(e).__name__}): "
            "still downloading, or the drive is slow: it is tried again next pass"
        )
        return False
    if probe.returncode != 0:
        # No state is kept between passes, so this cannot tell a download that
        # will finish from a file that will never decode. Name both, at a level
        # that shows: a file that is not audio is skipped on every pass.
        logger.warning(
            f"SKIP cannot decode {path.name}: still downloading, or not a valid "
            "audio file: if it is not downloading, remove it from inbox/"
        )
        return False
    return True


ARCHIVE_SIDECARS = ("_transcript_diarized.txt", "_speakers.json")

# Work products the engine writes next to the claimed file, by suffix.
WORK_SUFFIXES = (
    "_transcript_diarized.jsonl",
    "_transcript_diarized.txt",
    "_speakers.json",
)
# The longest name the pipeline may build from a recording's stem: a work
# product, or an archived sidecar after archive_stem() added " (n)". Result
# names in results/ ("YYMMDD_" + title + " (n)" + "_diarized.jsonl") are
# shorter, and the title is never longer than the stem.
LONGEST_STEM_SUFFIX = " (99)_transcript_diarized.jsonl"
# name_fits() creates "<stem><NAME_PROBE_SUFFIX>" for a moment. The suffix is at
# least as long as LONGEST_STEM_SUFFIX (both ASCII, so in bytes and in UTF-16
# units alike) and ends in an extension nothing else uses, so a probe orphaned
# by a crash between create and unlink is recognised and removed later.
NAME_PROBE_EXT = ".moim-probe"
NAME_PROBE_SUFFIX = f" (99)_name_fits_probe{NAME_PROBE_EXT}"

# Temporary files the engine (diarize.py) creates in the claimed file's folder,
# named after the engine process id. The full-length wav is as large as the
# recording decoded, and it is removed only when the engine finishes.
_ENGINE_TEMP_NAME = re.compile(r"_(?:diarize_full|diarize_head|concat_list)_(\d+)\.(?:wav|txt)")


def _exists(path: Path) -> bool:
    """Path.exists() that answers False instead of raising.

    A name the filesystem cannot hold (ENAMETOOLONG) raises on Python 3.11
    rather than returning False, and such a file cannot exist anyway.
    """
    try:
        return path.exists()
    except OSError:
        return False


def _safe_unlink(path: Path) -> None:
    """Delete a file if it is there; log and carry on when that fails.

    Cleanup must never be what stops a pass: an unlink that raised on an
    over-long name used to escape scan_inbox on every pass, forever.
    """
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        logger.warning(f"could not remove {path.name}: {type(e).__name__}: {e}")


def _safe_move(src: Path, dest: Path) -> bool:
    """Move a file; log and return False instead of raising when it fails."""
    try:
        shutil.move(str(src), dest)
        return True
    except OSError as e:
        logger.error(f"could not move {src.name} to {dest.parent}: {type(e).__name__}: {e}")
        return False


def remove_engine_temp_files(folder: Path, pid: int) -> None:
    """Remove the temporary files an engine run with this pid left in folder.

    diarize.py deletes its full-length wav only on success, so a failed run
    leaves a copy of the whole recording, decoded, in _processing.
    """
    for name in (f"_diarize_full_{pid}.wav", f"_diarize_head_{pid}.wav", f"_concat_list_{pid}.txt"):
        _safe_unlink(folder / name)


def name_fits(folder: Path, stem: str) -> bool:
    """Whether every name the pipeline builds from this stem can exist in folder.

    Decided by creating the longest such name, because the limit is the
    filesystem's and is not a byte count everywhere: APFS allows 255 UTF-16
    units (255 NFC Hangul syllables, 765 bytes, are fine; an NFD syllable takes
    2 or 3 of them), while ext4 allows 255 bytes. Only "name too long" answers
    no; any other error lets the run go ahead, since its cleanup is now safe.
    """
    probe = folder / f"{stem}{NAME_PROBE_SUFFIX}"
    try:
        fd = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return True
    except OSError as e:
        # ERROR_FILENAME_EXCED_RANGE (206) is how Windows says the same thing.
        return not (e.errno == errno.ENAMETOOLONG or getattr(e, "winerror", None) == 206)
    os.close(fd)
    _safe_unlink(probe)
    return True


def archive_stem(dest_dir: Path, stem: str, suffix: str) -> str:
    """A stem under which nothing of this recording's set exists in dest_dir yet.

    _done and _error keep the original recording. A recurring group tends to
    save every session under the same name ("book club 5p.m4a" every week),
    and a plain move onto an existing name replaces the older original without
    a word, along with its transcript. So a taken name gets " (2)", " (3)", ...
    and the recording and its sidecar files move under the same new stem, so
    they still pair up.
    """
    def taken(s: str) -> bool:
        return _exists(dest_dir / f"{s}{suffix}") or any(
            _exists(dest_dir / f"{s}{side}") for side in ARCHIVE_SIDECARS
        )

    candidate, n = stem, 2
    while taken(candidate):
        candidate = f"{stem} ({n})"
        n += 1
    return candidate


def _is_stale_engine_temp(name: str) -> bool | None:
    """None when name is not an engine temp file; else whether its run is over."""
    m = _ENGINE_TEMP_NAME.fullmatch(name)
    if not m:
        return None
    pid = int(m.group(1))
    # Our own pid cannot be mid-run here (quarantine runs before this pass
    # starts any engine), and a container restart reuses pids.
    return pid == os.getpid() or not PipelineLock._pid_alive(pid)


def _is_name_probe(path: Path) -> bool:
    """An empty file name_fits() created and a crash kept it from deleting.

    Empty as well as named so: a recording is never 0 bytes, so even one a
    user happened to give this extension is never taken for a probe.
    """
    if not path.name.endswith(NAME_PROBE_EXT):
        return False
    try:
        return path.is_file() and path.stat().st_size == 0
    except OSError:
        return False


def quarantine_leftovers(cfg: Config) -> None:
    """Move files stranded in _processing (a previous crash) to _error.

    Never auto-retry them: the same input crashed once already, and an
    automatic retry loop would burn the machine on a poison file.

    The engine's own temp files are not recordings and never go to _error: a
    leftover from a run that is over is deleted, one whose process is still
    alive is left alone. Every step is OSError-safe, so one file that cannot
    be moved or deleted is logged and the rest are still handled.
    """
    if not cfg.processing_dir.exists():
        return
    try:
        entries = sorted(cfg.processing_dir.iterdir())
    except OSError as e:
        logger.error(f"cannot list {cfg.processing_dir}: {type(e).__name__}: {e}")
        return
    for f in entries:
        if _is_name_probe(f):
            _safe_unlink(f)
            logger.info(f"removed a name check file left by an earlier run: {f.name}")
            continue
        stale_temp = _is_stale_engine_temp(f.name)
        if stale_temp is not None:
            if stale_temp:
                _safe_unlink(f)
                logger.info(f"removed a temp file left by an earlier run: {f.name}")
            continue
        # Same gate as the inbox scan: a claimed file whose extension was
        # stripped would otherwise be invisible here too, and stay in
        # _processing forever with nothing logged.
        if not f.is_file() or not classify_audio(f):
            continue
        stem = f.stem
        for suffix in WORK_SUFFIXES:
            _safe_unlink(cfg.processing_dir / f"{stem}{suffix}")
        dest = archive_stem(cfg.error_dir, stem, f.suffix)
        if _safe_move(f, cfg.error_dir / f"{dest}{f.suffix}"):
            logger.warning(
                f"stranded file quarantined to _error (a previous run stopped while "
                f"processing it): {f.name}: to retry, move it back to inbox/"
            )


def free_result_base(results_dir: Path, base: str) -> str:
    """A result name stem under which nothing exists in results/ yet.

    Two recordings with the same title on the same day ("standup" from two
    teams, or a second take) would otherwise write the same three files, and
    the one lost for good is the jsonl: it is the only thing `enroll` can read.
    """
    def taken(b: str) -> bool:
        return any((results_dir / f"{b}{suffix}").exists() for suffix in RESULT_SUFFIXES)

    candidate, n = base, 2
    while taken(candidate):
        candidate = f"{base} ({n})"
        n += 1
    return candidate


def claim_result_base(results_dir: Path, base: str) -> str:
    """Like free_result_base, but the name is taken atomically before returning.

    Checking first and writing later lets a `process` run and the watcher,
    finishing the same title on the same day, pick the same name, and one
    copy then overwrites the other's jsonl. The transcript file is created
    here with O_EXCL, so of two runs only one can get a given base; the other
    gets FileExistsError and moves on to the next number. The caller writes
    the transcript through this (empty) file and removes it if that fails.
    """
    candidate, n = base, 2
    while True:
        if not any(_exists(results_dir / f"{candidate}{sfx}") for sfx in RESULT_SUFFIXES[1:]):
            try:
                fd = os.open(results_dir / f"{candidate}{RESULT_SUFFIXES[0]}",
                             os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
            except FileExistsError:
                pass
            else:
                os.close(fd)
                return candidate
        candidate = f"{base} ({n})"
        n += 1


@dataclass
class RunProgress:
    """What one process_recording call has put in results/, for its caller.

    scan_inbox decides from this whether a recording that stopped can go back
    to inbox/: once its transcript is in results/, putting it back would have
    every later pass transcribe it again (a new paid upload on Soniox) and add
    " (2)", " (3)", ... results.
    """

    # Every file this run created in results/, a partly written one included.
    created: list[Path] = field(default_factory=list)
    # True once the transcript is complete in results/.
    results_written: bool = False


def process_recording(
    cfg: Config, claimed: Path, *, summarize: bool = True, progress: RunProgress | None = None
) -> Path:
    """Run one claimed file end-to-end. Returns the primary result path.

    Every recording takes the same route: diarize, name the speakers from the
    fingerprint database, summarize with the one summary lens. What kind of
    recording it is (a meeting, a call, a book club) is not asked.

    Raises on failure; the caller decides quarantine/cleanup. When `progress`
    is given, it is kept up to date with what has been written to results/.
    """
    progress = progress if progress is not None else RunProgress()
    hint, title = filename_speaker_hint(claimed.stem, cfg.language)
    # The user's local date, timezone-explicit — result filenames must match
    # the day the user experienced, not the UTC day.
    date_tag = datetime.now(UTC).astimezone().strftime("%y%m%d")
    logger.info(
        f"processing: {claimed.name} (title={title}"
        + (f", speakers={hint} from filename)" if hint else ")")
    )

    db = SpeakerDB(cfg.speakers_dir)

    if cfg.stt_backend == "soniox":
        # 🔴 Everything local-only stays inside this else-branch, get_hf_token
        # above all. It raises SystemExit, which is a BaseException and so slips
        # past the `except Exception` in scan_inbox — called unconditionally, a
        # Soniox user with no Hugging Face token would not get one failed
        # recording but a dead watch loop.
        #
        # The hybrid step needs that same token, and this is why it is handed a
        # workspace to look in rather than a token: the lookup happens inside
        # find_hf_token, which returns None instead of exiting. A missing token
        # can then mean "skip the improvement" instead of "kill the loop".
        from .diarize_cloud import run_cloud_diarization
        from .stt.soniox_client import get_api_key

        result = run_cloud_diarization(
            str(claimed),
            api_key=get_api_key(cfg.workspace),
            speaker_db=db,
            model_dir=cfg.embed_model_dir,
            region=cfg.soniox_region,
            glossary_path=cfg.glossary_path,
            language_hints=[cfg.language],
            num_speakers=hint,
            workspace=cfg.workspace,
            hybrid=cfg.hybrid_diarization,
        )
    else:
        stt = get_backend(cfg.stt_backend, cfg.stt_model)
        result = run_diarization(
            str(claimed),
            stt_backend=stt,
            language=cfg.language,
            speaker_db=db,
            model_dir=cfg.embed_model_dir,
            hf_token=get_hf_token(cfg.workspace),
            glossary_path=cfg.glossary_path,
            num_speakers=hint,
            default_speakers=(cfg.default_speakers or None),
        )
    transcript_path = Path(result["txt_path"])
    if not transcript_path.exists() or transcript_path.stat().st_size == 0:
        raise RuntimeError("empty transcript")

    cfg.results_dir.mkdir(parents=True, exist_ok=True)
    base = claim_result_base(cfg.results_dir, f"{date_tag}_{title}")
    if base != f"{date_tag}_{title}":
        logger.info(f"results/ already has {date_tag}_{title}; writing this one as {base}")
    out_txt = cfg.results_dir / f"{base}_transcript.txt"
    progress.created.append(out_txt)
    try:
        shutil.copy2(transcript_path, out_txt)
    except BaseException:
        # The claimed name must not stay behind as an empty file.
        _safe_unlink(out_txt)
        raise
    progress.results_written = True
    # Keep the segment records too: they are the raw material for
    # `moim-scribe enroll` (mapping SPEAKER_NN ranges to names later).
    jsonl_src = Path(result["jsonl_path"])
    if jsonl_src.exists():
        out_jsonl = cfg.results_dir / f"{base}_diarized.jsonl"
        progress.created.append(out_jsonl)
        shutil.copy2(jsonl_src, out_jsonl)

    stats_path = Path(result["stats_path"])

    # A fingerprint is compared against everyone enrolled, so as the database
    # grows a name from a different recurring group can win. Say so; do not act
    # on it — see speakers/groups.py for why this is a display and not a gate.
    # The group of this recording is just the pack that matched, so there is
    # nothing extra to configure, and when no pack matched the check is skipped
    # rather than guessed at. It runs before the summarizer decision on
    # purpose: a transcript-only setup names speakers too.
    if stats_path.exists():
        from .packs import find_matching_pack
        from .speakers.groups import check_stats_file

        _pack = find_matching_pack(cfg.packs_dir, title)
        _note = check_stats_file(stats_path, _pack.name if _pack else None, db.load_index())
        if _note:
            logger.warning(_note)

    if not summarize:
        logger.info(f"transcript-only folder (pre-0.2 `types:` block): {out_txt}")
        return out_txt

    # Decided before anything summary-related is loaded, so `summarizer: none`
    # works in a workspace that has no lens files at all.
    summarizer = get_summarizer(cfg.summarizer, cfg.summarizer_model, cfg.summarizer_timeout_sec)
    if summarizer is None:
        logger.info(f"summarizer=none, transcript only: {out_txt}")
        return out_txt

    h = _headers(cfg.language)
    rec_info = _recording_time_block(claimed, cfg.language)
    # Filtered, never the raw file: some fields in speakers.json are diagnostics
    # that can name people who were not in the room, and the header right above
    # this block tells the model the attendee list comes from here.
    rendered_stats = render_speaker_stats_block(stats_path) if stats_path.exists() else ""
    spk_stats = ""
    if rendered_stats:
        spk_stats = f"{h['spk_stats']}\n{rendered_stats}\n"
        if hint:
            spk_stats += h["spk_fixed"].format(n=hint) + "\n"
    else:
        # Don't let the omission be silent. Without this block the summary falls
        # back to inferring the attendee count from the conversation — the one
        # thing the lens forbids — and the finished output looks entirely normal.
        logger.warning(
            "speaker stats block omitted — the summary will have no measured attendee count"
        )
    ctx_block = build_block(cfg.packs_dir, title, cfg.language)

    # The filename is the one name source a human set deliberately, at save
    # time, so for people's names it beats transcription: a self-introduction
    # spoken seconds after someone else's gets pulled toward the wrong surname.
    # Until this block existed the title was computed for pack matching and
    # then thrown away, so the deliverable had no way to know it.
    title_block = h["rec_title"].format(title=title) + "\n"
    prompt = (
        f"{_load_common_lens(cfg)}\n{_load_lens(cfg, SUMMARY_LENS)}\n\n"
        f"{title_block}{rec_info}{spk_stats}{ctx_block}\n"
        f"{h['transcript']}\n{transcript_path.read_text(encoding='utf-8')}"
    )

    out_md = cfg.results_dir / f"{base}_summary.md"
    progress.created.append(out_md)
    try:
        summary = summarizer.summarize(prompt)
    except Exception as e:
        # The transcript is already safe in results/ — surface the summary
        # failure without losing the expensive diarization work.
        raise RuntimeError(f"summarization failed (transcript kept at {out_txt}): {e}") from e
    out_md.write_text(_strip_preamble(summary).strip() + "\n", encoding="utf-8")
    logger.info(f"summary: {out_md}")
    save_card(cfg.packs_dir, title, out_md)
    return out_md


class PipelineLock:
    """Cross-platform single-instance lock (directory + pid file).

    Stale detection: if the recorded pid is gone, or is this process's own
    pid (a container restarts with the same pid every boot, so a lock left by
    the previous boot names a pid that is alive: us), or the pid file is
    unreadable and the lock is over 24h old, the lock is broken and reclaimed.

    acquire() returning False means another run holds the lock, unless
    `error` is set: then the lock could not be created at all (a read-only
    workspace, say), which is a different problem with a different fix.
    """

    def __init__(self, workspace: Path):
        self.lock_dir = workspace / ".moim-scribe.lock"
        self.error: OSError | None = None

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if sys.platform == "win32":
            # os.kill(pid, 0) is NOT a liveness probe on Windows: signal 0 is
            # CTRL_C_EVENT, which gets broadcast to our own console group and
            # KeyboardInterrupts the caller (found the hard way in CI). Ask
            # the kernel instead.
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return exit_code.value == STILL_ACTIVE
                return True  # opened but unqueryable — err on "alive" (keeps the lock)
            finally:
                kernel32.CloseHandle(handle)
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False

    def acquire(self) -> bool:
        if self.lock_dir.exists():
            pid_file = self.lock_dir / "pid"
            stale = False
            try:
                pid = int(pid_file.read_text().strip())
                stale = pid == os.getpid() or not self._pid_alive(pid)
            except (OSError, ValueError):
                age = time.time() - self.lock_dir.stat().st_mtime
                stale = age > 24 * 3600
            if not stale:
                return False
            shutil.rmtree(self.lock_dir, ignore_errors=True)
        try:
            self.lock_dir.mkdir()
        except FileExistsError:
            return False  # another run created it between our check and here
        except OSError as e:
            self.error = e
            return False
        try:
            (self.lock_dir / "pid").write_text(str(os.getpid()))
        except OSError as e:
            self.error = e
            shutil.rmtree(self.lock_dir, ignore_errors=True)
            return False
        return True

    def release(self) -> None:
        shutil.rmtree(self.lock_dir, ignore_errors=True)


def _candidate_subfolders(cfg: Config) -> list[Path]:
    """Direct subfolders of inbox/ that may hold input, sorted.

    Names starting with "_" are the pipeline's own (_processing, _done, _error)
    and names starting with "." are hidden; neither is ever input. Symlinked
    folders are not followed: one pointing at _done would reprocess the whole
    archive every pass, and one pointing at someone else's folder would have
    its files moved out.
    """
    if not cfg.inbox_dir.is_dir():
        return []
    return sorted(
        d for d in cfg.inbox_dir.iterdir()
        if d.is_dir() and not d.is_symlink() and not d.name.startswith(("_", "."))
    )


def inbox_folders(cfg: Config) -> list[Path]:
    """The folders one pass looks in.

    Without a `types:` block in config.yaml (0.2): inbox/ itself, then its
    direct subfolders. Subfolders carry no meaning (a recording is processed
    the same wherever it sits); they are scanned so users can keep their own
    order ("inbox/team/", "inbox/calls/"). One level only: a deep tree under
    inbox/ is more likely an accident (a synced folder dropped in whole) than
    an instruction.

    With a `types:` block (a workspace from 0.1): exactly the subfolders the
    block names, matched with normalize_folder_name (so `Private` matches
    `private`, and an NFC name matches an NFD folder), and not inbox/ itself.
    That is what 0.1 scanned, and an upgrade must not start processing (and
    sending to a summarizer) files the user parked elsewhere.
    """
    subfolders = _candidate_subfolders(cfg)
    if cfg.legacy_type_folders is not None:
        return [d for d in subfolders if normalize_folder_name(d.name) in cfg.legacy_type_folders]
    folders = [cfg.inbox_dir, *subfolders] if cfg.inbox_dir.is_dir() else []
    return [d for d in folders if d.is_dir()]


def _is_transcript_only(cfg: Config, folder: Path) -> bool:
    """A folder a 0.1 `types:` block mapped to null stays transcript-only."""
    return (
        cfg.legacy_type_folders is not None
        and folder != cfg.inbox_dir
        and normalize_folder_name(folder.name) in cfg.transcript_only_folders
    )


def _recordings_in(folder: Path) -> int:
    try:
        entries = list(folder.iterdir())
    except OSError:
        return 0
    return sum(
        1 for f in entries
        if not f.name.startswith(".") and f.is_file() and not _ENGINE_TEMP_NAME.fullmatch(f.name)
        and classify_audio(f)
    )


def unscanned_recordings(cfg: Config) -> int:
    """Recordings a `types:` block keeps out of the scan (inbox/ root, unlisted folders).

    0 without a block: then nothing that could be input is left out.
    """
    if cfg.legacy_type_folders is None or not cfg.inbox_dir.is_dir():
        return 0
    scanned = set(inbox_folders(cfg))
    return _recordings_in(cfg.inbox_dir) + sum(
        _recordings_in(d) for d in _candidate_subfolders(cfg) if d not in scanned
    )


class SetupReporter:
    """Logs setup problems, without repeating the same ones every pass.

    scan_inbox makes a fresh one per call by default, so a one-off run always
    logs. `watch` keeps one for its whole life: an unchanged problem list is
    logged again only every `repeat_every` passes, a changed one at once, and
    the moment it clears that is said once too.
    """

    def __init__(self, repeat_every: int | None = None):
        self.repeat_every = repeat_every
        self._last: tuple[str, ...] = ()
        self._since = 0

    def report(self, problems: list[str], *, untouched: bool = True) -> None:
        """Log the problems; `untouched` says no recording was claimed or moved."""
        current = tuple(problems)
        if not current:
            if self._last:
                logger.info("setup problems resolved: processing resumes")
            self._last, self._since = (), 0
            return
        if current == self._last and self.repeat_every is not None:
            self._since += 1
            if self._since < self.repeat_every:
                return
        self._last, self._since = current, 0
        prefix = "setup problem, no recording was touched" if untouched else "setup problem"
        for p in current:
            logger.error(f"{prefix}: {p}")


def _put_back(claimed: Path, folder: Path) -> Path | None:
    """Move a claimed file back to the folder it was claimed from.

    Never onto a file that has appeared there meanwhile: then the claimed one
    goes back as "<stem> (2)<ext>" (or the next free number).
    """
    stem, n = claimed.stem, 2
    dest = folder / claimed.name
    while _exists(dest):
        dest = folder / f"{stem} ({n}){claimed.suffix}"
        n += 1
    return dest if _safe_move(claimed, dest) else None


def _undo_run(cfg: Config, work_products: list[Path], progress: RunProgress) -> None:
    """Delete what one run made: work products, engine temp files, results."""
    for wp in work_products:
        _safe_unlink(wp)
    remove_engine_temp_files(cfg.processing_dir, ENGINE_PID)
    for out in progress.created:
        if _exists(out):
            _safe_unlink(out)
            logger.info(f"removed this run's {out.name} from results/")


def _file_failure(cfg: Config, claimed: Path, stem: str, work_products: list[Path],
                  reason: str) -> None:
    """Move a recording that failed, and its transcript if any, to _error.

    Every step is OSError-safe: a cleanup that raises would end this pass and,
    the file still being in _processing, the quarantine of every later pass.
    """
    logger.error(f"FAILED -> _error: {claimed.name}: {reason}")
    dest = archive_stem(cfg.error_dir, stem, claimed.suffix)
    _safe_move(claimed, cfg.error_dir / f"{dest}{claimed.suffix}")
    for wp in work_products:
        if wp.name.endswith("_transcript_diarized.txt") and _exists(wp):
            _safe_move(wp, cfg.error_dir / f"{dest}{wp.name[len(stem):]}")
        else:
            _safe_unlink(wp)
    remove_engine_temp_files(cfg.processing_dir, ENGINE_PID)


def _put_back_or_park(cfg: Config, claimed: Path, folder: Path) -> str:
    """Return a setup-stopped recording to its folder; say truthfully where it is.

    If its folder is gone (or refuses the move), leaving it in _processing
    would have the next pass quarantine it as if a run had crashed on it. It
    goes to _error now instead, with the reason, and never comes back by
    itself, so the message says how to retry.
    """
    back = _put_back(claimed, folder)
    if back is not None:
        return (f"{claimed.name} was put back where it was, as {back}, and is processed "
                "once the problem is fixed")
    dest = archive_stem(cfg.error_dir, claimed.stem, claimed.suffix)
    parked = cfg.error_dir / f"{dest}{claimed.suffix}"
    if _safe_move(claimed, parked):
        return (f"{claimed.name} could not be put back into {folder}, so it is in {parked}: "
                "it is not processed again by itself, move it back to inbox/ once the "
                "problem is fixed")
    return (f"{claimed.name} could not be put back into {folder} and is still at {claimed}: "
            "the next pass moves it to _error/; move it back to inbox/ once the problem "
            "is fixed")


def _archive_done(cfg: Config, claimed: Path, stem: str, work_products: list[Path],
                  progress: RunProgress) -> None:
    """File a processed recording and its sidecars in _done.

    Which file feeds which tool matters here: `enroll` needs the *jsonl*
    (segment start/end exist nowhere else) and process_recording has already
    copied it into results/; `verify` and manual label fixes read the txt. So
    deleting work_products[0] is safe only because of that copy: if it ever
    stops happening, the recording becomes permanently un-enrollable. Keep the
    two in sync.

    A _done that cannot be written must not end the pass: the recording would
    stay in _processing and the next pass would quarantine it as if a run had
    crashed on it. The least bad place is then _error, said for what it is:
    the recording WAS processed and needs no retry. _error is never picked up
    by itself, so nothing is transcribed twice, and the user sees it there.
    """
    results = ", ".join(str(p) for p in progress.created if _exists(p)) or str(cfg.results_dir)
    _safe_unlink(work_products[0])
    # A recurring name must not replace last session's original (see archive_stem).
    dest = archive_stem(cfg.done_dir, stem, claimed.suffix)
    if _safe_move(claimed, cfg.done_dir / f"{dest}{claimed.suffix}"):
        for wp in work_products[1:]:
            if _exists(wp):
                _safe_move(wp, cfg.done_dir / f"{dest}{wp.name[len(stem):]}")
        if dest != stem:
            logger.info(f"_done already had {claimed.name}; kept both, this one "
                        f"as {dest}{claimed.suffix}")
        logger.info(f"done: {claimed.name}")
        return
    err_dest = archive_stem(cfg.error_dir, stem, claimed.suffix)
    parked = cfg.error_dir / f"{err_dest}{claimed.suffix}"
    if _safe_move(claimed, parked):
        for wp in work_products[1:]:
            if _exists(wp):
                _safe_move(wp, cfg.error_dir / f"{err_dest}{wp.name[len(stem):]}")
        where = f"it is in {parked} only because _done could not be written"
    else:
        where = (f"it is still at {claimed} and the next pass moves it to _error/ as if a "
                 "run had stopped on it")
    logger.error(
        f"{claimed.name} WAS processed (results: {results}), but could not be archived "
        f"to {cfg.done_dir}: {where}. It needs no retry: do not move it back to inbox/ "
        "unless you want it transcribed again"
    )


def scan_inbox(cfg: Config, *, reporter: SetupReporter | None = None) -> dict:
    """One inbox pass: preflight, stabilize, claim, process, and file the outcome.

    Returns {"processed": n, "failed": n, "skipped": n}. When the pass stopped
    on a setup problem (the lock could not be created, the preflight failed,
    or processing raised SystemExit or SetupProblem before the transcript was
    written) the dict also carries "setup_problems", a
    non-empty list of messages. The key is absent otherwise, so a normal
    result compares equal to the three counts. Callers should treat a present
    "setup_problems" like a failure (`scan-inbox` exits non-zero).
    """
    reporter = reporter or SetupReporter()
    counts = {"processed": 0, "failed": 0, "skipped": 0}
    lock = PipelineLock(cfg.workspace)
    if not lock.acquire():
        if lock.error is not None:
            problem = (
                f"cannot create the lock {lock.lock_dir}: {type(lock.error).__name__}: "
                f"{lock.error}: the workspace must be writable; nothing was processed"
            )
            reporter.report([problem])
            return {**counts, "setup_problems": [problem]}
        logger.info("another run is active: yielding this pass")
        return counts
    try:
        # Before anything is claimed or moved, quarantine included: a setup
        # problem must leave every recording exactly where the user put it.
        problems = _preflight.preflight(cfg)
        reporter.report(problems)
        if problems:
            return {**counts, "setup_problems": problems}

        left_out = unscanned_recordings(cfg)
        if left_out:
            logger.warning(
                f"{left_out} recording(s) in inbox/ or in folders that the `types:` block of "
                "config.yaml does not name are not processed: delete the `types:` block from "
                "config.yaml to use the flat inbox, or move them into a listed folder"
            )

        quarantine_leftovers(cfg)
        for folder in inbox_folders(cfg):
            for f in sorted(folder.iterdir()):
                if not f.is_file() or f.name.startswith(".") or not classify_audio(f):
                    continue
                if _ENGINE_TEMP_NAME.fullmatch(f.name):
                    # The engine writes a file of exactly this name next to the
                    # recording it works on, and cleanup deletes it.
                    logger.warning(
                        f"SKIP {f.name}: this name is reserved for temporary files: rename it"
                    )
                    counts["skipped"] += 1
                    continue
                if not is_stable_audio(f):
                    counts["skipped"] += 1
                    continue
                claimed = cfg.processing_dir / f.name
                if _exists(claimed):
                    # A move onto it would replace that file. It is there only
                    # if quarantine could not move it out; never overwrite it.
                    logger.warning(
                        f"SKIP {f.name}: a file of the same name is still in _processing"
                    )
                    counts["skipped"] += 1
                    continue
                try:
                    shutil.move(str(f), claimed)
                except OSError:
                    logger.info(f"SKIP claim failed (processed elsewhere?): {f.name}")
                    counts["skipped"] += 1
                    continue

                stem = claimed.stem
                if not name_fits(cfg.processing_dir, stem):
                    # Starting would fail on the first work product, and before
                    # cleanup was OSError-safe that failure stopped every pass.
                    logger.error(
                        f"filename too long: shorten it and move it back to inbox/: {claimed.name}"
                    )
                    dest = archive_stem(cfg.error_dir, stem, claimed.suffix)
                    _safe_move(claimed, cfg.error_dir / f"{dest}{claimed.suffix}")
                    counts["failed"] += 1
                    continue

                work_products = [cfg.processing_dir / f"{stem}{sfx}" for sfx in WORK_SUFFIXES]
                progress = RunProgress()
                try:
                    process_recording(cfg, claimed, summarize=not _is_transcript_only(cfg, folder),
                                      progress=progress)
                except KeyboardInterrupt:
                    # The user stopped `watch` (or a one-off scan) mid-recording.
                    # Leave things as they were before this file was claimed:
                    # this run's files gone, the recording back in its folder.
                    _undo_run(cfg, work_products, progress)
                    back = _put_back(claimed, folder)
                    if back is not None:
                        logger.warning(f"interrupted: {claimed.name} was put back as {back}")
                    else:
                        logger.error(
                            f"interrupted: {claimed.name} could not be put back and is still in "
                            f"{cfg.processing_dir}: move it back to {folder} yourself"
                        )
                    raise
                except (SystemExit, SetupProblem) as e:
                    if progress.results_written:
                        # Too late to undo: the transcript is in results/, and a
                        # recording put back now would be transcribed again on
                        # every pass. File it as an ordinary failure.
                        _file_failure(cfg, claimed, stem, work_products,
                                      f"setup problem after the transcript was written ({e})")
                        counts["failed"] += 1
                        continue
                    # A setup problem the preflight did not foresee. It is not
                    # this recording's fault, so it must not go to _error, and
                    # SystemExit must not escape: it would end `watch`. Undo the
                    # claim, drop this run's leftovers, stop the pass.
                    _undo_run(cfg, work_products, progress)
                    problem = (
                        f"processing stopped on a setup problem ({e}): "
                        f"{_put_back_or_park(cfg, claimed, folder)}"
                    )
                    reporter.report([problem], untouched=False)
                    return {**counts, "setup_problems": [problem]}
                except Exception as e:  # noqa: BLE001 (one bad file must not stop the pass)
                    _file_failure(cfg, claimed, stem, work_products, str(e))
                    counts["failed"] += 1
                    continue

                _archive_done(cfg, claimed, stem, work_products, progress)
                counts["processed"] += 1
        return counts
    finally:
        lock.release()
