"""The Soniox client, exercised without a network.

Everything here is either pure (token grouping, retry predicates, the
multipart body) or stubbed at the transport seam.
"""
import io
import json

import pytest

from moim_scribe.stt import soniox_client as sc

# ── key resolution ───────────────────────────────────────────────────────────

def test_key_precedence_argument_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("SONIOX_API_KEY", "from-env")
    (tmp_path / ".soniox_key").write_text("from-file", encoding="utf-8")
    assert sc.get_api_key(tmp_path, "from-arg") == "from-arg"


def test_key_precedence_env_beats_file(tmp_path, monkeypatch):
    monkeypatch.setenv("SONIOX_API_KEY", "from-env")
    (tmp_path / ".soniox_key").write_text("from-file", encoding="utf-8")
    assert sc.get_api_key(tmp_path) == "from-env"


def test_key_from_file_is_stripped(tmp_path, monkeypatch):
    monkeypatch.delenv("SONIOX_API_KEY", raising=False)
    (tmp_path / ".soniox_key").write_text("  from-file\n", encoding="utf-8")
    assert sc.get_api_key(tmp_path) == "from-file"


def test_blank_key_file_counts_as_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("SONIOX_API_KEY", raising=False)
    (tmp_path / ".soniox_key").write_text("\n \n", encoding="utf-8")
    with pytest.raises(sc.SonioxKeyMissing):
        sc.get_api_key(tmp_path)


def test_missing_key_is_catchable_as_exception(tmp_path, monkeypatch):
    """🔴 Not SystemExit. scan_inbox absorbs Exception so one bad recording
    cannot end the watch loop; a BaseException here would kill the daemon on
    every pass instead of failing that one file."""
    monkeypatch.delenv("SONIOX_API_KEY", raising=False)
    assert issubclass(sc.SonioxKeyMissing, Exception)
    assert not issubclass(sc.SonioxKeyMissing, SystemExit)
    try:
        sc.get_api_key(tmp_path)
    except Exception as e:  # noqa: BLE001 — that it is catchable is the point
        assert "setup-soniox" in str(e)
    else:
        pytest.fail("expected SonioxKeyMissing")


# ── region ───────────────────────────────────────────────────────────────────

def test_all_regions_resolve():
    for region in ("us", "eu", "jp"):
        assert sc.region_base_url(region).startswith("https://")


def test_unknown_region_lists_the_valid_ones():
    with pytest.raises(ValueError, match="eu, jp, us"):
        sc.region_base_url("kr")


# ── retry policy ─────────────────────────────────────────────────────────────

def test_retryable_statuses():
    assert sc._is_retryable(429)
    assert sc._is_retryable(500)
    assert sc._is_retryable(503)


def test_client_errors_are_not_retried():
    """Resending a 400/401/402/404 produces the same answer, and 409 has its
    own handling in cleanup()."""
    for status in (400, 401, 402, 404, 409):
        assert not sc._is_retryable(status)


def test_backoff_is_bounded():
    assert sc._retry_wait_seconds(0) == 1.0
    assert sc._retry_wait_seconds(3) == 8.0
    assert sc._retry_wait_seconds(30) == 60.0


# ── response checking ────────────────────────────────────────────────────────

def test_check_returns_parsed_body():
    assert sc._check(200, '{"id": "x"}', "ctx", 200) == {"id": "x"}


def test_check_treats_empty_body_as_empty_dict():
    assert sc._check(204, "", "delete", 204) == {}


def test_check_raises_with_status_and_context():
    with pytest.raises(sc.SonioxAPIError) as ei:
        sc._check(402, "out of credit", "upload", 201)
    assert ei.value.status_code == 402
    assert ei.value.context == "upload"
    assert issubclass(sc.SonioxAPIError, Exception)


# ── multipart body ───────────────────────────────────────────────────────────

def test_multipart_body_length_matches_what_it_yields(tmp_path):
    f = tmp_path / "rec.m4a"
    f.write_bytes(b"a" * 5000)
    body = sc._MultipartBody(b"PRE", f, b"POST")
    try:
        read = b""
        while True:
            chunk = body.read(512)
            if not chunk:
                break
            read += chunk
        # A mismatch here would make the server hang waiting for bytes that
        # never come, or reject the request as malformed.
        assert len(read) == body.length
        assert read.startswith(b"PRE") and read.endswith(b"POST")
    finally:
        body.close()


def test_multipart_body_preserves_file_bytes(tmp_path):
    f = tmp_path / "rec.m4a"
    f.write_bytes(bytes(range(256)))
    body = sc._MultipartBody(b"<", f, b">")
    try:
        assert body.read(-1) == b"<" + bytes(range(256)) + b">"
    finally:
        body.close()


def test_multipart_body_crosses_part_boundaries(tmp_path):
    """A read larger than the current part must not silently skip the rest."""
    f = tmp_path / "rec.m4a"
    f.write_bytes(b"MIDDLE")
    body = sc._MultipartBody(b"AB", f, b"YZ")
    try:
        chunks = []
        while True:
            c = body.read(4)
            if not c:
                break
            chunks.append(c)
        assert b"".join(chunks) == b"ABMIDDLEYZ"
    finally:
        body.close()


