"""The conversation example's shipping contract.

Three things about this example are load-bearing and none of them is obvious
from reading a single file, so they are pinned here:

  1. the lens lives under templates/lenses/, so `init` drops it straight into
     packs/lenses/ where _load_lens looks — if it shipped inside the example
     pack directory instead, the first run would die with FileNotFoundError
  2. it is NOT in DEFAULT_TYPES: present but inactive until the user opts in
  3. its carry-forward section is harvested into round cards, which is what
     makes the "what came up again" section eventually have material
"""
from pathlib import Path

import moim_scribe
from moim_scribe.config import DEFAULT_TYPES
from moim_scribe.packs import SECTION_HEADERS, build_card

TEMPLATES = Path(moim_scribe.__file__).parent / "templates"
EXAMPLE = TEMPLATES / "examples" / "conversation"
CARRY_FORWARD = ("## 합의된 것 · 열린 채로 남은 것", "## Agreed and left open")


def test_lens_ships_in_lenses_dir_not_in_the_example_pack():
    """init copies templates/lenses/* into packs/lenses/ — the path _load_lens reads."""
    for lang in ("ko", "en"):
        assert (TEMPLATES / "lenses" / f"conversation.{lang}.md").is_file()
    assert not list(EXAMPLE.glob("conversation.*.md"))


def test_every_shipped_lens_has_both_languages():
    stems = {p.name.rsplit(".", 2)[0] for p in (TEMPLATES / "lenses").glob("*.md")}
    for stem in stems:
        for lang in ("ko", "en"):
            assert (TEMPLATES / "lenses" / f"{stem}.{lang}.md").is_file(), f"{stem} missing .{lang}"


def test_conversation_is_not_a_default_type():
    """Shipped but inactive: turning it on is an explicit config.yaml edit."""
    assert "conversation" not in DEFAULT_TYPES


def test_example_pack_is_complete_and_typed():
    import json

    assert (EXAMPLE / "README.md").is_file()
    assert (EXAMPLE / "pack.md").is_file()
    spec = json.loads((EXAMPLE / "match.json").read_text(encoding="utf-8"))
    assert spec["type"] == "conversation"
    assert spec["title_contains"]


def test_readme_opens_with_a_consent_notice():
    """The warning has to be above the pitch, not buried under it."""
    head = (EXAMPLE / "README.md").read_text(encoding="utf-8")[:700]
    assert "동의" in head and "Consent" in head
    assert "docs/privacy.md" in head


def test_readme_states_what_the_lens_will_not_do():
    body = (EXAMPLE / "README.md").read_text(encoding="utf-8")
    assert "진단" in body  # never diagnoses the relationship
    assert "none" in body and "ollama" in body  # summarizer choice is spelled out


def test_lens_forbids_asserting_tone_and_guessed_attribution():
    """Two rules keep this an observation tool; losing them changes what it is."""
    ko = (TEMPLATES / "lenses" / "conversation.ko.md").read_text(encoding="utf-8")
    en = (TEMPLATES / "lenses" / "conversation.en.md").read_text(encoding="utf-8")
    assert "감정과 말투를 단정하지 않는다" in ko
    assert "SPEAKER_NN" in ko and "추측으로 귀속하지 않는다" in ko
    assert "Never assert emotion or tone" in en
    assert "SPEAKER_NN" in en and "by guessing" in en


def test_lens_gates_the_repeat_section_on_real_history():
    """Without earlier rounds in the prompt the section must be dropped, not invented."""
    ko = (TEMPLATES / "lenses" / "conversation.ko.md").read_text(encoding="utf-8")
    en = (TEMPLATES / "lenses" / "conversation.en.md").read_text(encoding="utf-8")
    assert "이전 회차 기록이 실제로 들어 있을 때만" in ko
    assert "only if the background block above actually contains earlier rounds" in en


def test_carry_forward_sections_are_harvested():
    assert all(h in SECTION_HEADERS for h in CARRY_FORWARD)


def test_round_card_picks_up_the_conversation_section(tmp_path):
    md = tmp_path / "out.md"
    md.write_text(
        "# 대화 기록 — 2026-05-28\n\n"
        "## 표현 기록\n- 이름: \"…\"\n\n"
        "## 합의된 것 · 열린 채로 남은 것\n- 합의: 요일제로 하기로\n- 열림: 시작 시점\n",
        encoding="utf-8",
    )
    card = build_card("conversation", "대화 2명", md)
    assert "요일제로 하기로" in card
    assert "열림: 시작 시점" in card
    assert "표현 기록" not in card  # per-speaker quotes don't belong in a carry-forward card
