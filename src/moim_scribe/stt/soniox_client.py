"""Minimal Soniox async-transcription client, standard library only.

Soniox differs from the whisper backends in a way that shapes this whole file:
it returns *diarized* text. One request yields tokens already tagged with
speaker ids, so the local pyannote step has nothing to do. That is why Soniox
is not an STTBackend — the Protocol in base.py returns (start, end, text) and
has nowhere to put a speaker — and why it gets its own path in diarize_cloud.py.

🔴 urllib, not requests. The CI unit job installs the package with
`pip install -e . --no-deps`, so a new third-party import is not installed and
every matrix job fails on collection. Uploads are streamed through a chained
reader with an explicit Content-Length rather than buffered, because a two-hour
recording is hundreds of megabytes.

🔴 Audio leaves the machine on this path. Everything the local backends do
stays on disk; here the recording is uploaded, transcribed remotely, and then
deleted by cleanup(). Deletion is not optional bookkeeping: Soniox keeps
uploads until told otherwise and caps an organization at 10 GB / 1,000 files,
so a pipeline that never cleans up eventually stops working.
"""
from __future__ import annotations

import io
import json
import logging
import os
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

logger = logging.getLogger("moim_scribe.soniox")

REGION_BASE_URLS = {
    "us": "https://api.soniox.com",
    "eu": "https://api.eu.soniox.com",
    "jp": "https://api.jp.soniox.com",
}
DEFAULT_REGION = "us"
DEFAULT_MODEL = "stt-async-v5"

# Timeouts are per-socket-operation, not total elapsed — a slow but steady
# upload is not cut off, only a connection that has actually stalled.
REQUEST_TIMEOUT_SEC = 30.0
UPLOAD_TIMEOUT_SEC = 1800.0

MAX_RETRIES = 3       # 429 and 5xx only, exponential backoff
HEARTBEAT_SEC = 30.0  # log progress while polling even when nothing changed
POLL_INTERVAL_SEC = 3.0
MAX_WAIT_SEC = 7200.0

# Soniox caps a single file at 300 minutes. The limit is fixed and cannot be
# raised, so checking before upload saves both the wait and the charge.
MAX_AUDIO_MINUTES = 300

# Grouping flat sub-word tokens back into segments needs three cut conditions,
# not just "speaker changed":
SEGMENT_GAP_SEC = 0.8   # silence longer than this ends a segment. Without it a
                        # speaker's turn swallows the pauses inside it, so talk
                        # time is inflated and every duration statistic is off.
SEGMENT_MAX_SEC = 15.0  # 🔴 deliberately equal to diarize.MAX_EMB_SEC. Segments
                        # longer than that are treated as suspected
                        # hallucination and excluded from ECAPA re-assignment
                        # entirely — so if this were larger, speaker
                        # identification would silently stop working on the
                        # cloud path while appearing to run.

CLEANUP_409_RETRY_INTERVAL_SEC = 5.0
CLEANUP_409_RETRIES = 3


class SonioxAPIError(RuntimeError):
    """A Soniox call returned an unexpected status.

    RuntimeError, deliberately: scan_inbox absorbs Exception so one bad
    recording cannot kill the watch loop. A SystemExit here would escape that
    handler and take the whole daemon down.

    `body` is the response body, never request headers, so it is safe to log.
    """

    def __init__(self, status_code: int, body: str, context: str):
        self.status_code = status_code
        self.body = body
        self.context = context
        super().__init__(f"Soniox API error [{context}] HTTP {status_code}: {body[:300]}")


class SonioxKeyMissing(RuntimeError):
    """No API key available. Also a RuntimeError, for the reason above."""


def get_api_key(workspace: Path, arg_key: str | None = None) -> str:
    """Resolve the API key: argument > SONIOX_API_KEY env > <workspace>/.soniox_key."""
    if arg_key:
        return arg_key
    if os.environ.get("SONIOX_API_KEY"):
        return os.environ["SONIOX_API_KEY"]
    p = Path(workspace) / ".soniox_key"
    if p.exists():
        key = p.read_text(encoding="utf-8").strip()
        if key:
            return key
    raise SonioxKeyMissing(
        "No Soniox API key found. Set SONIOX_API_KEY or create "
        f"{p}. See docs/setup-soniox.md — or set stt: mlx in config.yaml "
        "to transcribe locally instead."
    )


def region_base_url(region: str) -> str:
    try:
        return REGION_BASE_URLS[region]
    except KeyError:
        raise ValueError(
            f"unknown Soniox region: {region!r} (expected one of "
            f"{', '.join(sorted(REGION_BASE_URLS))})"
        ) from None


