"""Second review round: commands, config and leaf modules.

Each test names the finding it covers. The engine is never run here: every
case is decided before a recording would be touched.
"""
import logging
import os
import sys
import unicodedata
from pathlib import Path

import pytest

from moim_scribe import cli
from moim_scribe.config import Config, ConfigError, load_config
from moim_scribe.secrets import clean_secret


def _init(ws, *extra):
    return cli.main(["init", str(ws), *extra])


@pytest.fixture
def ws(tmp_path):
    w = tmp_path / "ws"
    assert _init(w, "--language", "en") == 0
    return w


@pytest.fixture
def clean_env(monkeypatch):
    for k in ("HF_TOKEN", "SONIOX_API_KEY", "OLLAMA_HOST", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)


# --- init --reset-config never overwrites a backup ---------------------------------

def test_reset_config_three_times_keeps_every_earlier_config(ws, capsys):
    cfg = ws / "config.yaml"
    bodies = ["language: en\nsummarizer: none\n", "language: en\n# second\n", "language: en\n# third\n"]
    names = []
    for body in bodies:
        cfg.write_text(body, encoding="utf-8")
        capsys.readouterr()
        assert _init(ws, "--reset-config", "--language", "en") == 0
        out = capsys.readouterr().out
        names.append(next(line for line in out.splitlines() if "saved as" in line))
    assert (ws / "config.yaml.bak").read_text(encoding="utf-8") == bodies[0]
    assert (ws / "config.yaml.bak.2").read_text(encoding="utf-8") == bodies[1]
    assert (ws / "config.yaml.bak.3").read_text(encoding="utf-8") == bodies[2]
    assert names[0].endswith("config.yaml.bak")
    assert names[1].endswith("config.yaml.bak.2")
    assert names[2].endswith("config.yaml.bak.3")


def test_reset_without_language_says_which_language_it_wrote(ws, capsys):
    capsys.readouterr()
    assert _init(ws, "--reset-config") == 0
    out = capsys.readouterr().out
    assert "language ko (the default)" in out and "--language" in out
    assert load_config(ws).language == "ko"


def test_reset_with_language_has_no_language_note(ws, capsys):
    capsys.readouterr()
    assert _init(ws, "--reset-config", "--language", "en") == 0
    assert "(the default)" not in capsys.readouterr().out


def test_first_init_has_no_language_note(tmp_path, capsys):
    assert _init(tmp_path / "w") == 0
    assert "(the default)" not in capsys.readouterr().out


# --- process refuses an inbox file by identity, not by string ----------------------

def _case_insensitive(folder: Path) -> bool:
    return Path(str(folder).upper()).exists()


def test_process_refuses_inbox_file_spelled_in_other_case(ws, capsys):
    if not _case_insensitive(ws / "inbox"):
        pytest.skip("case-sensitive filesystem: the other spelling is another folder")
    (ws / "inbox" / "z.m4a").write_bytes(b"x" * 100)
    assert cli.main(["process", str(ws / "INBOX" / "z.m4a"), "--workspace", str(ws)]) == 1
    assert "inbox/" in capsys.readouterr().err
    assert list((ws / "inbox").glob("z.m4a"))


def test_process_refuses_inbox_file_with_workspace_typed_in_other_case(ws, capsys):
    if not _case_insensitive(ws / "inbox"):
        pytest.skip("case-sensitive filesystem")
    (ws / "inbox" / "z.m4a").write_bytes(b"x" * 100)
    upper_ws = ws.parent / ws.name.upper()
    assert cli.main(["process", str(ws / "inbox" / "z.m4a"), "--workspace", str(upper_ws)]) == 1
    assert "inbox/" in capsys.readouterr().err


def test_process_refuses_inbox_file_in_other_unicode_form(tmp_path, capsys):
    ws = tmp_path / unicodedata.normalize("NFC", "회의")
    assert _init(ws, "--language", "en") == 0
    (ws / "inbox" / "z.m4a").write_bytes(b"x" * 100)
    nfd = Path(unicodedata.normalize("NFD", str(ws))) / "inbox" / "z.m4a"
    if not nfd.exists():
        pytest.skip("normalization-sensitive filesystem")
    capsys.readouterr()
    assert cli.main(["process", str(nfd), "--workspace", str(ws)]) == 1
    assert "inbox/" in capsys.readouterr().err


def test_inside_is_false_for_a_sibling_and_survives_missing_parents(tmp_path):
    (tmp_path / "inbox").mkdir()
    (tmp_path / "inbox2").mkdir()
    assert cli._inside(tmp_path / "inbox2" / "a.m4a", tmp_path / "inbox") is False
    assert cli._inside(tmp_path / "inbox" / "a" / "b.m4a", tmp_path / "inbox") is True
    assert cli._inside(tmp_path / "x.m4a", tmp_path / "nope") is False


# --- doctor data flow is literally true for every combination ----------------------

STT = ("mlx", "faster", "soniox")
SUMMARIZERS = (
    ("claude_cli", None), ("anthropic_api", None),
    ("ollama", None), ("ollama", "gpu-box:11434"), ("none", None),
)


