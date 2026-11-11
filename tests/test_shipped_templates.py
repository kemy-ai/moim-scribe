"""Contracts that hold for every template this package ships.

The lenses and styles here are backported from a private codebase whose
comments and examples name real people, real companies, and the dates of real
incidents. Those are useful there and must never arrive here, so the guard is a
test rather than a habit.
"""
import hashlib
import re
import unicodedata
from pathlib import Path

import moim_scribe
from moim_scribe.config import SUMMARY_LENS

TEMPLATES = Path(moim_scribe.__file__).parent / "templates"
LENSES = TEMPLATES / "lenses"

# Digests, not the names themselves. This file is public, and a list titled
# "names that must never appear" discloses precisely who they are — the guard
# would leak what it exists to contain. The plaintext master lives outside the
# repo in .local/private-names.txt (gitignored); regenerate this block with
#     python tests/test_shipped_templates.py
# Fictional example names (김하늘, 이바다 …) are fine and expected.
_SALT = "moim-scribe/private-names/v1"
_NAME_LENGTHS = frozenset([2, 3, 4, 5, 9])
PRIVATE_NAME_DIGESTS = frozenset({
    '493e82928b6e', '5a013d83145d', '601ca5f94718', '6f4d0a0ce39c',
    '795fc53caea9', '82e40a4a695d', '99bc447f6c27', 'a7a27ff272c4',
    'bf87a79c0c8f', 'd59919befacb', 'e2b356d9fd4e', 'e742f8643e99',
    'e91eb8ab4354', 'fb0140b34e1d',
})
REAL_DATE = re.compile(r"20\d\d-\d\d-\d\d")


def _normalize(text: str) -> str:
    """Fold away what differs between a name and a mention of it.

    Some upstream names carry an internal comma or space, and filenames arrive
    NFD where source is NFC; letter case varies too. Normalizing both sides
    lets one digest cover every spelling of a name — which is why there are
    fewer digests than lines in the master list.
    """
    return re.sub(r"[\s,./·_-]", "", unicodedata.normalize("NFC", text)).lower()


def _digest(fragment: str) -> str:
    return hashlib.sha256((_SALT + fragment).encode("utf-8")).hexdigest()[:12]


def _shipped_md():
    return sorted(TEMPLATES.rglob("*.md"))


def test_no_private_names_anywhere_in_templates():
    """Substring search, done blind: hash every fragment of the shipped text
    that is as long as some forbidden name, and look for a collision."""
    for path in _shipped_md():
        flat = _normalize(path.read_text(encoding="utf-8"))
        for n in _NAME_LENGTHS:
            for i in range(len(flat) - n + 1):
                assert _digest(flat[i:i + n]) not in PRIVATE_NAME_DIGESTS, (
                    f"{path.relative_to(TEMPLATES)} contains a private name "
                    f"at offset {i} (length {n}) — see .local/private-names.txt"
                )


def test_lenses_carry_no_real_incident_dates():
    """Lenses state rules; the dated incidents behind them belong upstream.

    Example packs may use invented dates, so this is scoped to lenses.
    """
    for path in LENSES.glob("*.md"):
        found = REAL_DATE.findall(path.read_text(encoding="utf-8"))
        assert not found, f"{path.name} contains date(s) {found}"


def test_every_lens_ships_in_both_languages():
    stems = {p.name.rsplit(".", 2)[0] for p in LENSES.glob("*.md")}
    assert stems  # the glob must actually be finding files
    for stem in stems:
        for lang in ("ko", "en"):
            assert (LENSES / f"{stem}.{lang}.md").is_file(), f"{stem} missing .{lang}"


def test_exactly_one_summary_lens_ships():
    """One lens for every recording. A second deliverable lens appearing here
    means type selection is creeping back in without anything to select it."""
    stems = {p.name.rsplit(".", 2)[0] for p in LENSES.glob("*.md")}
    assert stems == {"_common", SUMMARY_LENS}
    assert not (TEMPLATES / "styles").exists()


def test_summary_lens_does_not_ask_for_a_classification():
    """The lens has to cope with a meeting and a catch-up alike, by dropping
    sections that have nothing in them, not by picking a shape first."""
    ko = (LENSES / f"{SUMMARY_LENS}.ko.md").read_text(encoding="utf-8")
    en = (LENSES / f"{SUMMARY_LENS}.en.md").read_text(encoding="utf-8")
    assert "분류하지 않는다" in ko and "생략" in ko
    assert "Do not classify" in en and "Leave out" in en
    assert "분량은 녹음 길이에 비례한다" in ko
    assert "Length scales with the recording" in en


