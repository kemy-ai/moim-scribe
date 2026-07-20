"""Backend factories and their failure messages (no network, no models)."""
import pytest

from moim_scribe.stt.base import DEFAULT_MODELS as STT_DEFAULTS
from moim_scribe.stt.base import get_backend
from moim_scribe.summarize import get_summarizer
from moim_scribe.summarize.claude_cli import ClaudeCliSummarizer


def test_stt_unknown_backend():
    with pytest.raises(ValueError, match="unknown STT backend"):
        get_backend("whisperx")


def test_stt_defaults_exist():
    assert set(STT_DEFAULTS) == {"mlx", "faster"}


def test_summarizer_none_is_none():
    assert get_summarizer("none") is None


def test_summarizer_unknown_raises():
    with pytest.raises(ValueError, match="unknown summarizer"):
        get_summarizer("gpt")


def test_summarizer_factory_builds_cli():
    s = get_summarizer("claude_cli", model="sonnet", timeout_sec=5)
    assert isinstance(s, ClaudeCliSummarizer)
    assert s.model == "sonnet" and s.timeout_sec == 5


def test_claude_cli_missing_executable_message():
    s = ClaudeCliSummarizer("sonnet", executable=None)
    s.executable = "/nonexistent/claude-binary"
    with pytest.raises(Exception):
        s.summarize("prompt")


def test_anthropic_api_requires_key(monkeypatch):
    from moim_scribe.summarize.anthropic_api import AnthropicApiSummarizer

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        AnthropicApiSummarizer("claude-sonnet-5").summarize("hi")


def test_ollama_connection_error_message():
    from moim_scribe.summarize.ollama import OllamaSummarizer

    s = OllamaSummarizer("llama3.1", timeout_sec=1, host="http://127.0.0.1:59999")
    with pytest.raises(RuntimeError, match="Ollama"):
        s.summarize("hi")
