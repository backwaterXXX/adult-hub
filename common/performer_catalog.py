"""Local performer catalogue and deterministic Telegram title matching."""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from typing import Iterable

from common.performers import (
    STUDIO_NAMES,
    STUDIO_SUFFIXES,
    SINGLE_WORD_STOPWORDS,
    TITLE_STOPWORDS,
    is_non_performer_name,
    is_valid_performer_name,
    strip_nationality_prefix,
)


_NON_PERFORMER_LABEL_RE = re.compile(
    r"^\s*(?:produtor(?:a|as|es)?|productor(?:a|as|es)?|producer|produced\s+by|"
    r"studio|est[uú]dio|label|selo)\s*[:\-–—]",
    re.IGNORECASE | re.UNICODE,
)
_NON_NAME_CREDIT_PREFIXES = {"men", "women", "male", "female"}

_LEADING_TAG_RE = re.compile(r"^[\[(]?[^\])]*[\])]\s*[-–—:]?\s*")
_LEADING_AGGREGATOR_RE = re.compile(
    r"^(?:(?:only\s*fans|just\s*for\s*fans|onlyfans)\s*[-–—:]?\s*|OF\s*[-–—:]?\s*|JFF\s*[-–—:]?\s*)",
    re.IGNORECASE,
)
_TRAILING_AGGREGATOR_RE = re.compile(
    r"\s+[-–—:]?\s*(?:only\s*fans|just\s*for\s*fans|onlyfans|\bOF\b|\bJFF\b)$",
    re.IGNORECASE,
)
_STUDIO_ACRONYM_PREFIX_RE = re.compile(
    r"^(?:LE|RFC|RFCC|TT|EV|FKM|FsM|MOL|LB|VOYR|WhyNotBi|RawStrokes|DreamBoyBondage|MeninosOnline)\s*[-–—:\s]\s*",
    re.IGNORECASE,
)
_STUDIO_PREFIX_PATTERNS = [
    re.compile(rf"^{re.escape(s)}\s*[-–—:\s]\s*", re.IGNORECASE)
    for s in sorted(STUDIO_NAMES, key=len, reverse=True)
]
_STUDIO_SUFFIX_PATTERNS = [
    re.compile(rf"\s*[-–—:\s]\s*{re.escape(s)}$", re.IGNORECASE)
    for s in sorted(STUDIO_NAMES, key=len, reverse=True)
]


def clean_credit_candidate(value: str) -> str:
    """Strip bracketed tags, aggregator markers, studio prefixes and demonyms from candidate."""
    curr = value.strip(" \t\r\n-–—:[]()")
    if not curr:
        return ""
    if "\n" in curr:
        curr = _choose_credit_line(curr)
    for _ in range(5):
        prev = curr
        curr = _strip_credit_affixes(curr)
        curr = _strip_first_matching(curr, _STUDIO_PREFIX_PATTERNS)
        curr = _strip_first_matching(curr, _STUDIO_SUFFIX_PATTERNS)
        bare = strip_nationality_prefix(curr).strip(" \t\r\n-–—:[]()")
        if bare:
            curr = bare
        if curr == prev:
            break
    words = curr.split()
    if len(words) >= 3 and words[0].casefold() in _NON_NAME_CREDIT_PREFIXES:
        curr = " ".join(words[1:]).strip(" \t\r\n-–—:[]()")
    return curr


def _choose_credit_line(curr: str) -> str:
    """Pick the last line of a multi-line credit that cleans to a valid performer name."""
    lines = [line.strip(" \t\r\n-–—:[]()") for line in curr.splitlines() if line.strip(" \t\r\n-–—:[]()")]
    if len(lines) > 1:
        for cand_line in reversed(lines):
            c = clean_credit_candidate(cand_line)
            if is_valid_performer_name(c):
                return cand_line
        return lines[-1]
    return curr


def _strip_credit_affixes(curr: str) -> str:
    """Apply the tag, aggregator and studio-acronym removals once, in their fixed order."""
    curr = _LEADING_TAG_RE.sub("", curr).strip(" \t\r\n-–—:[]()")
    curr = _LEADING_AGGREGATOR_RE.sub("", curr).strip(" \t\r\n-–—:[]()")
    curr = _TRAILING_AGGREGATOR_RE.sub("", curr).strip(" \t\r\n-–—:[]()")
    curr = _STUDIO_ACRONYM_PREFIX_RE.sub("", curr).strip(" \t\r\n-–—:[]()")
    return curr


