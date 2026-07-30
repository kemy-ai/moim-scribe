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
from .speaker_stats import render_speaker_stats_block
from .speakers.db import SpeakerDB
from .stt import get_backend
from .summarize import get_summarizer

logger = logging.getLogger("moim_scribe.pipeline")

MIN_FILE_BYTES = 10_000
STABILIZE_WAIT_SEC = 3.0
PREFETCH_CHUNK_BYTES = 1 << 20
PREFETCH_SLOW_SEC = 5.0  # above this, say so: the whole pass was held meanwhile

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
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if probe.returncode == 0 and probe.stdout.strip() == "audio":
        # This log line is the reason the function exists: the user has to learn
        # that an extension went missing, or it happens again.
        logger.info(f"audio with no usable extension — processing anyway: {path.name}")
        return True
    return False


def _prefetch(path: Path) -> None:
    """Make a cloud-sync provider materialize an online-only placeholder.

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
    try:
        with path.open("rb") as fh:
            while fh.read(PREFETCH_CHUNK_BYTES):
                pass
    except OSError as e:
        # Not fatal: the checks below decide. A stub that cannot be read yet just
        # gets skipped this pass, exactly as before.
        logger.info(f"prefetch skipped ({type(e).__name__}) — continuing: {path.name}")
        return
    elapsed = time.time() - t0
    if elapsed > PREFETCH_SLOW_SEC:
        logger.info(
            f"prefetch of {path.name} took {elapsed:.0f}s (cloud download) — "
            "the pipeline was held for that time"
        )


def is_stable_audio(path: Path) -> bool:
    """True when the file is fully present and decodable.

    Cloud placeholders report a final size while content is still syncing,
    so a metadata check is not enough — the file must actually decode.
    """
    _prefetch(path)
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
        # Same gate as the inbox scan: a claimed file whose extension was
        # stripped would otherwise be invisible here too, and stay in
        # _processing forever with nothing logged.
        if not f.is_file() or not classify_audio(f):
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
    ctx_block = build_block(cfg.packs_dir, type_folder, title, cfg.language)

    # The filename is the one name source a human set deliberately, at save
    # time, so for people's names it beats transcription: a self-introduction
    # spoken seconds after someone else's gets pulled toward the wrong surname.
    # Until this block existed the title was computed for pack matching and
    # then thrown away, so the deliverable had no way to know it.
    title_block = h["rec_title"].format(title=title) + "\n"
    prompt = (
        f"{_load_common_lens(cfg)}\n{_load_lens(cfg, lens_name)}\n\n"
        f"{title_block}{rec_info}{spk_stats}{ctx_block}\n"
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
                if not f.is_file() or not classify_audio(f):
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

                # Success: move the txt and speakers.json to _done, drop the
                # working jsonl. Which file feeds which tool matters here:
                # `enroll` needs the *jsonl* (segment start/end exist nowhere
                # else) and process_recording has already copied it into
                # results/; `verify` and manual label fixes read the txt.
                # So deleting work_products[0] is safe only because of that
                # copy — if it ever stops happening, the recording becomes
                # permanently un-enrollable. Keep the two in sync.
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
