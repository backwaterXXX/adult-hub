"""Pure wikitext parsing helpers for Porn Base Central performer data."""
from __future__ import annotations

import re
import unicodedata
from typing import Dict, List


_REF_RE = re.compile(r"<ref\b[^>]*>.*?</ref\s*>", re.IGNORECASE | re.DOTALL)
_REF_SELF_RE = re.compile(r"<ref\b[^>]*/\s*>", re.IGNORECASE)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_TEMPLATE_RE = re.compile(r"\{\{[^{}]*\}\}")
_LINK_RE = re.compile(r"\[\[([^|\]]+)\|([^\]]+)\]\]")
_LINK_SIMPLE_RE = re.compile(r"\[\[([^\]]+)\]\]")
_TAG_RE = re.compile(r"<[^>]+>")
_FILE_RE = re.compile(r"\bFile:[^\s|\]]+\.(?:jpg|jpeg|png|gif|webp|svg)", re.IGNORECASE)
_BOLD_ITALIC_RE = re.compile(r"'''''|'''|''")
_PERFORMER_ROLE_RE = re.compile(
    r"\b(?:actor|performer|model|escort|porn\s+star|adult\s+film)\b",
    re.IGNORECASE,
)


def _identity_key(value: str) -> str:
    folded = unicodedata.normalize("NFKD", str(value or "").casefold())
    return "".join(
        char for char in folded
        if not unicodedata.category(char).startswith("M") and char.isalnum()
    )


def profile_matches_name(name: str, parsed: Dict[str, object]) -> bool:
    """Accept PBC metadata only when the page identifies the requested person.

    Search results and redirects can point at unrelated pages for generic
    performer strings. Require both a performer-like bio and an identity match:
    the requested name must be an exact parsed alias or lead the biography.
    """
    requested = _identity_key(name)
    if not requested or not isinstance(parsed, dict):
        return False
    bio = str(parsed.get("bio") or "").strip()
    aliases = parsed.get("aliases") or []
    if not isinstance(aliases, (list, tuple)):
        aliases = []
    identity_matches = any(_identity_key(alias) == requested for alias in aliases)
    if not identity_matches:
        identity_matches = bool(re.match(
            r"^\s*" + re.escape(str(name).strip()) + r"(?=$|[^\w])",
            bio,
            re.IGNORECASE | re.UNICODE,
        ))
    if not identity_matches:
        return False
    return bool(_PERFORMER_ROLE_RE.search(bio) or str(parsed.get("birth") or "").strip())


def _strip_templates(text: str) -> str:
    prev = None
    out = text
    for _ in range(10):  # bounded for pathological nesting
        if out == prev:
            break
        prev = out
        out = _TEMPLATE_RE.sub(" ", out)
    return out


def _clean_inline(text: str) -> str:
    """Wikitext/html inline markup -> plain text, whitespace collapsed."""
    if not isinstance(text, str):
        return ""
    out = _REF_RE.sub(" ", text)
    out = _REF_SELF_RE.sub(" ", out)
    out = _COMMENT_RE.sub(" ", out)
    out = _strip_templates(out)
    out = _LINK_RE.sub(r"\2", out)
    out = _LINK_SIMPLE_RE.sub(r"\1", out)
    out = _FILE_RE.sub(" ", out)
    out = _BOLD_ITALIC_RE.sub("", out)
    out = _TAG_RE.sub(" ", out)
    out = re.sub(r"\s+", " ", out).strip()
    return out


