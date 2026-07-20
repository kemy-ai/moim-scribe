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

import logging
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from .config import AUDIO_EXTS, Config
from .diarize import get_hf_token, run_diarization
from .packs import build_block, save_card
from .speakers.db import SpeakerDB
from .stt import get_backend
from .summarize import get_summarizer

logger = logging.getLogger("moim_scribe.pipeline")

MIN_FILE_BYTES = 10_000
STABILIZE_WAIT_SEC = 3.0

PROMPT_HEADERS = {
    "ko": {
        "rec_info": "=== 녹음 정보 (메타데이터 실측) ===",
        "rec_line": "녹음 일시: {dt}  ※ 정리의 '일시'에 이 값을 쓸 것(전사에 별도 날짜 언급이 없으면)",
        "spk_stats": "=== 화자 통계 (diarize 실측 — 참석 인원·발화 비율은 이 데이터 기준) ===",
        "spk_fixed": "※ 파일명 지정 참석 인원: {n}명 (이 값이 확정값)",
        "transcript": "=== 전사본 ===",
    },
    "en": {
        "rec_info": "=== Recording info (from file metadata) ===",
        "rec_line": "Recorded at: {dt}  (use this for the summary's date unless the transcript says otherwise)",
        "spk_stats": "=== Speaker stats (measured — use these for attendee count and talk shares) ===",
        "spk_fixed": "Note: attendee count fixed by filename: {n} (authoritative)",
        "transcript": "=== Transcript ===",
    },
}

_KO_FILENAME_COUNT = re.compile(r"([0-9]+)\s*[명인]")
_EN_FILENAME_COUNT = re.compile(r"\b([0-9]+)\s*(?:p|ppl|people|speakers?)\b", re.IGNORECASE)


def filename_speaker_hint(title: str, language: str) -> tuple[int | None, str]:
    """Extract an attendee-count hint from the filename and strip it.

    Naming the count in the filename ("book club 5명", "standup 4p") is the
    most reliable source — set at save time, never misheard. Filenames from
    phones are frequently NFD, so normalize first.
    """
    t = unicodedata.normalize("NFC", title)
    pattern = _KO_FILENAME_COUNT if language == "ko" else _EN_FILENAME_COUNT
    m = pattern.search(t)
    if not m:
        return None, t
    count = int(m.group(1))
    cleaned = re.sub(r"\s+", " ", pattern.sub("", t)).strip()
    return count, (cleaned or "untitled")


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
            capture_output=True, text=True, timeout=60,
        )
        raw = r.stdout.strip().splitlines()[0].strip() if r.stdout.strip() else ""
        if not raw:
            return ""
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone()
        h = _headers(language)
        return f"{h['rec_info']}\n{h['rec_line'].format(dt=local.strftime('%Y-%m-%d (%a) %H:%M'))}\n"
    except Exception as e:  # noqa: BLE001 — metadata is a bonus, never fatal
        logger.debug(f"creation_time unavailable: {e}")
        return ""


def _load_lens(cfg: Config, lens: str) -> str:
    """Load a lens with language fallback: <lens>.<lang>.md, then <lens>.md."""
    lenses_dir = cfg.packs_dir / "lenses"
    for candidate in (f"{lens}.{cfg.language}.md", f"{lens}.md"):
        p = lenses_dir / candidate
        if p.is_file():
            return p.read_text(encoding="utf-8")
    raise FileNotFoundError(f"lens not found: {lenses_dir}/{lens}[.{cfg.language}].md")


def _load_common_lens(cfg: Config) -> str:
    lenses_dir = cfg.packs_dir / "lenses"
    for candidate in (f"_common.{cfg.language}.md", "_common.md"):
        p = lenses_dir / candidate
        if p.is_file():
            return p.read_text(encoding="utf-8")
    return ""


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
    return summary


