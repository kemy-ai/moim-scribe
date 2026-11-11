"""Shared secret reader (C2), message wording (C5) and OLLAMA_HOST (C6)."""
from pathlib import Path

import pytest

from moim_scribe import diarize
from moim_scribe.secrets import GATED_HF_REPOS, read_secret_file
from moim_scribe.stt import soniox_client
from moim_scribe.summarize.ollama import OllamaSummarizer

TOKEN = "hf_abc123"
CASES = {
    "utf8": TOKEN.encode(),
    "utf8_bom": b"\xef\xbb\xbf" + TOKEN.encode() + b"\r\n",
    "utf16le_bom_crlf": b"\xff\xfe" + (TOKEN + "\r\n").encode("utf-16-le"),
    "utf16be_bom": b"\xfe\xff" + (TOKEN + "\n").encode("utf-16-be"),
    "nuls": b"hf_\x00abc\x00123\x00\n",
    "padded": b"  \r\n\t" + TOKEN.encode() + b"  \r\n\r\n",
}


@pytest.mark.parametrize("name", CASES)
def test_reader_cleans_every_encoding(tmp_path, name):
    p = tmp_path / ".hf_token"
    p.write_bytes(CASES[name])
    assert read_secret_file(p) == TOKEN


@pytest.mark.parametrize("raw", [b"", b" \r\n\t\n", b"\xff\xfe\r\x00\n\x00", b"\x00\x00"])
def test_reader_empty_means_absent(tmp_path, raw):
    p = tmp_path / ".hf_token"
    p.write_bytes(raw)
    assert read_secret_file(p) is None


def test_reader_missing_file_is_none(tmp_path):
    assert read_secret_file(tmp_path / "nope") is None


@pytest.mark.parametrize("name", CASES)
def test_hf_token_uses_the_reader(tmp_path, monkeypatch, name):
    monkeypatch.delenv("HF_TOKEN", raising=False)
    (tmp_path / ".hf_token").write_bytes(CASES[name])
    assert diarize.find_hf_token(tmp_path) == TOKEN
    assert diarize.get_hf_token(tmp_path) == TOKEN


def test_find_returns_none_and_get_raises_without_token(tmp_path, monkeypatch):
    monkeypatch.delenv("HF_TOKEN", raising=False)
    (tmp_path / ".hf_token").write_bytes(b"\xff\xfe \x00\n\x00")
    assert diarize.find_hf_token(tmp_path) is None
    with pytest.raises(SystemExit) as e:
        diarize.get_hf_token(tmp_path)
    assert "--token" not in str(e.value)


@pytest.mark.parametrize("name", CASES)
def test_soniox_key_uses_the_reader(tmp_path, monkeypatch, name):
    monkeypatch.delenv("SONIOX_API_KEY", raising=False)
    (tmp_path / ".soniox_key").write_bytes(CASES[name])
    assert soniox_client.get_api_key(tmp_path) == TOKEN


def test_soniox_missing_message_names_stt_backend(tmp_path, monkeypatch):
    monkeypatch.delenv("SONIOX_API_KEY", raising=False)
    with pytest.raises(soniox_client.SonioxKeyMissing) as e:
        soniox_client.get_api_key(tmp_path)
    assert "stt_backend: mlx" in str(e.value)
    assert "stt: " not in str(e.value).replace("stt_backend: ", "")


def test_gated_repos_cover_the_pipeline_the_code_loads():
    assert "pyannote/speaker-diarization-3.1" in GATED_HF_REPOS
    src = Path(diarize.__file__).read_text(encoding="utf-8")
    needle = 'from_pretrained("pyannote/speaker-diarization-3.1"'
    assert needle in src


def test_no_user_facing_stt_key_or_speakers_flag_in_code():
    import pathlib
    root = pathlib.Path(diarize.__file__).parent
    for rel in ("diarize.py", "diarize_cloud.py", "stt/soniox_client.py"):
        text = (root / rel).read_text(encoding="utf-8")
        assert "stt: soniox" not in text and "stt: mlx" not in text, rel
        assert "--speakers" not in text and "or pass --token" not in text, rel


@pytest.mark.parametrize("raw,want", [
    ("0.0.0.0", "http://0.0.0.0"),
    ("myhost:11434", "http://myhost:11434"),
    ("http://h:1/", "http://h:1"),
    ("https://h", "https://h"),
])
def test_ollama_host_gets_a_scheme(monkeypatch, raw, want):
    monkeypatch.setenv("OLLAMA_HOST", raw)
    assert OllamaSummarizer("m").host == want
