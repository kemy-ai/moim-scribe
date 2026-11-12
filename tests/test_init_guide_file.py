"""`moim-scribe init` puts the CLAUDE.md guide in the workspace, once.

The guide tells Claude Code how to take a speaker count and not to open the
user's recordings. A CLAUDE.md that is already there may be the user's own
file, so init never replaces it.
"""
import argparse
import importlib.resources

from moim_scribe import cli


def _init(ws, language=None, reset=False):
    args = argparse.Namespace(workspace=str(ws), language=language, force=False,
                              reset_config=reset, language_given=language is not None)
    return cli.cmd_init(args)


def _shipped(lang: str) -> str:
    res = importlib.resources.files("moim_scribe") / "templates" / "workspace" / f"CLAUDE.{lang}.md"
    return res.read_text(encoding="utf-8")


def test_new_workspace_gets_the_korean_guide_by_default(tmp_path):
    assert _init(tmp_path) == 0
    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == _shipped("ko")


def test_language_en_gets_the_english_guide(tmp_path):
    assert _init(tmp_path, language="en") == 0
    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == _shipped("en")


def test_other_language_codes_get_the_korean_guide(tmp_path):
    assert _init(tmp_path, language="ja") == 0
    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == _shipped("ko")


def test_existing_claude_md_is_left_alone(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("my own notes\n", encoding="utf-8")
    assert _init(tmp_path, language="en") == 0
    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == "my own notes\n"


def test_reset_config_does_not_replace_claude_md(tmp_path):
    assert _init(tmp_path, language="en") == 0
    (tmp_path / "CLAUDE.md").write_text("edited by the user\n", encoding="utf-8")
    assert _init(tmp_path, language="ko", reset=True) == 0
    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == "edited by the user\n"


def test_rerun_adds_the_guide_to_a_0_2_0_workspace_without_touching_config(tmp_path):
    assert _init(tmp_path, language="en") == 0
    (tmp_path / "CLAUDE.md").unlink()
    cfg = tmp_path / "config.yaml"
    before = cfg.read_text(encoding="utf-8")
    assert _init(tmp_path) == 0  # no --language: follows the config.yaml (en)
    assert cfg.read_text(encoding="utf-8") == before
    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == _shipped("en")


def test_rerun_with_explicit_language_wins_over_the_config(tmp_path):
    assert _init(tmp_path, language="en") == 0
    (tmp_path / "CLAUDE.md").unlink()
    assert _init(tmp_path, language="ko") == 0
    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == _shipped("ko")


def test_reset_without_language_follows_the_fresh_default(tmp_path):
    assert _init(tmp_path, language="en") == 0
    (tmp_path / "CLAUDE.md").unlink()
    assert _init(tmp_path, reset=True) == 0  # fresh config is ko
    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == _shipped("ko")


def test_announces_only_when_it_writes(tmp_path, capsys):
    _init(tmp_path)
    assert "added CLAUDE.md" in capsys.readouterr().out
    _init(tmp_path)
    assert "added CLAUDE.md" not in capsys.readouterr().out