# ── token grouping ───────────────────────────────────────────────────────────

def _tok(start, end, text, speaker=None):
    return {"start_ms": start, "end_ms": end, "text": text, "speaker": speaker}


def test_no_tokens_gives_no_segments():
    assert sc.tokens_to_segments([]) == []


def test_subwords_join_without_separators():
    segs = sc.tokens_to_segments([
        _tok(0, 100, "안녕", "1"), _tok(100, 200, "하세요", "1"),
    ])
    assert len(segs) == 1
    assert segs[0]["text"] == "안녕하세요"


def test_speaker_change_splits():
    segs = sc.tokens_to_segments([
        _tok(0, 100, "hi", "1"), _tok(100, 200, "there", "2"),
    ])
    assert [s["speaker"] for s in segs] == ["1", "2"]


def test_silence_longer_than_gap_splits():
    segs = sc.tokens_to_segments([
        _tok(0, 100, "one", "1"),
        _tok(2000, 2100, "two", "1"),  # 1.9s gap, same speaker
    ])
    assert len(segs) == 2


def test_silence_shorter_than_gap_does_not_split():
    segs = sc.tokens_to_segments([
        _tok(0, 100, "one", "1"),
        _tok(500, 600, "two", "1"),  # 0.4s gap < 0.8
    ])
    assert len(segs) == 1


def test_long_run_is_force_split_below_the_embedding_cap():
    """🔴 Segments longer than SEGMENT_MAX_SEC are dropped from ECAPA
    re-assignment as suspected hallucination, so a run that never splits would
    disable speaker identification without any error."""
    tokens = [_tok(i * 500, i * 500 + 500, "x", "1") for i in range(80)]  # 40s, no gaps
    segs = sc.tokens_to_segments(tokens)
    assert len(segs) > 1
    assert max(s["end"] - s["start"] for s in segs) <= sc.SEGMENT_MAX_SEC


def test_out_of_order_tokens_are_sorted_first():
    segs = sc.tokens_to_segments([
        _tok(1000, 1100, "second", "1"), _tok(0, 100, "first", "1"),
    ])
    assert segs[0]["text"] == "first"
    assert segs[0]["start"] < segs[-1]["start"]


def test_missing_speaker_key_is_tolerated():
    """With diarization off the key is absent entirely, not None."""
    segs = sc.tokens_to_segments([
        {"start_ms": 0, "end_ms": 100, "text": "a"},
        {"start_ms": 100, "end_ms": 200, "text": "b"},
    ])
    assert len(segs) == 1
    assert segs[0]["speaker"] is None


def test_missing_timestamp_fails_loudly():
    """Indexing rather than .get(): a silently wrong timeline is worse than a
    crash, because every downstream duration depends on it."""
    with pytest.raises(KeyError):
        sc.tokens_to_segments([{"text": "a", "speaker": "1"}])


# ── cleanup ──────────────────────────────────────────────────────────────────

def test_cleanup_never_raises_even_when_transport_fails(monkeypatch, caplog):
    def boom(*a, **k):
        raise OSError("connection reset")

    monkeypatch.setattr(sc, "_request", boom)
    with caplog.at_level("WARNING"):
        sc.cleanup("file-1", "tr-1", "https://api.soniox.com", "key")
    # The outputs are already on disk by the time cleanup runs; raising here
    # would turn a bookkeeping failure into lost work.
    assert "OSError" in caplog.text


def test_cleanup_tells_you_what_to_delete_when_it_cannot(monkeypatch, caplog):
    monkeypatch.setattr(sc, "_request", lambda *a, **k: (500, ""))
    monkeypatch.setattr(sc.time, "sleep", lambda s: None)
    with caplog.at_level("WARNING"):
        sc.cleanup("file-9", "tr-9", "https://api.soniox.com", "key")
    # Both ids must appear: an upload nobody deletes counts against the quota
    # until someone does it by hand.
    assert "tr-9" in caplog.text and "file-9" in caplog.text
    assert "console.soniox.com" in caplog.text


def test_cleanup_retries_409_then_gives_instructions(monkeypatch, caplog):
    calls = []

    def fake(method, url, key, timeout, **kw):
        calls.append(url)
        return (409, "still processing")

    monkeypatch.setattr(sc, "_request", fake)
    monkeypatch.setattr(sc.time, "sleep", lambda s: None)
    with caplog.at_level("INFO"):
        sc.cleanup(None, "tr-x", "https://api.soniox.com", "key")
    assert len([c for c in calls if "transcriptions" in c]) == sc.CLEANUP_409_RETRIES + 1
    assert "DELETE FAILED" in caplog.text


def test_cleanup_skips_calls_for_ids_it_does_not_have(monkeypatch):
    calls = []
    monkeypatch.setattr(sc, "_request",
                        lambda m, u, k, timeout, **kw: calls.append(u) or (204, ""))
    sc.cleanup(None, None, "https://api.soniox.com", "key")
    assert calls == []


# ── create_transcription: the one call that must not be retried ──────────────