def is_stable_audio(path: Path) -> bool:
    """True when the file is fully present and decodable.

    Cloud placeholders report a final size while content is still syncing,
    so a metadata check is not enough — the file must actually decode.
    """
    try:
        s1 = path.stat().st_size
    except OSError:
        return False
    time.sleep(STABILIZE_WAIT_SEC)
    try:
        s2 = path.stat().st_size
    except OSError:
        return False
    if s1 != s2 or s1 < MIN_FILE_BYTES:
        logger.info(f"SKIP still syncing/too small: {path.name} ({s1} -> {s2})")
        return False
    probe = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-t", "0.1", "-f", "null", "-"],
        capture_output=True, timeout=60,
    )
    if probe.returncode != 0:
        logger.info(f"SKIP not decodable yet (cloud download in progress?): {path.name}")
        return False
    return True


def quarantine_leftovers(cfg: Config) -> None:
    """Move files stranded in _processing (a previous crash) to _error.

    Never auto-retry them: the same input crashed once already, and an
    automatic retry loop would burn the machine on a poison file.
    """
    for f in sorted(cfg.processing_dir.iterdir()) if cfg.processing_dir.exists() else []:
        if not f.is_file() or f.suffix.lower() not in AUDIO_EXTS:
            continue
        stem = f.stem
        for junk in cfg.processing_dir.glob(f"{stem}_transcript_diarized.*"):
            junk.unlink(missing_ok=True)
        (cfg.processing_dir / f"{stem}_speakers.json").unlink(missing_ok=True)
        shutil.move(str(f), cfg.error_dir / f.name)
        logger.warning(f"stranded file quarantined to _error (previous crash): {f.name}")


def process_recording(cfg: Config, claimed: Path, type_folder: str) -> Path:
    """Run one claimed file end-to-end. Returns the primary result path.

    Raises on failure — the caller decides quarantine/cleanup.
    """
    lens_name = cfg.types.get(type_folder, None)
    hint, title = filename_speaker_hint(claimed.stem, cfg.language)
    date_tag = datetime.now().strftime("%y%m%d")
    logger.info(
        f"processing: {claimed.name} (type={type_folder}, title={title}"
        + (f", speakers={hint} from filename)" if hint else ")")
    )

    stt = get_backend(cfg.stt_backend, cfg.stt_model)
    db = SpeakerDB(cfg.speakers_dir)
    token = get_hf_token(cfg.workspace)

    result = run_diarization(
        str(claimed),
        stt_backend=stt,
        language=cfg.language,
        speaker_db=db,
        model_dir=cfg.embed_model_dir,
        hf_token=token,
        glossary_path=cfg.glossary_path,
        num_speakers=hint,
        default_speakers=(cfg.default_speakers or None),
    )
    transcript_path = Path(result["txt_path"])
    if not transcript_path.exists() or transcript_path.stat().st_size == 0:
        raise RuntimeError("empty transcript")

    cfg.results_dir.mkdir(parents=True, exist_ok=True)
    out_txt = cfg.results_dir / f"{date_tag}_{title}_transcript.txt"
    shutil.copy2(transcript_path, out_txt)
    # Keep the segment records too — they are the raw material for
    # `moim-scribe enroll` (mapping SPEAKER_NN ranges to names later).
    jsonl_src = Path(result["jsonl_path"])
    if jsonl_src.exists():
        shutil.copy2(jsonl_src, cfg.results_dir / f"{date_tag}_{title}_diarized.jsonl")

    if lens_name is None:
        logger.info(f"transcribe-only type — done: {out_txt}")
        return out_txt

    h = _headers(cfg.language)
    rec_info = _recording_time_block(claimed, cfg.language)
    stats_path = Path(result["stats_path"])
    spk_stats = ""
    if stats_path.exists():
        spk_stats = f"{h['spk_stats']}\n{stats_path.read_text(encoding='utf-8')}\n"
        if hint:
            spk_stats += h["spk_fixed"].format(n=hint) + "\n"
    ctx_block = build_block(cfg.packs_dir, type_folder, title, cfg.language)

    prompt = (
        f"{_load_common_lens(cfg)}\n{_load_lens(cfg, lens_name)}\n\n"
        f"{rec_info}{spk_stats}{ctx_block}\n"
        f"{h['transcript']}\n{transcript_path.read_text(encoding='utf-8')}"
    )

    summarizer = get_summarizer(cfg.summarizer, cfg.summarizer_model, cfg.summarizer_timeout_sec)
    if summarizer is None:
        logger.info("summarizer=none — transcript only")
        return out_txt

    out_md = cfg.results_dir / f"{date_tag}_{title}_{type_folder}.md"
    try:
        summary = summarizer.summarize(prompt)
    except Exception as e:
        # The transcript is already safe in results/ — surface the summary
        # failure without losing the expensive diarization work.
        raise RuntimeError(f"summarization failed (transcript kept at {out_txt}): {e}") from e
    out_md.write_text(_strip_preamble(summary).strip() + "\n", encoding="utf-8")
    logger.info(f"summary: {out_md}")
    save_card(cfg.packs_dir, type_folder, title, out_md)
    return out_md


