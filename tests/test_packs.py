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
    assert find_matching_pack(tmp_path, "gathering", "anything") is None


def test_basic_match_and_type_gate(tmp_path):
    make_pack(tmp_path, "club", {"type": "gathering", "title_contains": ["scifi"]})
    assert find_matching_pack(tmp_path, "gathering", "scifi week 3").name == "club"
    assert find_matching_pack(tmp_path, "meeting", "scifi week 3") is None


def test_longest_keyword_wins(tmp_path):
    make_pack(tmp_path, "generic", {"type": "gathering", "title_contains": ["club"]})
    make_pack(tmp_path, "specific", {"type": "gathering", "title_contains": ["scifi club"]})
    assert find_matching_pack(tmp_path, "gathering", "scifi club w1").name == "specific"


def test_nfd_title_still_matches(tmp_path):
    # phone-uploaded Korean filenames are NFD; keywords in match.json are NFC
    make_pack(tmp_path, "moim", {"type": "gathering", "title_contains": ["북클럽"]})
    nfd_title = unicodedata.normalize("NFD", "데모 북클럽 3회")
    assert find_matching_pack(tmp_path, "gathering", nfd_title).name == "moim"


def test_corrupt_pack_skipped(tmp_path):
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "match.json").write_text("{not json", encoding="utf-8")
    make_pack(tmp_path, "good", {"type": "gathering", "title_contains": ["ok"]})
    assert find_matching_pack(tmp_path, "gathering", "ok title").name == "good"


def test_build_block_includes_style_pack_and_rules(tmp_path):
    styles = tmp_path / "styles"
    styles.mkdir()
    (styles / "bookclub.md").write_text("STYLE TEMPLATE", encoding="utf-8")
    make_pack(
        tmp_path, "club",
        {"type": "gathering", "title_contains": ["scifi"], "style": "bookclub"},
        pack_md="ROSTER HERE",
    )
    block = build_block(tmp_path, "gathering", "scifi w1", language="en")
    assert "STYLE TEMPLATE" in block and "ROSTER HERE" in block
    assert "NOT from this recording" in block  # header
    assert "transcript wins" in block  # rules survive


def test_build_block_style_language_fallback(tmp_path):
    styles = tmp_path / "styles"
    styles.mkdir()
    (styles / "bookclub.ko.md").write_text("KO STYLE", encoding="utf-8")
    (styles / "bookclub.en.md").write_text("EN STYLE", encoding="utf-8")
    make_pack(tmp_path, "club",
              {"type": "gathering", "title_contains": ["scifi"], "style": "bookclub"})
    ko_block = build_block(tmp_path, "gathering", "scifi w1", language="ko")
    en_block = build_block(tmp_path, "gathering", "scifi w1", language="en")
    assert "KO STYLE" in ko_block and "EN STYLE" not in ko_block
    assert "EN STYLE" in en_block and "KO STYLE" not in en_block


def test_build_block_no_match_is_empty(tmp_path):
    assert build_block(tmp_path, "gathering", "nothing") == ""


def test_build_block_truncates_but_keeps_rules(tmp_path):
    make_pack(tmp_path, "big", {"type": "gathering", "title_contains": ["big"]},
              pack_md="x" * (MAX_BLOCK_CHARS * 2))
    block = build_block(tmp_path, "gathering", "big one", language="en")
    assert len(block) <= MAX_BLOCK_CHARS
    assert "transcript wins" in block


def test_round_card_extracts_sections(tmp_path):
    md = tmp_path / "out.md"
    md.write_text(
        "# Notes\n\n## Decisions\n- ship it\n\n## Irrelevant\n- noise\n\n## Next meeting\n- next book\n",
        encoding="utf-8",
    )
    card = build_card("gathering", "scifi w1", md)
    assert "- ship it" in card and "- next book" in card and "noise" not in card


def test_save_card_appends_uniquely(tmp_path):
    make_pack(tmp_path, "club", {"type": "gathering", "title_contains": ["scifi"]})
    md = tmp_path / "out.md"
    md.write_text("# Notes\n", encoding="utf-8")
    save_card(tmp_path, "gathering", "scifi w1", md)
    save_card(tmp_path, "gathering", "scifi w1", md)
    cards = list((tmp_path / "club" / "_history").glob("*.md"))
    assert len(cards) == 2  # second save gets a _2 suffix, never overwrites


def test_save_card_unmatched_is_noop(tmp_path):
    md = tmp_path / "out.md"
    md.write_text("# Notes\n", encoding="utf-8")
    save_card(tmp_path, "gathering", "unmatched", md)
    assert not any(p.is_dir() and (p / "_history").exists() for p in tmp_path.iterdir())