def test_create_transcription_disables_retries(monkeypatch):
    seen = {}

    def fake_request(method, url, key, timeout, json_body=None, max_retries=sc.MAX_RETRIES):
        seen["max_retries"] = max_retries
        seen["body"] = json_body
        return 201, json.dumps({"id": "tr-1"})

    monkeypatch.setattr(sc, "_request", fake_request)
    assert sc.create_transcription("f1", "https://api.soniox.com", "key") == "tr-1"
    # 🔴 Soniox has no idempotency key, so a retry after a lost response bills a
    # second transcription and orphans the first.
    assert seen["max_retries"] == 0
    assert seen["body"]["enable_speaker_diarization"] is True
    assert "context" not in seen["body"]


def test_create_transcription_sends_context_only_when_present(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        sc, "_request",
        lambda *a, json_body=None, **k: (seen.update(body=json_body), (201, '{"id":"t"}'))[1],
    )
    sc.create_transcription("f1", "https://api.soniox.com", "key", context="proper nouns")
    assert seen["body"]["context"] == "proper nouns"


def test_wait_for_completion_raises_on_error_state(monkeypatch):
    monkeypatch.setattr(
        sc, "_request",
        lambda *a, **k: (200, json.dumps({"status": "error", "error_type": "bad_audio",
                                          "error_message": "could not decode"})),
    )
    with pytest.raises(sc.SonioxAPIError, match="bad_audio"):
        sc.wait_for_completion("tr-1", "https://api.soniox.com", "key")


def test_wait_for_completion_returns_on_completed(monkeypatch):
    monkeypatch.setattr(sc, "_request",
                        lambda *a, **k: (200, json.dumps({"status": "completed"})))
    assert sc.wait_for_completion("tr-1", "https://api.soniox.com", "key")["status"] == "completed"


def test_wait_for_completion_stops_waiting_eventually(monkeypatch):
    monkeypatch.setattr(sc, "_request",
                        lambda *a, **k: (200, json.dumps({"status": "processing"})))
    monkeypatch.setattr(sc.time, "sleep", lambda s: None)
    with pytest.raises(TimeoutError):
        sc.wait_for_completion("tr-1", "https://api.soniox.com", "key",
                               poll_interval=0, max_wait=0)


def test_upload_uses_an_ascii_filename(tmp_path, monkeypatch):
    """Recording names are routinely non-ASCII and often NFD; a raw UTF-8
    Content-Disposition without RFC 5987 encoding can be rejected outright."""
    f = tmp_path / "뉴스 회의 3명.m4a"
    f.write_bytes(b"x" * 100)
    captured = {}

    def fake_send(req, timeout):
        captured["headers"] = dict(req.headers)
        captured["body_head"] = req.data.read(200)
        return 201, json.dumps({"id": "file-1"})

    monkeypatch.setattr(sc, "_send", fake_send)
    assert sc.upload_file(str(f), "https://api.soniox.com", "key") == "file-1"
    assert b'filename="audio.m4a"' in captured["body_head"]
    assert captured["headers"]["Content-length"] is not None


def test_upload_missing_file_raises_before_any_request(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "_send", lambda *a: pytest.fail("must not send"))
    with pytest.raises(FileNotFoundError):
        sc.upload_file(str(tmp_path / "nope.m4a"), "https://api.soniox.com", "key")


def test_request_sets_json_content_type(monkeypatch):
    captured = {}

    def fake_send(req, timeout):
        captured["type"] = req.headers.get("Content-type")
        captured["auth"] = req.headers.get("Authorization")
        captured["data"] = req.data
        return 201, "{}"

    monkeypatch.setattr(sc, "_send", fake_send)
    sc._request("POST", "https://x/y", "secret", timeout=1, json_body={"a": 1})
    assert captured["type"] == "application/json"
    assert captured["auth"] == "Bearer secret"
    assert json.loads(captured["data"]) == {"a": 1}


def test_request_retries_then_returns(monkeypatch):
    statuses = iter([503, 503, 200])
    monkeypatch.setattr(sc, "_send", lambda req, t: (next(statuses), "{}"))
    monkeypatch.setattr(sc.time, "sleep", lambda s: None)
    status, _ = sc._request("GET", "https://x/y", "k", timeout=1)
    assert status == 200


def test_request_gives_up_after_max_retries(monkeypatch):
    monkeypatch.setattr(sc, "_send", lambda req, t: (503, "still down"))
    monkeypatch.setattr(sc.time, "sleep", lambda s: None)
    status, body = sc._request("GET", "https://x/y", "k", timeout=1, max_retries=2)
    assert status == 503  # returned, not raised — the caller interprets it
    assert body == "still down"


def test_send_returns_body_for_http_errors(monkeypatch):
    import urllib.error

    def raise_http(req, timeout=None):
        raise urllib.error.HTTPError(
            "https://x", 402, "Payment Required", {}, io.BytesIO(b"no credit"),
        )

    monkeypatch.setattr(sc.urllib.request, "urlopen", raise_http)
    status, body = sc._send(sc.urllib.request.Request("https://x"), 1)
    assert (status, body) == (402, "no credit")