def test_summary_lens_forbids_guessed_attribution():
    ko = (LENSES / f"{SUMMARY_LENS}.ko.md").read_text(encoding="utf-8")
    en = (LENSES / f"{SUMMARY_LENS}.en.md").read_text(encoding="utf-8")
    assert "SPEAKER_NN" in ko and "추측해 옮기지 않는다" in ko
    assert "SPEAKER_NN" in en and "by guesswork" in en


def test_summary_lens_says_which_language_to_write_in():
    """Found in an end-to-end run: an English workspace got a Korean summary,
    because the summarizer's own environment preferred Korean and nothing in
    the lens said otherwise."""
    ko = (LENSES / f"{SUMMARY_LENS}.ko.md").read_text(encoding="utf-8")
    en = (LENSES / f"{SUMMARY_LENS}.en.md").read_text(encoding="utf-8")
    assert "전사본의 언어로 쓴다" in ko
    assert "in the language the transcript is in" in en


def test_summary_lens_does_not_state_an_estimated_label_count_as_attendees():
    """Measured on a real recording: three people, no count given, seven
    labels, and the summary said "7 attendees". The lens now ties the attendee
    count to the filename note and hedges otherwise."""
    ko = (LENSES / f"{SUMMARY_LENS}.ko.md").read_text(encoding="utf-8")
    en = (LENSES / f"{SUMMARY_LENS}.en.md").read_text(encoding="utf-8")
    assert "파일명 지정 참석 인원" in ko and "화자 라벨 N개" in ko
    assert "attendee count fixed by filename" in en and "N speaker labels" in en
    # the phrases the lens keys on must be the ones the pipeline really prints
    from moim_scribe.pipeline import PROMPT_HEADERS

    assert "파일명 지정 참석 인원" in PROMPT_HEADERS["ko"]["spk_fixed"]
    assert "attendee count fixed by filename" in PROMPT_HEADERS["en"]["spk_fixed"]


def test_round_card_headers_exist_letter_for_letter_in_the_lens():
    """🔴 Round cards are harvested by header. If the lens renames a section and
    packs.SECTION_HEADERS does not follow, every card silently comes out
    header-only and the history a pack accumulates is empty."""
    from moim_scribe.packs import SECTION_HEADERS

    bodies = "\n".join(
        (LENSES / f"{SUMMARY_LENS}.{lang}.md").read_text(encoding="utf-8") for lang in ("ko", "en")
    )
    lines = {ln.strip() for ln in bodies.splitlines()}
    assert SECTION_HEADERS  # an empty list would pass the loop below vacuously
    for header in SECTION_HEADERS:
        assert header in lines, f"{header!r} is harvested but the lens never asks for it"


def test_example_pack_matches_by_title_alone():
    import json

    for match in (TEMPLATES / "examples").glob("*/match.json"):
        spec = json.loads(match.read_text(encoding="utf-8"))
        assert set(spec) == {"title_contains"}, f"{match.parent.name}: {sorted(spec)}"


def test_common_lens_bans_checkboxes_and_tildes_in_both_languages():
    """Renderers turn `[x]` and paired `~` into strikethrough, silently."""
    for lang in ("ko", "en"):
        body = (LENSES / f"_common.{lang}.md").read_text(encoding="utf-8")
        assert "[x]" in body  # mentioned in order to forbid it
        assert "~" in body


if __name__ == "__main__":  # regenerate the digest block from the local master
    master = Path(__file__).resolve().parents[1] / ".local" / "private-names.txt"
    raw = [ln.strip() for ln in master.read_text(encoding="utf-8").splitlines()]
    names = sorted({_normalize(ln) for ln in raw if ln and not ln.startswith("#")})
    digests = sorted(_digest(n) for n in names)
    print(f"_NAME_LENGTHS = frozenset({sorted({len(n) for n in names})})")
    print("PRIVATE_NAME_DIGESTS = frozenset({")
    for i in range(0, len(digests), 4):
        print("    " + " ".join(f"{d!r}," for d in digests[i:i + 4]))
    print("})")
