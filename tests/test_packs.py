"""Pack matching, background-block assembly, and round cards."""
import json
import unicodedata

from moim_scribe.packs import (
    MAX_BLOCK_CHARS,
    build_block,
    build_card,
    find_matching_pack,
    save_card,
)


def make_pack(packs_dir, name, spec, pack_md="background text"):
    d = packs_dir / name
    d.mkdir(parents=True)
    (d / "match.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    if pack_md is not None:
        (d / "pack.md").write_text(pack_md, encoding="utf-8")
    return d


def test_no_match_returns_none(tmp_path):
    assert find_matching_pack(tmp_path, "anything") is None


def test_match_is_by_title_keyword_alone(tmp_path):
    make_pack(tmp_path, "club", {"title_contains": ["scifi"]})
    assert find_matching_pack(tmp_path, "scifi week 3").name == "club"
    assert find_matching_pack(tmp_path, "budget review") is None


def test_a_pre_0_2_pack_with_type_and_style_still_matches(tmp_path):
    """Packs written before 0.2 carry "type" and "style". The type used to gate
    the match; left in place it must not stop the pack from matching now, or
    every existing pack goes quiet on upgrade with nothing in the log."""
    make_pack(tmp_path, "club",
              {"type": "gathering", "title_contains": ["scifi"], "style": "bookclub"},
              pack_md="ROSTER HERE")
    assert find_matching_pack(tmp_path, "scifi week 3").name == "club"
    block = build_block(tmp_path, "scifi week 3", language="en")
    assert "ROSTER HERE" in block


def test_longest_keyword_wins(tmp_path):
    make_pack(tmp_path, "generic", {"title_contains": ["club"]})
    make_pack(tmp_path, "specific", {"title_contains": ["scifi club"]})
    assert find_matching_pack(tmp_path, "scifi club w1").name == "specific"


def test_nfd_title_still_matches(tmp_path):
    # phone-uploaded Korean filenames are NFD; keywords in match.json are NFC
    make_pack(tmp_path, "moim", {"title_contains": ["북클럽"]})
    nfd_title = unicodedata.normalize("NFD", "데모 북클럽 3회")
    assert find_matching_pack(tmp_path, nfd_title).name == "moim"


def test_corrupt_pack_skipped(tmp_path):
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "match.json").write_text("{not json", encoding="utf-8")
    make_pack(tmp_path, "good", {"title_contains": ["ok"]})
    assert find_matching_pack(tmp_path, "ok title").name == "good"


def test_build_block_includes_pack_and_rules(tmp_path):
    make_pack(tmp_path, "club", {"title_contains": ["scifi"]}, pack_md="ROSTER HERE")
    block = build_block(tmp_path, "scifi w1", language="en")
    assert "ROSTER HERE" in block
    assert "NOT from this recording" in block  # header
    assert "transcript wins" in block  # rules survive


def test_build_block_reads_no_style_file(tmp_path):
    """There is one output format. A styles/ folder left over from before 0.2
    must not leak a second one into the prompt."""
    styles = tmp_path / "styles"
    styles.mkdir()
    (styles / "bookclub.md").write_text("STYLE TEMPLATE", encoding="utf-8")
    (styles / "bookclub.en.md").write_text("STYLE TEMPLATE EN", encoding="utf-8")
    make_pack(tmp_path, "club", {"title_contains": ["scifi"], "style": "bookclub"},
              pack_md="ROSTER HERE")
    block = build_block(tmp_path, "scifi w1", language="en")
    assert "ROSTER HERE" in block
    assert "STYLE TEMPLATE" not in block


def test_build_block_includes_the_latest_session_file(tmp_path):
    d = make_pack(tmp_path, "club", {"title_contains": ["scifi"]}, pack_md="ROSTER HERE")
    (d / "session").mkdir()
    (d / "session" / "2026-01-01.md").write_text("OLD NOTES", encoding="utf-8")
    (d / "session" / "2026-02-01.md").write_text("NEW NOTES", encoding="utf-8")
    block = build_block(tmp_path, "scifi w1", language="en")
    assert "NEW NOTES" in block and "OLD NOTES" not in block


def test_build_block_no_match_is_empty(tmp_path):
    assert build_block(tmp_path, "nothing") == ""


def test_build_block_truncates_but_keeps_rules(tmp_path):
    make_pack(tmp_path, "big", {"title_contains": ["big"]},
              pack_md="x" * (MAX_BLOCK_CHARS * 2))
    block = build_block(tmp_path, "big one", language="en")
    assert len(block) <= MAX_BLOCK_CHARS
    assert "transcript wins" in block


def test_round_card_extracts_sections(tmp_path):
    md = tmp_path / "out.md"
    md.write_text(
        "# Notes\n\n## Decisions\n- ship it\n\n## Irrelevant\n- noise\n\n## Open items\n- next book\n",
        encoding="utf-8",
    )
    card = build_card("scifi w1", md)
    assert "- ship it" in card and "- next book" in card and "noise" not in card
    assert "type:" not in card


def test_round_card_extracts_korean_sections(tmp_path):
    md = tmp_path / "out.md"
    md.write_text(
        "# 정리\n\n## 결정사항\n- 출시한다\n\n## 주요 내용\n- 잡담\n\n## 보류·다음에 이어갈 것\n- 다음 책\n",
        encoding="utf-8",
    )
    card = build_card("북클럽 3회", md)
    assert "- 출시한다" in card and "- 다음 책" in card and "잡담" not in card


def test_save_card_appends_uniquely(tmp_path):
    make_pack(tmp_path, "club", {"title_contains": ["scifi"]})
    md = tmp_path / "out.md"
    md.write_text("# Notes\n", encoding="utf-8")
    save_card(tmp_path, "scifi w1", md)
    save_card(tmp_path, "scifi w1", md)
    cards = list((tmp_path / "club" / "_history").glob("*.md"))
    assert len(cards) == 2  # second save gets a _2 suffix, never overwrites


def test_save_card_unmatched_is_noop(tmp_path):
    md = tmp_path / "out.md"
    md.write_text("# Notes\n", encoding="utf-8")
    save_card(tmp_path, "unmatched", md)
    assert not any(p.is_dir() and (p / "_history").exists() for p in tmp_path.iterdir())