@pytest.mark.parametrize("stt", STT)
@pytest.mark.parametrize("summarizer, host", SUMMARIZERS)
def test_data_flow_is_true_for_every_combination(tmp_path, monkeypatch, stt, summarizer, host):
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    if host:
        monkeypatch.setenv("OLLAMA_HOST", host)
    cfg = Config(workspace=tmp_path, stt_backend=stt, summarizer=summarizer)
    lines = cli.data_flow(cfg)
    audio = next(x for x in lines if x.startswith("audio: "))
    transcript = next(x for x in lines if x.startswith("transcript: "))
    cloud_stt = stt == "soniox"
    to_anthropic = summarizer in ("claude_cli", "anthropic_api")
    remote_ollama = summarizer == "ollama" and host is not None

    if cloud_stt:
        assert audio.startswith("audio: UPLOADED to Soniox")
        assert "delete the upload and the transcription" in audio
        assert "Soniox" in transcript and "stays on this machine" not in transcript
    else:
        assert audio.startswith("audio: stays on this machine")
        assert "Soniox" not in transcript
    assert ("Anthropic" in transcript) == to_anthropic
    assert (host or "\0") in transcript if remote_ollama else "Ollama server" not in transcript
    fully_local = not cloud_stt and not to_anthropic and not remote_ollama
    assert transcript.startswith("transcript: stays on this machine") == fully_local
    assert ("sent to" in transcript) == (to_anthropic or remote_ollama)


@pytest.mark.parametrize("stt, expect", [("soniox", True), ("faster", False)])
def test_data_flow_names_the_glossary_only_when_soniox_gets_it(tmp_path, stt, expect):
    cfg = Config(workspace=tmp_path, stt_backend=stt, summarizer="none")
    assert not any("glossary" in x for x in cli.data_flow(cfg))
    cfg.glossary_path.write_text("names", encoding="utf-8")
    assert any("glossary.txt: sent to Soniox" in x for x in cli.data_flow(cfg)) == expect


def test_doctor_soniox_none_does_not_say_the_transcript_stays(ws, capsys, clean_env):
    (ws / "config.yaml").write_text("stt_backend: soniox\nsummarizer: none\n", encoding="utf-8")
    cli.main(["doctor", str(ws)])
    out = capsys.readouterr().out
    assert "transcript: made by Soniox on its servers" in out
    assert "transcript: stays on this machine" not in out


# --- config: stt alias notice, bare types:, value validation ----------------------

def _cfg(tmp_path, body):
    (tmp_path / "config.yaml").write_text(body, encoding="utf-8")
    return load_config(tmp_path)


def test_stt_alias_is_announced_and_soniox_says_upload(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="moim_scribe.config"):
        cfg = _cfg(tmp_path, "stt: soniox\n")
    assert cfg.stt_backend == "soniox"
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("`stt` is read as `stt_backend`: soniox" in m and "UPLOADS the audio" in m
               for m in msgs)


def test_stt_alias_for_a_local_backend_is_announced_without_upload(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="moim_scribe.config"):
        _cfg(tmp_path, "stt: faster\n")
    msgs = [r.getMessage() for r in caplog.records if "`stt` is read" in r.getMessage()]
    assert len(msgs) == 1 and "UPLOAD" not in msgs[0]


def test_no_alias_notice_without_the_alias(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="moim_scribe.config"):
        _cfg(tmp_path, "stt_backend: soniox\n")
    assert not any("`stt` is read" in r.getMessage() for r in caplog.records)


def test_bare_types_means_the_01_default_map(tmp_path):
    cfg = _cfg(tmp_path, "summarizer: none\ntypes:\n")
    assert cfg.legacy_type_folders == {"meeting", "gathering", "transcript"}
    assert cfg.transcript_only_folders == {"transcript"}


def test_absent_types_is_still_no_block(tmp_path):
    cfg = _cfg(tmp_path, "summarizer: none\n")
    assert cfg.legacy_type_folders is None
    assert cfg.transcript_only_folders == frozenset()


@pytest.mark.parametrize("value", ['"false"', '"true"', "0", "1", "no-thanks"])
def test_hybrid_diarization_must_be_a_real_boolean(tmp_path, value):
    with pytest.raises(ConfigError, match="hybrid_diarization must be true or false"):
        _cfg(tmp_path, f"hybrid_diarization: {value}\n")


@pytest.mark.parametrize("value", ["false", "true", "no", "yes"])
def test_hybrid_diarization_accepts_yaml_booleans(tmp_path, value):
    assert isinstance(_cfg(tmp_path, f"hybrid_diarization: {value}\n").hybrid_diarization, bool)


@pytest.mark.parametrize("value", ["us", "eu", "jp"])
def test_soniox_region_known_values(tmp_path, value):
    assert _cfg(tmp_path, f"soniox_region: {value}\n").soniox_region == value


