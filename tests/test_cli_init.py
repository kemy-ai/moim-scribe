"""What `moim-scribe init` writes, and the two ways it has of going wrong.

CONFIG_TEMPLATE goes through str.format(), so any literal brace added to it
raises KeyError the moment someone runs init — a failure that never shows up
while editing the string.
"""
import argparse

import pytest
import yaml

from moim_scribe import cli
from moim_scribe.config import load_config


def _init(ws, **kw):
    args = argparse.Namespace(workspace=str(ws), language=kw.get("language", "ko"),
                              force=kw.get("force", False))
    return cli.cmd_init(args)


def test_template_renders_without_stray_placeholders():
    """A literal { or } anywhere in the template makes init raise KeyError."""
    rendered = cli.CONFIG_TEMPLATE.format(language="ko", stt_backend="mlx")
    assert "{" not in rendered and "}" not in rendered


def test_rendered_template_is_loadable_yaml(tmp_path):
    rendered = cli.CONFIG_TEMPLATE.format(language="en", stt_backend="faster")
    (tmp_path / "config.yaml").write_text(rendered, encoding="utf-8")
    cfg = load_config(tmp_path)
    assert cfg.language == "en"
    assert cfg.stt_backend == "faster"
    # Commented-out keys must stay commented — an accidentally uncommented
    # soniox_region would silently apply to everyone.
    assert cfg.soniox_region == "us"


def test_template_documents_the_soniox_upload(tmp_path):
    """The one setting that sends audio off the machine has to say so where the
    user actually chooses it, not only in the docs."""
    rendered = cli.CONFIG_TEMPLATE.format(language="ko", stt_backend="mlx")
    stt_block = rendered.split("stt_backend:")[0]
    assert "soniox" in stt_block
    assert "UPLOADS THE AUDIO" in stt_block


def test_init_writes_secrets_into_a_fresh_gitignore(tmp_path):
    assert _init(tmp_path) == 0
    body = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert ".hf_token" in body
    assert ".soniox_key" in body


def test_init_appends_missing_secrets_to_an_existing_gitignore(tmp_path):
    """🔴 The workspace .gitignore predates .soniox_key for anyone who ran init
    before it existed. Skipping the file because it exists would leave the new
    key untracked-but-unignored in exactly those workspaces."""
    gi = tmp_path / ".gitignore"
    gi.write_text("# mine\nspeakers/\n.hf_token\n", encoding="utf-8")
    assert _init(tmp_path) == 0
    body = gi.read_text(encoding="utf-8")
    assert ".soniox_key" in body
    assert "# mine" in body           # the user's own lines survive
    assert body.count(".hf_token") == 1  # and nothing is duplicated


def test_init_adds_a_newline_before_appending_when_needed(tmp_path):
    gi = tmp_path / ".gitignore"
    gi.write_text("speakers/", encoding="utf-8")  # no trailing newline
    _init(tmp_path)
    lines = gi.read_text(encoding="utf-8").splitlines()
    assert "speakers/" in lines
    assert ".soniox_key" in lines  # not glued onto the previous line


def test_init_leaves_a_complete_gitignore_untouched(tmp_path):
    gi = tmp_path / ".gitignore"
    original = "speakers/\n.hf_token\n.soniox_key\n"
    gi.write_text(original, encoding="utf-8")
    _init(tmp_path)
    assert gi.read_text(encoding="utf-8") == original


def test_init_refuses_to_overwrite_config_without_force(tmp_path):
    _init(tmp_path)
    (tmp_path / "config.yaml").write_text("language: xx\n", encoding="utf-8")
    assert _init(tmp_path) == 1
    assert load_config(tmp_path).language == "xx"


def test_init_creates_the_type_folders_from_the_template(tmp_path):
    _init(tmp_path)
    cfg = load_config(tmp_path)
    for name in ("meeting", "gathering", "transcript"):
        assert (cfg.inbox_dir / name).is_dir()
    # conversation ships as a lens and an example pack but is off by default,
    # so its folder must not exist until the user enables the type.
    assert not (cfg.inbox_dir / "conversation").exists()


def test_init_copies_lenses_where_the_loader_looks(tmp_path):
    _init(tmp_path)
    cfg = load_config(tmp_path)
    lenses = {p.name for p in (cfg.packs_dir / "lenses").iterdir()}
    assert "conversation.ko.md" in lenses
    assert "conversation.en.md" in lenses


@pytest.mark.parametrize("backend", ["mlx", "faster"])
def test_template_default_backend_is_a_real_local_backend(tmp_path, backend):
    """init picks by platform and must never pick the cloud one for you."""
    rendered = cli.CONFIG_TEMPLATE.format(language="ko", stt_backend=backend)
    (tmp_path / "config.yaml").write_text(rendered, encoding="utf-8")
    assert load_config(tmp_path).stt_backend == backend
    assert yaml.safe_load(rendered)["stt_backend"] != "soniox"
