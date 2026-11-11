"""B1, C1, B5 (config half): config validation, aliases, defaults, legacy types."""
import logging
import platform
import sys
import unicodedata

import pytest

from moim_scribe.config import (
    ConfigError,
    default_stt_backend,
    load_config,
    normalize_folder_name,
)


def _load(tmp_path, text):
    (tmp_path / "config.yaml").write_text(text, encoding="utf-8")
    return load_config(tmp_path)


@pytest.mark.parametrize("text", ["- language: en\n- summarizer: none\n", "just text\n", "language: en\nsummarizer: [none\n"])
def test_non_mapping_or_invalid_yaml_is_a_config_error(tmp_path, text):
    with pytest.raises(ConfigError):
        _load(tmp_path, text)


def test_empty_file_is_defaults(tmp_path):
    assert _load(tmp_path, "").language == "ko"


def test_unknown_key_warns_once_naming_it(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="moim_scribe.config"):
        _load(tmp_path, "language: en\nsumarizer: none\n")
    msgs = [r.message for r in caplog.records if "unknown key" in r.message]
    assert len(msgs) == 1 and "sumarizer" in msgs[0]


def test_stt_is_an_alias_of_stt_backend(tmp_path):
    assert _load(tmp_path, "stt: soniox\nsummarizer: none\n").stt_backend == "soniox"


def test_stt_and_stt_backend_may_repeat_the_same_value(tmp_path):
    assert _load(tmp_path, "stt: faster\nstt_backend: faster\n").stt_backend == "faster"


def test_stt_conflicting_with_stt_backend_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="disagree"):
        _load(tmp_path, "stt: soniox\nstt_backend: faster\n")


@pytest.mark.parametrize("val", ["null", "false", "~"])
def test_null_or_false_summarizer_means_none(tmp_path, caplog, val):
    with caplog.at_level(logging.INFO, logger="moim_scribe.config"):
        cfg = _load(tmp_path, f"summarizer: {val}\n")
    assert cfg.summarizer == "none"
    assert any("treated as 'none'" in r.message for r in caplog.records)


def test_absent_summarizer_keeps_the_default(tmp_path):
    assert _load(tmp_path, "language: en\n").summarizer == "claude_cli"


@pytest.mark.parametrize(
    "text, needle",
    [
        ("summarizer: nonsense\n", "summarizer"),
        ("summarizer: None\n", "did you mean 'none'"),
        ("stt_backend: nonsense\n", "stt_backend"),
        ("stt_backend: Soniox\n", "did you mean 'soniox'"),
        ("default_speakers: -1\n", "default_speakers"),
        ("default_speakers: five\n", "default_speakers"),
        ("default_speakers: 2.5\n", "default_speakers"),
        ("summarizer_timeout_sec: 0\n", "summarizer_timeout_sec"),
        ("summarizer_timeout_sec: -5\n", "summarizer_timeout_sec"),
        ("language: 123\n", "language"),
        ("types: [meeting]\n", "types"),
    ],
)
def test_invalid_values_are_config_errors(tmp_path, text, needle):
    with pytest.raises(ConfigError, match=needle):
        _load(tmp_path, text)


def test_valid_values_load(tmp_path):
    cfg = _load(tmp_path, "stt_backend: soniox\nsummarizer: ollama\ndefault_speakers: 0\nsummarizer_timeout_sec: 30\n")
    assert (cfg.stt_backend, cfg.summarizer, cfg.summarizer_timeout_sec) == ("soniox", "ollama", 30)


@pytest.mark.parametrize(
    "plat, machine, expected",
    [("darwin", "arm64", "mlx"), ("darwin", "x86_64", "faster"), ("linux", "arm64", "faster"), ("win32", "AMD64", "faster")],
)
def test_default_stt_backend_by_platform(monkeypatch, plat, machine, expected):
    monkeypatch.setattr(sys, "platform", plat)
    monkeypatch.setattr(platform, "machine", lambda: machine)
    assert default_stt_backend() == expected


def test_config_default_uses_the_platform_default(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert _load(tmp_path, "language: en\n").stt_backend == "faster"
    assert _load(tmp_path, "stt_backend: mlx\n").stt_backend == "mlx"


def test_no_types_block_means_none(tmp_path):
    cfg = _load(tmp_path, "language: en\n")
    assert cfg.legacy_type_folders is None
    assert cfg.transcript_only_folders == frozenset()


def test_types_block_keeps_all_names_and_null_subset_normalized(tmp_path):
    cfg = _load(tmp_path, "types:\n  Meeting: meeting\n  Private: null\n")
    assert cfg.legacy_type_folders == frozenset({"meeting", "private"})
    assert cfg.transcript_only_folders == frozenset({"private"})


def test_folder_names_compare_with_nfc_and_casefold():
    nfd = unicodedata.normalize("NFD", "회의")
    assert normalize_folder_name(nfd) == normalize_folder_name("회의")
    assert normalize_folder_name("Private") == normalize_folder_name("private")