# ── low-level HTTP ───────────────────────────────────────────────────────────

def _retry_wait_seconds(attempt: int) -> float:
    return min(float(2 ** attempt), 60.0)


def _is_retryable(status_code: int) -> bool:
    """429 and 5xx only. Any other 4xx means the request itself is wrong, so
    resending it produces the same answer — return it for the caller to read."""
    return status_code == 429 or 500 <= status_code < 600


def _send(req: urllib.request.Request, timeout: float) -> tuple[int, str]:
    """One request. Returns (status, body) for 4xx/5xx instead of raising, so
    the retry decision and the error interpretation both live in one place."""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def _request(method: str, url: str, key: str, timeout: float,
             json_body: dict | None = None,
             max_retries: int = MAX_RETRIES) -> tuple[int, str]:
    """Shared request path for everything except the upload.

    The upload has its own loop because a retry must reopen the file: a body
    that has already been read leaves its position at the end, so resending the
    same object would transmit nothing.
    """
    data = None
    headers = {"Authorization": f"Bearer {key}"}
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    attempt = 0
    while True:
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            status, body = _send(req, timeout)
        except OSError as e:
            # URLError (a subclass) and bare socket errors both land here.
            if attempt >= max_retries:
                raise
            wait = _retry_wait_seconds(attempt)
            logger.warning(
                f"network error ({type(e).__name__}) — retrying in {wait:.0f}s "
                f"({attempt + 1}/{max_retries}): {method} {url}"
            )
            time.sleep(wait)
            attempt += 1
            continue

        if _is_retryable(status) and attempt < max_retries:
            wait = _retry_wait_seconds(attempt)
            logger.warning(
                f"HTTP {status} — retrying in {wait:.0f}s "
                f"({attempt + 1}/{max_retries}): {method} {url}"
            )
            time.sleep(wait)
            attempt += 1
            continue
        return status, body


def _check(status: int, body: str, context: str, expected: int) -> dict:
    """Raise unless the status matches. An empty body (204) yields {}."""
    if status != expected:
        raise SonioxAPIError(status, body, context)
    return json.loads(body) if body.strip() else {}


class _MultipartBody:
    """Reads preamble, then the file, then epilogue — as one stream.

    Exists so the upload never holds the recording in memory. http.client calls
    read(blocksize) repeatedly when the body has a read attribute and
    Content-Length is set, which streams straight off disk.
    """

    def __init__(self, preamble: bytes, path: Path, epilogue: bytes):
        # The file handle deliberately outlives __init__: the point of this
        # class is to stream the file as the request body instead of reading it
        # into memory, so it cannot be opened inside a `with`. close() releases
        # it, and upload_file() calls that in a finally.
        self._streams = [io.BytesIO(preamble), open(path, "rb"), io.BytesIO(epilogue)]  # noqa: SIM115
        self._at = 0
        self.length = len(preamble) + path.stat().st_size + len(epilogue)

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            chunks = []
            while True:
                chunk = self.read(1 << 20)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)
        while self._at < len(self._streams):
            chunk = self._streams[self._at].read(size)
            if chunk:
                return chunk
            self._at += 1
        return b""

    def close(self) -> None:
        for s in self._streams:
            try:
                s.close()
            except OSError:
                pass