def _split_aliases(value: str) -> List[str]:
    """Split an infobox/a.k.a. alias blob on commas/semicolons, cleaned."""
    _JUNK = {"n/a", "na", "-", "--", "?", "??", "none", "unknown", "tbd", "tba", "nil", "null"}
    if not isinstance(value, str) or not value.strip():
        return []
    cleaned = _clean_inline(value)
    # Also strip stray quotes around each alias.
    parts = re.split(r"[,;]", cleaned)
    out: List[str] = []
    seen = set()
    for part in parts:
        # Split "A and B" only when it looks like a join of short names?
        # Keep it simple: the spec mandates comma/semicolon splitting,
        # but " and " is common in a.k.a. blobs, so handle it there
        # (callers may pass pre-split values; harmless here).
        candidate = part.strip().strip("\"'")
        if candidate and candidate not in ("",):
            key = candidate.lower()
            if key and key not in seen and key not in _JUNK and len(candidate) >= 2:
                seen.add(key)
                out.append(candidate)
    # Expand any remaining " and " joins (e.g. "Foo and Bar").
    expanded: List[str] = []
    seen2 = set()
    for alias in out:
        chunks = re.split(r"\s+and\s+|\s+&\s+", alias)
        if len(chunks) > 1 and all(c.strip() for c in chunks):
            for chunk in chunks:
                c = chunk.strip().strip("\"'")
                if c and c.lower() not in seen2 and c.lower() not in _JUNK and len(c) >= 2:
                    seen2.add(c.lower())
                    expanded.append(c)
        else:
            if alias.lower() not in seen2:
                seen2.add(alias.lower())
                expanded.append(alias)
    return expanded


def _infobox_field(wikitext: str, field: str) -> str:
    """Return the raw value of an infobox line like ``| Aliases: | Foo``."""
    if not isinstance(wikitext, str):
        return ""
    # "| Aliases = ...", "| Aliases: | ...", "Aliases: | ..." all accepted.
    pattern = re.compile(
        r"(?im)^\s*\|?\s*" + re.escape(field) + r"\s*[:=|]\s*(.+?)\s*$"
    )
    match = pattern.search(wikitext)
    if not match:
        return ""
    value = match.group(1).strip()
    # "| Aliases: | Foo, Bar" -> drop the second pipe.
    value = value.lstrip("|").strip()
    return value


def _flush_paragraph(blocks: List[str], current: List[str]) -> None:
    if current:
        blocks.append(" ".join(current).strip())
        current.clear()


def _is_paragraph_break(stripped: str) -> bool:
    """True for lines that end a prose paragraph (templates, tables, galleries, captions)."""
    if stripped.startswith(("{", "}", "|", "!", "=", "__", "<!--", "{|",
                            "<gallery", "File:", "Image:", "[[")):
        return True
    if stripped.endswith("=") and len(stripped) < 80:
        return True  # etykieta galerii w stylu "Hot House="
    if re.match(r"^\{\|", stripped):
        return True
    if re.match(r"^\d+px\|", stripped) or "thumb|" in stripped:
        return True  # fragment podpisu miniaturki (ciag dalszy [[File:...]])
    return False


def _paragraph_blocks(wikitext: str) -> List[str]:
    blocks: List[str] = []
    current: List[str] = []
    for line in wikitext.splitlines():
        stripped = line.strip()
        if not stripped or _is_paragraph_break(stripped):
            _flush_paragraph(blocks, current)
            continue
        current.append(stripped)
    _flush_paragraph(blocks, current)
    return blocks


def _has_letters(s: str) -> bool:
    return bool(re.search(r"[^\W_]", s, re.UNICODE))


def _select_paragraph_candidates(blocks: List[str]) -> List[str]:
    candidates = [b for b in blocks
                  if _has_letters(b) and "File:" not in b and "<gallery" not in b
                  and "Category:" not in b and len(b) >= 40]
    if not candidates:
        candidates = [b for b in blocks if _has_letters(b)]
    return candidates


def _score_paragraph(cand: str) -> int:
    score = 0
    low = cand.lower()
    if cand.lstrip().startswith("'''"):
        score += 3
    if re.search(r"\bborn\s+\d{1,2}\s+[a-z]+\s+\d{4}", low):
        score += 2
    if re.search(r"\bis\s+an?\s+.*?(actor|model|director|performer|porn\s*star)\b", low):
        score += 2
    if ". " in cand:
        score += 1
    if len(cand) > 200:
        score += 1
    return score


