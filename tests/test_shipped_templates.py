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


def test_gathering_lens_offers_both_shapes_in_both_languages():
    """The open-ended shape matters: a catch-up gathering read as minutes loses the point."""
    ko = (LENSES / "gathering.ko.md").read_text(encoding="utf-8")
    en = (LENSES / "gathering.en.md").read_text(encoding="utf-8")
    assert "구조 A" in ko and "구조 B" in ko
    assert "Structure A" in en and "Structure B" in en
    assert "분량은 녹음 길이에 비례한다" in ko
    assert "Length scales with the recording" in en


def test_per_person_shapes_forbid_guessed_attribution():
    """Card-per-person formats force a who-said-this call — the risky part."""
    ko = (LENSES / "gathering.ko.md").read_text(encoding="utf-8")
    en = (LENSES / "gathering.en.md").read_text(encoding="utf-8")
    assert "SPEAKER_NN" in ko and "추측으로 넣지 않는다" in ko
    assert "SPEAKER_NN" in en and "by guessing" in en


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