def _strip_first_matching(curr: str, patterns: list) -> str:
    """Apply the first pattern that changes curr and return the result; unchanged when none does."""
    for pat in patterns:
        subbed = pat.sub("", curr).strip(" \t\r\n-–—:[]()")
        if subbed != curr:
            return subbed
    return curr


def _credit_text(value: str) -> str:
    """Remove metadata-only producer/studio lines before performer matching."""
    kept = []
    for line in str(value or "").splitlines():
        # Skip leading whitespace and decorative punctuation/emoji linearly.
        # The former regex expressed this as a repeated ambiguous group, which
        # could backtrack badly on emoji-heavy Telegram captions.
        start = 0
        while start < len(line) and (
            line[start].isspace()
            or not (line[start].isalnum() or line[start] == "_")
        ):
            start += 1
        if not _NON_PERFORMER_LABEL_RE.match(line[start:]):
            kept.append(line)
    return "\n".join(kept)


def normalize_name(value: str) -> str:
    """Fold accents, case and separators while retaining letters and digits."""
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    text = "".join(char for char in text if not unicodedata.category(char).startswith("M"))
    return "".join(char for char in text if char.isalnum())


def _normalized_with_spans(text: str) -> tuple[str, list[tuple[int, int]]]:
    output, spans = [], []
    for index, char in enumerate(text):
        folded = unicodedata.normalize("NFKD", char.casefold())
        folded = "".join(c for c in folded if not unicodedata.category(c).startswith("M"))
        for value in folded:
            if value.isalnum():
                output.append(value)
                spans.append((index, index + 1))
    return "".join(output), spans


def _edit_distance_one(left: str, right: str) -> bool:
    if abs(len(left) - len(right)) > 1:
        return False
    i = j = edits = 0
    while i < len(left) and j < len(right):
        if left[i] == right[j]:
            i += 1
            j += 1
            continue
        edits += 1
        if edits > 1:
            return False
        if i + 1 < len(left) and j + 1 < len(right) and left[i] == right[j + 1] and left[i + 1] == right[j]:
            i += 2
            j += 2
        elif len(left) >= len(right):
            i += 1
        else:
            j += 1
    return edits + int(i < len(left) or j < len(right)) == 1


def build_alias_index(records: Iterable[dict]) -> dict[str, list[dict]]:
    """Index reliable multi-part identities; leave mononyms for human review.

    Single-word performer names are easy to encounter in ordinary Telegram
    prose. We keep them out of automatic matching. A joined alias is accepted
    only when the canonical StashDB name has multiple parts and the alias is
    distinctive enough to survive as one token.
    """
    index: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        performer_id = str(record.get("id") or "")
        name = str(record.get("name") or "").strip()
        persisted = record.get("_persisted", True) is True
        if (not performer_id or is_non_performer_name(name)
                or len(re.findall(r"\w+", name, re.UNICODE)) < 2):
            continue
        trusted_aliases = {normalize_name(value) for value in record.get("trusted_aliases", [])}
        for alias in [name, *(record.get("aliases") or [])]:
            alias = str(alias or "").strip()
            if is_non_performer_name(alias):
                continue
            key = normalize_name(alias)
            if not key or len(key) < 4:
                continue
            existing = next((row for row in index[key] if row["id"] == performer_id), None)
            if existing:
                existing["trusted"] = existing["trusted"] or key in trusted_aliases
                existing["persisted"] = existing["persisted"] or persisted
            else:
                index[key].append({"id": performer_id, "name": name, "alias": alias,
                                   "trusted": key in trusted_aliases,
                                   "persisted": persisted})
    return dict(index)


def prepare_catalog(records: Iterable[dict]) -> dict:
    """Prepare reusable exact and typo indexes for a performer catalogue."""
    index = build_alias_index(records)
    keys_by_length: dict[int, list[str]] = defaultdict(list)
    for key in index:
        keys_by_length[len(key)].append(key)
    return {
        "index": index,
        "lengths": sorted(keys_by_length, reverse=True),
        "keys_by_length": dict(keys_by_length),
    }


