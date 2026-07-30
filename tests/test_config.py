"""Workspace config loading and skeleton creation."""
from moim_scribe.config import DEFAULT_TYPES, ensure_workspace_dirs, load_config


def test_defaults_without_file(tmp_path):
    cfg = load_config(tmp_path)
    assert cfg.language == "ko"
    assert cfg.stt_backend == "mlx"
    assert cfg.summarizer == "claude_cli"
    assert cfg.types == DEFAULT_TYPES
    assert cfg.default_speakers == 0
    assert cfg.speakers_dir == tmp_path / "speakers"


def test_yaml_overrides(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "language: en\nstt_backend: faster\nsummarizer: none\n"
        "default_speakers: 2\ntypes:\n  standup: meeting\n  raw: null\n",
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    assert cfg.language == "en"
    assert cfg.stt_backend == "faster"
    assert cfg.summarizer == "none"
    assert cfg.default_speakers == 2
    assert cfg.types == {"standup": "meeting", "raw": None}


def test_soniox_settings_default_to_local_and_us(tmp_path):
    """The cloud path must never be reachable by omission — a config that says
    nothing about stt has to keep the audio on the machine."""
    cfg = load_config(tmp_path)
    assert cfg.stt_backend != "soniox"
    assert cfg.soniox_region == "us"
    assert cfg.soniox_key_path == tmp_path / ".soniox_key"


def test_soniox_region_is_read_from_yaml(tmp_path):
    """🔴 A new field needs both the dataclass attribute and an entry in
    load_config's key list; with only the attribute it silently keeps its
    default no matter what the file says."""
    (tmp_path / "config.yaml").write_text(
        "stt_backend: soniox\nsoniox_region: eu\n", encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    assert cfg.stt_backend == "soniox"
    assert cfg.soniox_region == "eu"


def test_null_values_keep_defaults(tmp_path):
    (tmp_path / "config.yaml").write_text("stt_model: null\nlanguage:\n", encoding="utf-8")
    cfg = load_config(tmp_path)
    assert cfg.stt_model is None
    assert cfg.language == "ko"


def test_ensure_workspace_dirs(tmp_path):
    cfg = load_config(tmp_path)
    ensure_workspace_dirs(cfg)
    for d in (
        cfg.inbox_dir / "meeting",
        cfg.inbox_dir / "gathering",
        cfg.inbox_dir / "transcript",
        cfg.processing_dir,
        cfg.done_dir,
        cfg.error_dir,
        cfg.results_dir,
        cfg.packs_dir / "lenses",
        cfg.speakers_dir,
    ):
        assert d.is_dir()