def _first_paragraph_raw(wikitext: str) -> str:
    """First PROSE paragraph (bio intro): scoring kandydatow.

    Pomija infoboxy/szablony/tabele/galerie (`File:`/`Image:`/`<gallery`/naglowki
    `=`/etykiety galerii konczace sie na `=`). Z kandydatow wygrywa ten,
    ktory najbardziej przypomina wstep biogramu (bold na starcie, "born ...",
    "is a(n) ... actor/model", tresc zdaniowa, istotna dlugosc).
    """
    if not isinstance(wikitext, str):
        return ""
    candidates = _select_paragraph_candidates(_paragraph_blocks(wikitext))
    if not candidates:
        return ""

    best = max(candidates, key=lambda c: (_score_paragraph(c), len(c)))
    if _score_paragraph(best) == 0 and len(best) < 40:
        return ""
    return best



_AKA_RE = re.compile(
    r"a\.k\.a\.\s*([^;()]+)", re.IGNORECASE
)


def _aka_aliases(paragraph_raw: str) -> List[str]:
    out: List[str] = []
    if not isinstance(paragraph_raw, str):
        return out
    for match in _AKA_RE.finditer(paragraph_raw):
        blob = match.group(1).strip()
        # Cut trailing "born ..." tail common in "(a.k.a. Y; born ...)".
        blob = re.split(r"\bborn\b", blob, flags=re.IGNORECASE)[0].strip()
        blob = blob.rstrip(",; ")
        if blob:
            out.extend(_split_aliases(blob))
    # Dedup, preserve order.
    seen = set()
    deduped: List[str] = []
    for alias in out:
        key = alias.lower()
        if alias and key not in seen:
            seen.add(key)
            deduped.append(alias)
    return deduped


def _extend_unique(aliases: List[str], seen: set, values: List[str]) -> None:
    for value in values:
        key = value.lower()
        if value and key not in seen:
            seen.add(key)
            aliases.append(value)


def _collect_aliases(wikitext: str) -> List[str]:
    aliases: List[str] = []
    seen: set = set()
    _extend_unique(aliases, seen, _split_aliases(_infobox_field(wikitext, "Aliases")))
    _extend_unique(aliases, seen, _aka_aliases(_first_paragraph_raw(wikitext)))
    # Biobox career lists: {{...|alias= Foo; Bar |...}} (semicolon/comma separated).
    try:
        for _m in re.finditer(r"\|alias\s*=\s*([^|\n}]+)", wikitext):
            _extend_unique(aliases, seen, _split_aliases(_m.group(1)))
    except re.error:
        pass
    return aliases


def _birth_from_born_phrase(wikitext: str) -> str:
    try:
        _bm = re.search(
            r"\bborn\s+(\d{1,2}\s+[A-Za-z]+\s+\d{4})",
            _first_paragraph_raw(wikitext))
        if not _bm and isinstance(wikitext, str):
            _bm = re.search(
                r"\bborn\s+(\d{1,2}\s+[A-Za-z]+\s+\d{4})", wikitext)
        if _bm:
            return _bm.group(1)[:120]
    except re.error:
        pass
    return ""


def _parse_birth(wikitext: str) -> str:
    # --- birth: "Birth:" infobox line albo "born D Month YYYY" we wstepie ---
    birth_raw = _infobox_field(wikitext, "Birth")
    birth = ""
    if birth_raw:
        birth = re.split(r"[\(;]", birth_raw, maxsplit=1)[0]
        birth = _clean_inline(birth)[:120]
    if not birth:
        birth = _birth_from_born_phrase(wikitext)
    return birth


def parse_person(wikitext) -> Dict[str, object]:
    """Parse wikitext into ``{"aliases": [...], "bio": "...", "birth": "..."}``.

    Pure function; never raises on ugly input. Canonical-name filtering is
    left to the caller (see :func:`backfill_pbc`).
    """
    empty: Dict[str, object] = {"aliases": [], "bio": "", "birth": ""}
    if not isinstance(wikitext, str) or not wikitext.strip():
        return dict(empty)

    # --- aliases: infobox + a.k.a. ---
    aliases = _collect_aliases(wikitext)

    # --- bio: first paragraph, plain text, max 2000 chars ---
    bio = _clean_inline(_first_paragraph_raw(wikitext))[:2000]

    birth = _parse_birth(wikitext)

    return {"aliases": aliases, "bio": bio, "birth": birth}

