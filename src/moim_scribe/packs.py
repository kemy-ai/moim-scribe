"""Context packs: optional per-series background knowledge and round history.

Every recording is summarized with the same lens. A pack adds what the model
cannot hear: who the regulars are, what the group's jargon means, what was
left open last time. Packs are optional: with none, the prompt is exactly
the plain one.

A pack directory (packs/<name>/):
  match.json   {"title_contains": ["book club"]}
  pack.md      human-curated background (roster, running context)
  session/     optional extra material (latest one .md auto-loaded)
  _history/    auto-appended round cards (curate back into pack.md by hand;
               the pipeline only ever appends here, never edits pack.md)
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import UTC, date, datetime
from pathlib import Path

logger = logging.getLogger("moim_scribe.packs")


def _today_local() -> date:
    """The user's local calendar date, timezone-explicit. A card saved at 1am
    KST must carry that local date, not the previous day's UTC date."""
    return datetime.now(UTC).astimezone().date()

MAX_BLOCK_CHARS = 8000
TRUNCATE_TAIL = {"ko": "\n…(배경 일부 생략)", "en": "\n…(background truncated)"}

RULES_BLOCK = {
    "ko": (
        "※ 배경지식 사용 규칙:\n"
        "- 위 배경지식은 사전 자료다. 전사본에 실제로 나온 내용과 반드시 구분하라.\n"
        '- 배경지식만 근거로 "누가 말했다/결정했다"라고 쓰지 마라. 귀속·인용은 전사본에 근거가 있을 때만.\n'
        "- 용도: (a) 화자 실명·호칭 확인 (b) 약어·용어·인물 관계 이해 (c) 이전 회차와의 연속성 파악.\n"
        "- 배경지식과 전사가 충돌하면 전사본이 우선이다."
    ),
    "en": (
        "Rules for using this background:\n"
        "- It is pre-existing reference material — keep it strictly separate from what was actually said.\n"
        "- Never attribute statements or decisions based on background alone; quotes and attribution "
        "need evidence in the transcript.\n"
        "- Use it to (a) confirm names and titles, (b) resolve jargon and relationships, "
        "(c) connect to previous rounds.\n"
        "- Where background and transcript conflict, the transcript wins."
    ),
}

HEADER = {
    "ko": "=== 배경지식 (사전 축적 참고 자료 — 이번 녹음에서 나온 말이 아님) ===",
    "en": "=== Background (accumulated reference — NOT from this recording) ===",
}

# Section headers harvested into round cards (whichever exist in the output).
# The harvest is by header, so these must stay letter-for-letter equal to the
# carry-forward sections of templates/lenses/summary.*.md (a test holds the
# two together). A summary without any of them yields a header-only card.
SECTION_HEADERS = [
    "## 결정사항", "## 할 일", "## 보류·다음에 이어갈 것",
    "## Decisions", "## Action items", "## Open items",
]


def _norm(s: str) -> str:
    """NFC + casefold. Phone-uploaded filenames are often NFD; this absorbs
    both that and Latin case differences during matching."""
    return unicodedata.normalize("NFC", s or "").casefold()