def upload_file(path: str, base_url: str, key: str) -> str:
    """Upload the audio and return its file_id.

    🔴 The multipart filename is fixed to "audio<ext>" rather than the real
    one. Recording names are routinely non-ASCII and often NFD, and a raw UTF-8
    Content-Disposition without RFC 5987 encoding is rejected by strict
    servers. The server only keys on the form field name ("file"), so nothing
    is lost.

    Retried on 429/5xx. A retry whose response was lost in transit can leave an
    orphan upload holding quota — the warning below is what makes that
    visible, since only cleanup() knows the ids it received.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"audio file not found: {path}")

    filename = "audio" + p.suffix
    boundary = f"----moimscribe{uuid.uuid4().hex}"
    preamble = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    epilogue = f"\r\n--{boundary}--\r\n".encode()
    url = f"{base_url}/v1/files"

    attempt = 0
    while True:
        body = _MultipartBody(preamble, p, epilogue)
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(body.length),
            },
        )
        try:
            status, resp_body = _send(req, UPLOAD_TIMEOUT_SEC)
        except OSError as e:
            if attempt >= MAX_RETRIES:
                raise
            wait = _retry_wait_seconds(attempt)
            logger.warning(
                f"upload network error ({type(e).__name__}) — retrying in "
                f"{wait:.0f}s ({attempt + 1}/{MAX_RETRIES}); if the response was "
                "merely lost, an orphan upload may be holding quota"
            )
            time.sleep(wait)
            attempt += 1
            continue
        finally:
            body.close()

        if _is_retryable(status) and attempt < MAX_RETRIES:
            wait = _retry_wait_seconds(attempt)
            logger.warning(f"upload HTTP {status} — retrying in {wait:.0f}s "
                           f"({attempt + 1}/{MAX_RETRIES})")
            time.sleep(wait)
            attempt += 1
            continue

        return _check(status, resp_body, "upload", 201)["id"]


def create_transcription(file_id: str, base_url: str, key: str,
                         language_hints: list[str] | None = None,
                         diarization: bool = True,
                         model: str = DEFAULT_MODEL,
                         enable_language_identification: bool = False,
                         context: str | None = None) -> str:
    """Create the transcription job and return its id.

    🔴 Never retried. This is the only call that creates billable work, and
    Soniox has no idempotency key — so a request whose response was lost may
    already have produced a transcription. Retrying would bill twice and leave
    the first one on the server forever, since cleanup() can only delete ids it
    was told about. Failed jobs are not auto-retried by Soniox either; the
    documented remedy is to submit a new transcription.

    Every field is top-level in the request body; none of them nest.
    """
    hints = list(language_hints) if language_hints is not None else ["ko"]
    body = {
        "model": model,
        "file_id": file_id,
        "language_hints": hints,
        "enable_speaker_diarization": diarization,
        "enable_language_identification": enable_language_identification,
    }
    # Only sent when non-empty: an empty context can draw a 400, and omitting
    # the key keeps the request identical to one made without a glossary.
    if context:
        body["context"] = context
    try:
        status, resp = _request("POST", f"{base_url}/v1/transcriptions", key,
                                timeout=REQUEST_TIMEOUT_SEC, json_body=body,
                                max_retries=0)
    except OSError:
        logger.error(
            "the response was lost, but the transcription may already exist on "
            "the server — check https://console.soniox.com for a duplicate of "
            "this file before re-running"
        )
        raise
    return _check(status, resp, "create_transcription", 201)["id"]


def wait_for_completion(transcription_id: str, base_url: str, key: str,
                        poll_interval: float = POLL_INTERVAL_SEC,
                        max_wait: float = MAX_WAIT_SEC) -> dict:
    """Poll until the job is completed or failed. Returns the final state.

    Logs on every state change and at least every HEARTBEAT_SEC otherwise —
    a long wait with no output is indistinguishable from a hang.
    """
    url = f"{base_url}/v1/transcriptions/{transcription_id}"
    t0 = time.time()
    last_status: str | None = None
    last_heartbeat = t0

    while True:
        elapsed = time.time() - t0
        if elapsed > max_wait:
            raise TimeoutError(
                f"transcription timed out after {max_wait:.0f}s "
                f"(id={transcription_id}, last status={last_status})"
            )

        status, body = _request("GET", url, key, timeout=REQUEST_TIMEOUT_SEC)
        data = _check(status, body, "poll_status", 200)
        state = data.get("status")
        now = time.time()

        if state != last_status:
            logger.info(f"  {last_status or 'submitted'} -> {state} ({elapsed:.0f}s)")
            last_status = state
            last_heartbeat = now
        elif now - last_heartbeat >= HEARTBEAT_SEC:
            logger.info(f"  waiting… status={state} ({elapsed:.0f}s)")
            last_heartbeat = now

        if state == "completed":
            return data
        if state == "error":
            raise SonioxAPIError(
                200, f"{data.get('error_type')}: {data.get('error_message')}",
                "transcription_failed",
            )

        time.sleep(poll_interval)


def get_transcript(transcription_id: str, base_url: str, key: str) -> dict:
    """Fetch the token array. 🔴 Only valid after wait_for_completion —
    calling earlier returns 409 (transcription_invalid_state)."""
    url = f"{base_url}/v1/transcriptions/{transcription_id}/transcript"
    status, body = _request("GET", url, key, timeout=REQUEST_TIMEOUT_SEC)
    return _check(status, body, "get_transcript", 200)


def tokens_to_segments(tokens: list[dict], gap_sec: float = SEGMENT_GAP_SEC,
                       max_sec: float = SEGMENT_MAX_SEC) -> list[dict]:
    """Group flat sub-word tokens into {start, end, speaker, text} segments.

    A new segment starts when any of three things happens: the speaker changes,
    the silence since the previous token reaches gap_sec, or adding this token
    would push the segment past max_sec. Cutting on speaker change alone
    produces very long segments that the downstream MAX_EMB_SEC gate discards,
    which disables speaker re-assignment without any visible error.

    Tokens are sorted by start_ms first — nothing guarantees the server returns
    them in order, and one out-of-order token would corrupt the whole timeline.
    Python's sort is stable, so sub-word order within a timestamp survives.

    start_ms/end_ms are indexed rather than .get() on purpose: they are
    required fields, and failing loudly beats a silently wrong timeline. The
    sort key is the one exception, since ordering cannot corrupt a later
    calculation.
    """
    if not tokens:
        return []

    ordered = sorted(tokens, key=lambda t: t.get("start_ms", 0))

    def _sec(ms: int) -> float:
        return ms / 1000.0

    def _clean(text: str) -> str:
        return " ".join(text.strip().split())

    segments: list[dict] = []
    cur_speaker = ordered[0].get("speaker")
    cur_start = _sec(ordered[0]["start_ms"])
    cur_end = _sec(ordered[0]["end_ms"])
    cur_text = ordered[0].get("text", "")
    prev_end_ms = ordered[0]["end_ms"]

    def _flush() -> None:
        segments.append({
            "start": round(cur_start, 1), "end": round(cur_end, 1),
            "speaker": cur_speaker, "text": _clean(cur_text),
        })

    for tok in ordered[1:]:
        spk = tok.get("speaker")
        start_ms = tok["start_ms"]
        end_ms = tok["end_ms"]
        gap = (start_ms - prev_end_ms) / 1000.0
        would_be_len = _sec(end_ms) - cur_start

        if spk != cur_speaker or gap >= gap_sec or would_be_len > max_sec:
            _flush()
            cur_speaker = spk
            cur_start = _sec(start_ms)
            cur_end = _sec(end_ms)
            cur_text = tok.get("text", "")
        else:
            cur_end = _sec(end_ms)
            cur_text += tok.get("text", "")
        prev_end_ms = end_ms

    _flush()
    return segments


def cleanup(file_id: str | None, transcription_id: str | None,
            base_url: str, key: str) -> None:
    """Delete the uploaded file and the transcription from Soniox.

    🔴 Never raises. The caller writes its output files before calling this, so
    a slow or failed cleanup cannot cost work that was already paid for.

    Deleting a transcription that is still processing returns 409; that is
    retried rather than abandoned. When deletion ultimately fails the log says
    what to delete and where, because the alternative is a recording sitting on
    a third-party server that nobody remembers is there.

    Only the exception type is logged, never its message — urllib error strings
    can carry request detail.
    """
    if transcription_id:
        attempt = 0
        while True:
            try:
                status, _ = _request(
                    "DELETE", f"{base_url}/v1/transcriptions/{transcription_id}",
                    key, timeout=REQUEST_TIMEOUT_SEC,
                )
            except Exception as e:  # noqa: BLE001 — cleanup must not raise
                logger.warning(f"transcription delete raised, continuing: {type(e).__name__}")
                break
            if status == 204:
                logger.info(f"  deleted remote transcription {transcription_id}")
                break
            if status == 409 and attempt < CLEANUP_409_RETRIES:
                attempt += 1
                logger.info(
                    f"  transcription still processing (409) — retrying delete in "
                    f"{CLEANUP_409_RETRY_INTERVAL_SEC:.0f}s ({attempt}/{CLEANUP_409_RETRIES})"
                )
                time.sleep(CLEANUP_409_RETRY_INTERVAL_SEC)
                continue
            logger.warning(
                f"DELETE FAILED (HTTP {status}) — remove transcription "
                f"{transcription_id} by hand at https://console.soniox.com, or it "
                "stays on their servers and counts against your quota"
            )
            break

    if file_id:
        try:
            status, _ = _request("DELETE", f"{base_url}/v1/files/{file_id}", key,
                                 timeout=REQUEST_TIMEOUT_SEC)
        except Exception as e:  # noqa: BLE001 — cleanup must not raise
            logger.warning(f"file delete raised, continuing: {type(e).__name__}")
            return
        if status == 204:
            logger.info(f"  deleted remote file {file_id}")
        else:
            logger.warning(
                f"DELETE FAILED (HTTP {status}) — remove file {file_id} by hand at "
                "https://console.soniox.com; uploads are not auto-deleted and the "
                "organization cap is 10 GB / 1,000 files"
            )