def match_catalog(text_fields: Iterable[tuple[str, str]], records: Iterable[dict],
                  *, prepared: dict | None = None) -> tuple[list[dict], list[dict]]:
    """Return unambiguous exact matches and conservative typo proposals.

    Fuzzy proposals are emitted only for distinctively long tokens in an
    explicit comma/ampersand/and separated credit list.
    """
    fields = [(str(field), _credit_text(text)) for field, text in text_fields if text]
    fields = [(field, text) for field, text in fields if text]
    prepared = prepared or prepare_catalog(records)
    exact: dict[str, dict] = {}
    exact_order: dict[str, tuple[int, int]] = {}
    proposals: list[dict] = []
    claimed: dict[str, list[tuple[int, int]]] = defaultdict(list)
    normalized_fields = [(field, text, *_normalized_with_spans(text)) for field, text in fields]

    _collect_exact_matches(normalized_fields, prepared, exact, exact_order, proposals, claimed)

    # Typo review is restricted to explicit credits, long aliases and a single
    # edit (including adjacent transposition) to avoid fuzzy matches in prose.
    index = prepared["index"]
    for field, text in fields:
        for line in text.splitlines():
            line = line.strip()
            if not re.search(r",|&|\band\b", line, re.IGNORECASE):
                continue
            for raw_candidate in re.split(r",|&|\band\b", line, flags=re.IGNORECASE):
                _review_credit_candidate(raw_candidate, field, index, prepared, proposals)
    return sorted(exact.values(), key=lambda record: exact_order.get(record["id"], (len(fields), 0))), proposals


def _collect_exact_matches(
    normalized_fields: list,
    prepared: dict,
    exact: dict,
    exact_order: dict,
    proposals: list,
    claimed: dict,
) -> None:
    """Record unambiguous alias matches and ambiguity proposals for every probed span."""
    index = prepared["index"]
    # Probe only substrings whose lengths exist in the alias index. This avoids
    # rebuilding and scanning tens of thousands of aliases for every video.
    for field_index, (field, text, normalized_text, offsets) in enumerate(normalized_fields):
        for pos in range(len(normalized_text)):
            for length in prepared["lengths"]:
                if pos + length > len(normalized_text):
                    continue
                key = normalized_text[pos:pos + length]
                collisions = index.get(key)
                if not collisions:
                    continue
                span = (offsets[pos][0], offsets[pos + length - 1][1])
                if not _is_word_bounded(text, span):
                    continue
                if any(a < span[1] and span[0] < b for a, b in claimed[field]):
                    continue
                # Ambiguous names are suggestions only; never guess an identity.
                resolved = _resolve_collisions(key, collisions)
                if resolved is None:
                    proposals.append({"candidate": text[span[0]:span[1]], "matched_text": text[span[0]:span[1]],
                                      "source_field": field, "reason": "ambiguous_catalog_alias",
                                      "performer_id": None})
                    claimed[field].append(span)
                    continue
                record = resolved[0]
                alias_parts = re.findall(r"\w+", record["alias"], re.UNICODE)
                if len(alias_parts) < 2 and len(key) < 10 and not record["trusted"]:
                    continue
                exact[record["id"]] = record
                exact_order.setdefault(record["id"], (field_index, span[0]))
                claimed[field].append(span)


def _is_word_bounded(text: str, span: tuple[int, int]) -> bool:
    """Return True when the span is not glued to alphanumeric characters on either side."""
    if span[0] and text[span[0] - 1].isalnum():
        return False
    if span[1] < len(text) and text[span[1]].isalnum():
        return False
    return True


def _resolve_collisions(key: str, collisions: list) -> list | None:
    """Return the collision rows to use, or None when the alias is ambiguous.

    If exactly one persisted performer's canonical name matches, that performer
    wins over secondary aliases or synthetic unpersisted bios.
    """
    if len({row["id"] for row in collisions}) == 1:
        return collisions
    canonical_matches = [
        row for row in collisions
        if row.get("persisted") and normalize_name(row["name"]) == key
    ]
    if len({r["id"] for r in canonical_matches}) == 1:
        return canonical_matches
    return None


def _review_credit_candidate(raw_candidate: str, field: str, index: dict, prepared: dict, proposals: list) -> None:
    """Append uncatalogued-credit and possible-typo proposals for one credit-list entry."""
    detected_text = raw_candidate.strip(" \t\r\n-–—:[]()")
    candidate = clean_credit_candidate(detected_text)
    if not candidate:
        return
    if not _is_reviewable_credit(candidate):
        return

    key = normalize_name(detected_text)
    candidate_key = normalize_name(candidate)
    if len(key) < 6 or key in index or candidate_key in index:
        return
    words = candidate.split()
    explicit_name_shape = (
        2 <= len(words) <= 4 and all(word[:1].isupper() for word in words)
    ) or bool(re.search(r"[a-z][A-Z]", candidate) and len(words) == 1)
    if explicit_name_shape and is_valid_performer_name(candidate):
        proposals.append({"candidate": candidate, "matched_text": detected_text,
                          "source_field": field, "reason": "uncatalogued_credit",
                          "performer_id": None})
    record = _closest_typo_record(candidate_key, prepared, index)
    if record is not None:
        proposals.append({"candidate": record["name"], "matched_text": candidate,
                          "source_field": field, "reason": "possible_typo",
                          # Legacy-profile fallback records are
                          # synthetic; only persisted catalog rows
                          # can satisfy the proposal FK.
                          "performer_id": (record["id"] if record["persisted"]
                                           else None)})