@pytest.mark.parametrize("value, hint", [("US", "did you mean 'us'"), ("asia", None), ("1", None)])
def test_soniox_region_unknown_values(tmp_path, value, hint):
    with pytest.raises(ConfigError, match="unknown soniox_region") as e:
        _cfg(tmp_path, f"soniox_region: {value}\n")
    if hint:
        assert hint in str(e.value)


def test_cli_prints_a_bad_hybrid_value_without_traceback(ws, capsys):
    (ws / "config.yaml").write_text('hybrid_diarization: "false"\n', encoding="utf-8")
    assert cli.main(["speakers", str(ws)]) == 1
    assert "config error:" in capsys.readouterr().err


# --- materialize_symlinks temp files ------------------------------------------------

symlinks = pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")


@symlinks
def test_temp_copy_names_are_unique_per_process_and_call(tmp_path, monkeypatch):
    from moim_scribe.speakers import embed

    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "blob").write_bytes(b"w")
    model = tmp_path / "m"
    model.mkdir()
    seen = []
    real_copy = embed.shutil.copyfile

    def spy(src, dst):
        seen.append(Path(dst).name)
        return real_copy(src, dst)

    monkeypatch.setattr(embed.shutil, "copyfile", spy)
    for name in ("a.ckpt", "b.ckpt"):
        (model / name).symlink_to(cache / "blob")
    assert embed.materialize_symlinks(model) == 2
    assert len(set(seen)) == 2
    assert all(f".copying-{os.getpid()}-" in n for n in seen)


@symlinks
def test_failed_copy_leaves_no_partial_file(tmp_path, monkeypatch):
    from moim_scribe.speakers import embed

    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "blob").write_bytes(b"weights" * 100)
    model = tmp_path / "m"
    model.mkdir()
    (model / "a.ckpt").symlink_to(cache / "blob")

    def full_disk(src, dst):
        Path(dst).write_bytes(b"part")
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(embed.shutil, "copyfile", full_disk)
    assert embed.materialize_symlinks(model) == 0
    assert sorted(p.name for p in model.iterdir()) == ["a.ckpt"]
    assert (model / "a.ckpt").is_symlink()


def test_leftover_temp_copy_does_not_force_offline(tmp_path, monkeypatch):
    """A killed run left only a temp copy: that is no cache, so loading must
    still be allowed to download instead of being forced offline."""
    import types

    from moim_scribe.speakers import embed

    model = tmp_path / "m"
    model.mkdir()
    (model / "embedding_model.ckpt.copying-123-abcd1234").write_bytes(b"part")
    (model / "old.ckpt.copying").write_bytes(b"part")  # name used before this fix

    class FakeEncoder:
        @classmethod
        def from_hparams(cls, source, savedir, run_opts):
            return object()

    speaker_mod = types.ModuleType("speechbrain.inference.speaker")
    speaker_mod.EncoderClassifier = FakeEncoder
    for name, mod in [
        ("torch", types.ModuleType("torch")),
        ("speechbrain", types.ModuleType("speechbrain")),
        ("speechbrain.inference", types.ModuleType("speechbrain.inference")),
        ("speechbrain.inference.speaker", speaker_mod),
    ]:
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.setattr(embed, "_model", None)
    monkeypatch.setattr(embed, "_model_dir", None)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("TRANSFORMERS_OFFLINE", raising=False)
    embed.get_model(model)
    assert "HF_HUB_OFFLINE" not in os.environ


# --- small ones ------------------------------------------------------------------

def test_gitignore_lists_the_process_work_folder(ws):
    assert ".process-*/" in (ws / ".gitignore").read_text(encoding="utf-8").splitlines()


def test_gitignore_of_an_old_workspace_gains_the_process_work_folder(ws):
    gi = ws / ".gitignore"
    gi.write_text("speakers/\n", encoding="utf-8")
    assert _init(ws) == 0
    assert ".process-*/" in gi.read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize("raw, expect", [
    (b"# my read token\nhf_abc\n", "hf_abc"),
    (b"  # note\r\n\r\nhf_abc\r\n", "hf_abc"),
    (b"# only a comment\n", None),
    ("# note\r\nkey123\r\n".encode("utf-16"), "key123"),
])
def test_clean_secret_skips_comment_lines(raw, expect):
    assert clean_secret(raw) == expect


def test_example_pack_is_only_installed_on_the_first_init(ws, capsys):
    import shutil

    pack = ws / "packs" / "bookclub-demo"
    assert pack.is_dir()
    shutil.rmtree(pack)
    (ws / "packs" / "lenses" / "summary.en.md").unlink()
    assert _init(ws) == 0
    assert not pack.exists()                                       # deletion respected
    assert (ws / "packs" / "lenses" / "summary.en.md").is_file()   # lens still repaired
    assert _init(ws, "--reset-config", "--language", "en") == 0
    assert not pack.exists()


def test_cross_extra_needs_faster_whisper_with_large_v3_turbo():
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
        pytest.skip("tomllib needs Python 3.11")
    root = Path(__file__).resolve().parents[1]
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["optional-dependencies"]["cross"] == ["faster-whisper>=1.1"]