def find_matching_pack(packs_dir: Path, title: str) -> Path | None:
    """Find the pack whose match.json matches the recording title.

    Match rule: any title_contains keyword being a substring of the
    normalized title. Among multiple matches the longest matched keyword wins
    (more specific beats more generic). A corrupt match.json only skips that
    one pack. Keys other than title_contains (the "type" and "style" of a
    pre-0.2 pack) are ignored: the keyword alone decides.
    """
    ntitle = _norm(title)
    best_dir: Path | None = None
    best_kw = ""
    for match_path in sorted(packs_dir.glob("*/match.json")):
        try:
            spec = json.loads(match_path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001 — one bad pack must not break matching
            logger.warning(f"match.json unreadable — skipped: {match_path} ({type(e).__name__}: {e})")
            continue
        if not isinstance(spec, dict):
            continue
        keywords = spec.get("title_contains", [])
        if isinstance(keywords, str):
            keywords = [keywords]  # a bare string means one keyword
        if not isinstance(keywords, list):
            continue
        for kw in keywords:
            if not isinstance(kw, str) or not kw:
                continue
            if _norm(kw) in ntitle and len(kw) > len(best_kw):
                best_dir, best_kw = match_path.parent, kw
    if best_dir:
        logger.info(f"matched pack: {best_dir.name} (keyword '{best_kw}')")
    return best_dir


def _latest_session_file(pack_dir: Path) -> Path | None:
    session_dir = pack_dir / "session"
    if not session_dir.is_dir():
        return None
    files = sorted((p for p in session_dir.glob("*.md") if p.is_file()), reverse=True)
    return files[0] if files else None


def _read_text_safe(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception as e:  # noqa: BLE001 — one broken file must not break the block
        logger.warning(f"unreadable — skipped: {path} ({type(e).__name__}: {e})")
        return ""


def build_block(packs_dir: Path, title: str, language: str = "ko") -> str:
    """Assemble the background block injected into the summary prompt.

    Order: pack.md + latest session file + usage rules. Empty string when
    nothing matches — the caller then injects nothing and the prompt is
    exactly the plain one. Bodies over MAX_BLOCK_CHARS are truncated from the
    end; the rules block always survives.
    """
    pack_dir = find_matching_pack(packs_dir, title)
    if not pack_dir:
        return ""

    lang = language if language in RULES_BLOCK else "en"
    parts = []
    pack_md = pack_dir / "pack.md"
    if pack_md.is_file():
        content = _read_text_safe(pack_md)
        if content:
            parts.append(content)
    latest_session = _latest_session_file(pack_dir)
    if latest_session:
        content = _read_text_safe(latest_session)
        if content:
            parts.append(content)

    if not parts:
        logger.info(f"pack '{pack_dir.name}' has no loadable content — block skipped")
        return ""

    header = HEADER[lang]
    rules = RULES_BLOCK[lang]
    tail = TRUNCATE_TAIL[lang]
    body = "\n\n".join(parts)
    full = f"{header}\n{body}\n\n{rules}"
    if len(full) <= MAX_BLOCK_CHARS:
        return full

    reserved = len(rules) + len(tail) + 2
    budget = max(MAX_BLOCK_CHARS - reserved, len(header) + 1)
    head_part = f"{header}\n{body}"[:budget].rstrip()
    logger.info(f"background {len(full)} chars over {MAX_BLOCK_CHARS} cap — truncated")
    return f"{head_part}{tail}\n\n{rules}"


def _safe_title(title: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", title or "").strip()
    return cleaned or "untitled"


def _unique_history_path(history_dir: Path, stem: str) -> Path:
    candidate = history_dir / f"{stem}.md"
    n = 2
    while candidate.exists():
        candidate = history_dir / f"{stem}_{n}.md"
        n += 1
    return candidate


def _extract_sections(text: str, headers: list[str] = SECTION_HEADERS) -> list[str]:
    """Extract each listed '## header' section up to the next '## ' heading."""
    heading_starts = [m.start() for m in re.finditer(r"(?m)^## .*$", text)]
    sections = []
    for header in headers:
        m = re.search(rf"(?m)^{re.escape(header)}\s*$", text)
        if not m:
            continue
        start = m.start()
        end = next((pos for pos in heading_starts if pos > start), len(text))
        section = text[start:end].rstrip()
        if section:
            sections.append(section)
    return sections


def build_card(title: str, out_md_path: str | Path) -> str:
    """Round-card body: header (date, title, result path) + harvested sections."""
    abs_path = str(Path(out_md_path).resolve())
    header = (
        f"# Round card — {title} ({_today_local().isoformat()})\n\n"
        f"- result: {abs_path}"
    )
    try:
        text = Path(out_md_path).read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"result unreadable — header-only card: {out_md_path} ({type(e).__name__}: {e})")
        return header + "\n"

    sections = _extract_sections(text)
    if not sections:
        return header + "\n"
    return header + "\n\n" + "\n\n".join(sections) + "\n"


def save_card(packs_dir: Path, title: str, out_md_path: str | Path) -> None:
    """Append a round card to the matched pack's _history/ (no-op when unmatched).

    Cards only accumulate; pack.md is never edited automatically — curation
    back into pack.md is a deliberate human step.
    """
    pack_dir = find_matching_pack(packs_dir, title)
    if not pack_dir:
        return
    try:
        history_dir = pack_dir / "_history"
        history_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{_today_local().isoformat()}_{_safe_title(title)}"
        dest = _unique_history_path(history_dir, stem)
        dest.write_text(build_card(title, out_md_path), encoding="utf-8")
        logger.info(f"round card saved: {dest}")
    except Exception as e:  # noqa: BLE001 — history is auxiliary, never fatal
        logger.warning(f"round card save failed (ignored): {type(e).__name__}: {e}")