def _is_reviewable_credit(candidate: str) -> bool:
    """Return False for labels, studios, and stopword-like candidates that must not be reviewed."""
    curr_low = candidate.lower()
    if is_non_performer_name(candidate) or curr_low in {"onlyfans", "of"}:
        return False
    if curr_low in STUDIO_NAMES or any(curr_low.endswith(sfx) for sfx in STUDIO_SUFFIXES):
        return False
    if curr_low in SINGLE_WORD_STOPWORDS or curr_low in TITLE_STOPWORDS:
        return False
    return True


def _closest_typo_record(candidate_key: str, prepared: dict, index: dict) -> dict | None:
    """Return the single closest catalog record one edit away from candidate_key, if unambiguous."""
    scored = []
    for length in (len(candidate_key) - 1, len(candidate_key), len(candidate_key) + 1):
        if length < 10:
            continue
        for alias_key in prepared["keys_by_length"].get(length, ()):
            if _edit_distance_one(candidate_key, alias_key):
                scored.extend((1, record) for record in index[alias_key])
    if not scored:
        return None
    top = max(score for score, _ in scored)
    winners = {row["id"]: row for score, row in scored if score == top}
    if len(winners) != 1:
        return None
    return next(iter(winners.values()))


def load_catalog(conn) -> list[dict]:
    """Load source identities plus existing database names and aliases."""
    people = conn.execute("SELECT id,name FROM performer_catalog WHERE active=TRUE ORDER BY name").fetchall()
    aliases = conn.execute("""
        SELECT performer_id,alias,origin FROM performer_catalog_aliases WHERE approved=TRUE
        ORDER BY length(alias) DESC
    """).fetchall()
    by_id = {str(pid): {"id": str(pid), "name": str(name), "aliases": [],
                        "trusted_aliases": [], "_persisted": True}
             for pid, name in people}
    for pid, alias, origin in aliases:
        if str(pid) in by_id:
            by_id[str(pid)]["aliases"].append(str(alias))
            if str(origin) == "reviewed":
                by_id[str(pid)]["trusted_aliases"].append(str(alias))
    _merge_legacy_bios(conn, by_id)
    _merge_legacy_aliases(conn, by_id)
    return list(by_id.values())


def _decode_legacy_aliases(legacy_aliases):
    """Decode a JSON-text aliases column; invalid JSON yields an empty list."""
    if isinstance(legacy_aliases, str):
        import json
        try:
            legacy_aliases = json.loads(legacy_aliases or "[]")
        except (TypeError, ValueError):
            legacy_aliases = []
    return legacy_aliases


def _merge_legacy_bios(conn, by_id: dict) -> None:
    """Add unpersisted performer_bios rows and their legacy aliases to by_id."""
    for name, legacy_aliases in conn.execute("SELECT name,aliases FROM performer_bios").fetchall():
        name = str(name or "").strip()
        if name:
            key = "local:" + normalize_name(name)
            by_id.setdefault(key, {"id": key, "name": name, "aliases": [],
                                   "trusted_aliases": [], "_persisted": False})
            legacy_aliases = _decode_legacy_aliases(legacy_aliases)
            if isinstance(legacy_aliases, (list, tuple)):
                for alias in legacy_aliases:
                    alias = str(alias or "").strip()
                    if alias:
                        by_id[key]["aliases"].append(alias)
                        by_id[key]["trusted_aliases"].append(alias)


def _merge_legacy_aliases(conn, by_id: dict) -> None:
    """Add rows from the legacy performer_aliases table, tolerating its absence."""
    try:
        legacy = conn.execute("SELECT alias,canonical FROM performer_aliases").fetchall()
    except Exception:
        legacy = []
    for alias, canonical in legacy:
        alias, canonical = str(alias or "").strip(), str(canonical or "").strip()
        key = "local:" + normalize_name(canonical)
        if canonical:
            by_id.setdefault(key, {"id": key, "name": canonical, "aliases": [],
                                  "trusted_aliases": [], "_persisted": False})
            if alias:
                by_id[key]["aliases"].append(alias)
                by_id[key]["trusted_aliases"].append(alias)