class PipelineLock:
    """Cross-platform single-instance lock (directory + pid file).

    Stale detection: if the recorded pid is gone (or unreadable and the
    lock is over 24h old), the lock is broken and reclaimed.
    """

    def __init__(self, workspace: Path):
        self.lock_dir = workspace / ".moim-scribe.lock"

    def _pid_alive(self, pid: int) -> bool:
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
                stale = not self._pid_alive(pid)
            except (OSError, ValueError):
                age = time.time() - self.lock_dir.stat().st_mtime
                stale = age > 24 * 3600
            if not stale:
                return False
            shutil.rmtree(self.lock_dir, ignore_errors=True)
        try:
            self.lock_dir.mkdir()
        except OSError:
            return False
        (self.lock_dir / "pid").write_text(str(os.getpid()))
        return True

    def release(self) -> None:
        shutil.rmtree(self.lock_dir, ignore_errors=True)


def scan_inbox(cfg: Config) -> dict:
    """One inbox pass: stabilize, claim, process, and file the outcome.

    Returns {"processed": n, "failed": n, "skipped": n}.
    """
    lock = PipelineLock(cfg.workspace)
    if not lock.acquire():
        logger.info("another run is active — yielding this pass")
        return {"processed": 0, "failed": 0, "skipped": 0}
    try:
        quarantine_leftovers(cfg)
        counts = {"processed": 0, "failed": 0, "skipped": 0}
        for type_folder in cfg.types:
            folder = cfg.inbox_dir / type_folder
            if not folder.is_dir():
                continue
            for f in sorted(folder.iterdir()):
                if not f.is_file() or f.suffix.lower() not in AUDIO_EXTS:
                    continue
                if not is_stable_audio(f):
                    counts["skipped"] += 1
                    continue
                claimed = cfg.processing_dir / f.name
                try:
                    shutil.move(str(f), claimed)
                except OSError:
                    logger.info(f"SKIP claim failed (processed elsewhere?): {f.name}")
                    counts["skipped"] += 1
                    continue

                stem = claimed.stem
                work_products = [
                    cfg.processing_dir / f"{stem}_transcript_diarized.jsonl",
                    cfg.processing_dir / f"{stem}_transcript_diarized.txt",
                    cfg.processing_dir / f"{stem}_speakers.json",
                ]
                try:
                    process_recording(cfg, claimed, type_folder)
                except Exception as e:  # noqa: BLE001 — one bad file must not stop the pass
                    logger.error(f"FAILED -> _error: {claimed.name} — {e}")
                    for wp in work_products:
                        if wp.name.endswith("_transcript_diarized.txt") and wp.exists():
                            shutil.move(str(wp), cfg.error_dir / wp.name)
                        else:
                            wp.unlink(missing_ok=True)
                    shutil.move(str(claimed), cfg.error_dir / claimed.name)
                    counts["failed"] += 1
                    continue

                # Success: keep speakers.json and the diarized txt in _done —
                # they are the source for later enrollment and label fixes.
                work_products[0].unlink(missing_ok=True)
                for wp in work_products[1:]:
                    if wp.exists():
                        shutil.move(str(wp), cfg.done_dir / wp.name)
                shutil.move(str(claimed), cfg.done_dir / claimed.name)
                logger.info(f"done: {claimed.name}")
                counts["processed"] += 1
        return counts
    finally:
        lock.release()
