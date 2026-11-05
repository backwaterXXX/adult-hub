"""Shared title parsing core with adapter-specific known-name policies."""

import html
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from functools import lru_cache
from typing import Callable, Dict, List, Optional

from common.performers import (
    ACTION_VERBS,
    MEDIA_EXT_SUFFIX_RE,
    SCENE_SUFFIX_RE,
    STANDALONE_SCENE_RE,
    TITLE_STOPWORDS,
    is_valid_performer_name,
    is_non_performer_name,
    normalize_performers,
    strip_nationality_prefix,
    strip_scene_suffix,
)


_DASH_SPLIT_RE = re.compile(r'(\s+[–—\-]\s+)')
_DASH_PARTS_RE = re.compile(r'\s+[–—\-]\s+')
_COLON_SPLIT_RE = re.compile(r':\s+')
_NAME_SPLIT_RE = re.compile(r'[,&]|\band\b', re.IGNORECASE)
_NAME_LIST_RE = re.compile(r'\band\b', re.IGNORECASE)
_YEAR_PARENS_RE = re.compile(r'\s*\(\s*(?:19|20)\d{2}\s*\)')
_YEAR_RE = re.compile(r'\s*\b(?:19|20)\d{2}\b')
_RESOLUTION_RE = re.compile(r'\s*\[\s*\d{3,4}p\s*\]|\s*\(\s*\d{3,4}p\s*\)|\s*\[?\b\d{3,4}p\b\]?', re.IGNORECASE)
_ACTION_SUFFIX_RE = re.compile(r'\s+(?:flip\s*fuck\w*|fuck\w*|breed\w*|dp\w*)$', re.IGNORECASE)
_SCENE_PREFIX_RE = re.compile(r'^(?:SC\d+[-=_]|MO\d+\b|Sc\.?\s*\d+[\s–—\-]*)\s*', re.IGNORECASE)
_PART_SUFFIX_RE = SCENE_SUFFIX_RE
_TITLE_SUFFIX_RE = re.compile(
    r'\s+\b(?:remastered|trailer|bonus|raw|hd|preview|interview|backstage|special|edition|cut)\b.*$',
    re.IGNORECASE,
)
_INITIALS_SUFFIX_RE = re.compile(r'(\s+[A-Z]{3,}){2,}$')
_HANDLE_RE = re.compile(r'\s*\(@?[\w.\-]+\)')
_LEADING_TAG_RE = re.compile(r'^[\[(][^\])]*[\])]\s*[-–—:]?\s*')
_LEADING_AGGREGATOR_RE = re.compile(
    r'^(?:(?:only\s*fans|just\s*for\s*fans)\s*[-–—:]?\s*|OF\s+[-–—:]?\s*)', re.IGNORECASE
)
_HASHTAG_RE = re.compile(r'#\w+\b')
_LEADING_NONWORD_RE = re.compile(r'^[^\w]+')
_LEADING_SEPARATOR_RE = re.compile(r'^[-–—:]\s*')
_HASHTAG_ONLY_RE = re.compile(r'^(?:[^\w\s#]*\s*#[a-zA-Z0-9_]*[a-zA-Z_][a-zA-Z0-9_]*\s*)+$', re.UNICODE)


class NormalizedKnownMap(dict[str, str]):
    """Known performer map that has already had nationality prefixes removed."""
_ACTION_TITLE_RE = re.compile(
    r'^([A-Z0-9][a-zA-Z0-9_\s\.\'\-,&]+?)\s+' + ACTION_VERBS
    + r'\s+([A-Z0-9][a-zA-Z0-9_\s\.\'\-]+?)(?:\s*[–\-]|$|!)', re.IGNORECASE
)


@lru_cache(maxsize=4096)
def _known_boundary_pattern(lower_name: str) -> re.Pattern[str]:
    parts = [part for part in re.split(r'[\s_-]+', lower_name) if part]
    separated = r'[\s_-]+'.join(re.escape(part) for part in parts)
    camel_case = r'(?=(?-i:[A-Z]))'.join(re.escape(part) for part in parts)
    alternatives = [separated]
    if len(parts) > 1:
        alternatives.append(camel_case)
    pattern = r'(?<![^\W_])(?:' + '|'.join(alternatives) + r')(?![^\W_])'
    return re.compile(pattern, re.IGNORECASE)


def _normalize_known_map(known_set: Mapping[str, str] | Iterable[str] | None) -> Dict[str, str]:
    """Normalize names and retain the first display spelling for each name."""
    if isinstance(known_set, NormalizedKnownMap):
        return known_set
    normalized: Dict[str, str] = {}
    if known_set is None:
        return normalized
    items = known_set.items() if isinstance(known_set, Mapping) else ((name, name) for name in known_set)
    for raw_key, raw_display in items:
        key, display = str(raw_key).strip().lower(), str(raw_display).strip()
        if key and display and not is_non_performer_name(key) and not is_non_performer_name(display):
            normalized.setdefault(key, display)
    for key in list(normalized):
        bare_key = strip_nationality_prefix(key)
        if bare_key != key and bare_key:
            display = normalized[key]
            bare_display = strip_nationality_prefix(display) or display
            normalized.setdefault(bare_key, bare_display)
            del normalized[key]
    return NormalizedKnownMap(normalized)


def normalize_known_map(known_set: Mapping[str, str] | Iterable[str] | None) -> NormalizedKnownMap:
    """Normalize a performer map once for reuse across multiple title parses."""
    return _normalize_known_map(known_set)


def _prefix_length_ignore_case(text: str, lower_prefix: str) -> int | None:
    """Find a lowercase prefix without assuming Unicode lowercasing preserves length."""
    prefix = ''
    for index, char in enumerate(text):
        prefix += char.lower()
        if prefix == lower_prefix:
            return index + 1
        if not lower_prefix.startswith(prefix):
            return None
    return None


def _expand_compact_name(name: str, known_set: Mapping[str, str] | None = None) -> str:
    """Turn underscore and CamelCase separators into spaces in a list member unless known."""
    if known_set:
        n_low = name.lower()
        if n_low in known_set:
            return known_set[n_low]
        n_compact = re.sub(r'[^a-z0-9]', '', n_low)
        if n_compact in known_set:
            return known_set[n_compact]
    name = re.sub(r'[_]+', ' ', name)
    if not re.search(r'\s', name):
        name = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', name)
    return re.sub(r'\s+', ' ', name).strip()


def _strip_name_decorations(n: str) -> str:
    """Remove media suffixes, years, resolutions, brackets, scene and title noise from a name candidate."""
    n = n.strip()
    n = MEDIA_EXT_SUFFIX_RE.sub('', n).strip()
    n = _YEAR_PARENS_RE.sub('', n)
    n = _YEAR_RE.sub('', n)
    n = _RESOLUTION_RE.sub('', n)
    n = _ACTION_SUFFIX_RE.sub('', n)
    n = re.sub(r'^[\[(][^\])]*[\])]\s*', '', n)
    n = re.sub(r'\s*\([^)]*\)', '', n)
    n = re.sub(r'\s*\[[^\]]*\]', '', n)
    n = _SCENE_PREFIX_RE.sub('', n)
    n = strip_scene_suffix(n)
    n = _TITLE_SUFFIX_RE.sub('', n)
    n = _INITIALS_SUFFIX_RE.sub('', n)
    n = re.sub(r'^[^\w]+|[^\w]+$', '', n).strip()
    n = strip_scene_suffix(n)
    n = re.sub(r'\s+\b(?:bb|ff)\b$', '', n, flags=re.IGNORECASE).strip()
    return n


def _strip_edge_punctuation(value: str) -> str:
    """Remove leading and trailing non-word characters and surrounding whitespace."""
    return re.sub(r'^[^\w]+|[^\w]+$', '', value).strip()


def _known_remainder_display(bare: str, known_index: Mapping[str, List[tuple[str, str]]]) -> Optional[str]:
    """Return the known display name matching the nationality-stripped remainder, or None."""
    first_remainder_word = bare.split(maxsplit=1)[0].lower() if bare else ''
    for k_low, display in known_index.get(first_remainder_word, ()):
        if bare.lower() == k_low:
            return _strip_edge_punctuation(display)
    return None


def _clean_name_candidate(
    n: str,
    known_index: Mapping[str, List[tuple[str, str]]] | None = None,
) -> str:
    n = _strip_name_decorations(n)
    bare = strip_nationality_prefix(n)
    if bare != n and bare:
        if known_index:
            matched = _known_remainder_display(bare, known_index)
            if matched is not None:
                return matched
        if is_valid_performer_name(bare):
            n = bare
    if known_index:
        matched = _known_index_match(n, known_index)
        if matched is not None:
            return matched
    return n.strip()


def _known_index_match(n: str, known_index: Mapping[str, List[tuple[str, str]]]) -> Optional[str]:
    """Return the display form for a candidate that matches a known performer by prefix, else None."""
    first_word = n.split(maxsplit=1)[0].lower() if n else ''
    for k_low, display in known_index.get(first_word, ()):
        prefix_len = _prefix_length_ignore_case(n, k_low)
        if prefix_len is not None and n[prefix_len:].strip() == '' and n.lower() == k_low:
            return _exact_known_display(n, display)
        if len(k_low.split()) >= 2 and prefix_len is not None:
            rest = n[prefix_len:].strip()
            if not rest or not rest[0].islower():
                return _prefix_known_display(n[:prefix_len].strip(), display)
    return None


def _exact_known_display(n: str, display: str) -> str:
    """Choose the display form when the whole candidate equals a known key."""
    if any(char.isupper() for char in display):
        return _strip_edge_punctuation(display)
    if any(char.isupper() for char in n):
        return _strip_edge_punctuation(n)
    return _strip_edge_punctuation(display)


def _prefix_known_display(matched_slice: str, display: str) -> str:
    """Choose the display form when a known multi-word key matches a prefix of the candidate."""
    if any(c.isupper() for c in display):
        return _strip_edge_punctuation(display)
    if any(c.isupper() for c in matched_slice):
        return _strip_edge_punctuation(matched_slice)
    return _strip_edge_punctuation(display)


def extract_trailing_paren_performers(
    text: str,
    known_index: Mapping[str, List[tuple[str, str]]] | None = None,
    known_set: Mapping[str, str] | None = None,
) -> List[str] | None:
    t = MEDIA_EXT_SUFFIX_RE.sub('', text.strip()).strip()
    t = _RESOLUTION_RE.sub('', t).strip()
    m = re.search(r'\(([^)]+)\)$', t)
    if not m:
        return None
    inner = m.group(1).strip()
    inner = re.sub(r'[,–—\-]?\s*\b(?:19|20)\d{2}\b\s*$', '', inner).strip()
    if not (',' in inner or '&' in inner or _NAME_LIST_RE.search(inner) or re.search(r'\by\b', inner, re.IGNORECASE)):
        return None
    raw_names = [
        _expand_compact_name(_clean_name_candidate(n, known_index), known_set)
        for n in re.split(r'[,&]|\band\b|\by\b', inner, flags=re.I)
        if n.strip()
    ]
    if len(raw_names) >= 2 and all(is_valid_performer_name(n) for n in raw_names):
        if any(len(n.split()) >= 2 for n in raw_names) or (known_index and any(n.lower() in known_index for n in raw_names)):
            return normalize_performers(raw_names, alias_map=known_set)
    return None


def parse_title_performers_with_known_map(
    title: str,
    known_set: Mapping[str, str] | Iterable[str] | None = None,
) -> List[str]:
    """Extract performer display names from a video title.

    Handles common studio / aggregator naming conventions:
      - 'Performer 1, Performer 2 & Performer 3 – Title'
      - 'Performer 1 & Performer 2 – Title'
      - 'Title – Performer 1, Performer 2 & Performer 3'
      - 'Series: Performer 1, Performer 2 & Performer 3'
      - 'Performer 1 [and P2] fucks/breeds/bottoms for Performer 3'
      - Strips parenthesized handles e.g. '(handle)' / '(@handle)'
      - Single performer after dash or in descriptive titles matched against known_performers

    Returns deduplicated names in order of appearance.
    """
    if not isinstance(title, str) or not title.strip():
        return []
    text = _prepare_title_text(title)
    known_set = _normalize_known_map(known_set)
    known_index: Dict[str, List[tuple[str, str]]] = defaultdict(list)
    for key, display in known_set.items():
        known_index[key.split()[0]].append((key, display))

    # 0.5. Check trailing parenthesized performer list e.g. "Title (Performer 1, Performer 2 & Performer 3)"
    trailing_paren_perfs = extract_trailing_paren_performers(text, known_index, known_set)
    if trailing_paren_perfs:
        return trailing_paren_perfs
    # Strip parenthesized handles / alternate names e.g. (handle) or (aka1, aka2)
    text = re.sub(r'\s*\([^)]*\)', '', text)

    # 1. Check Action verbs (fucks, flip fucks, breeds, bottoms for, fuck & DP, etc.)
    action_perfs = _match_action_title(text, known_index, known_set)
    if action_perfs is not None:
        return action_perfs

    # 2. Split on dashes (' – ', ' — ', ' - ') or colon
    dash_perfs = _match_dash_parts(text, known_index, known_set)
    if dash_perfs is not None:
        return dash_perfs

    # 3. Whole string or trailing suffix is a performer list (e.g. "Performer 1 & Performer 2", "P1, P2 & P3")
    whole_perfs = _match_whole_text_list(text, known_index, known_set)
    if whole_perfs is not None:
        return whole_perfs

    # 3.5. Sequence of space-separated CamelCase / compact performer names
    # e.g. "JayAlexander BryceEvans OliverHunt", "AndresMilan RicoMarlon", "CodySeiya GriffinBarrows IsaacX Part2"
    compact_perfs = _match_compact_sequence(text, known_index, known_set)
    if compact_perfs is not None:
        return compact_perfs

    # 4. Dictionary matching fallback (for descriptive titles)
    if known_set:
        dictionary_perfs = _match_known_dictionary(text, known_set)
        if dictionary_perfs is not None:
            return dictionary_perfs

    return []


def _prepare_title_text(title: str) -> str:
    """Unescape and strip site, quality, handle, and tag noise from a raw title."""
    text = html.unescape(title).strip()
    text = MEDIA_EXT_SUFFIX_RE.sub('', text).strip()
    text = _RESOLUTION_RE.sub('', text).strip()
    text = re.sub(r'\s+(?:em|at)\s+(?:Streamtape|DoodStream)(?:\s+com)?.*$', '', text, flags=re.IGNORECASE).strip()
    text = re.sub(r'\s+(?:mp4|mkv|avi)\b.*$', '', text, flags=re.IGNORECASE).strip()
    # Strip social handles in parentheses e.g. (louis_ricaute) or (@mattoffthetrail)
    text = _HANDLE_RE.sub('', text)
    # Strip bracketed studio / channel tags at the start e.g. [Only Fans], [Guy Bone], [Raw Fuck Club]
    text = _LEADING_TAG_RE.sub('', text)
    # Strip leading aggregator / site names e.g. Only Fans, Just For Fans, OF
    text = _LEADING_AGGREGATOR_RE.sub('', text)
    # Strip leading hashtags e.g. #OF #ANAL #TWINK and leading non-word symbols
    text = _HASHTAG_RE.sub('', text)
    text = _LEADING_NONWORD_RE.sub('', text)
    text = _LEADING_SEPARATOR_RE.sub('', text).strip()
    return text


def _expand_split_names(
    s: str,
    known_index: Mapping[str, List[tuple[str, str]]],
    known_set: Mapping[str, str],
) -> List[str]:
    """Split a name list on _NAME_SPLIT_RE and clean each non-empty piece into a performer display name."""
    return [
        _expand_compact_name(_clean_name_candidate(n, known_index), known_set)
        for n in _NAME_SPLIT_RE.split(s) if n.strip()
    ]


def _drop_trailing_invalid(names: List[str]) -> None:
    """Remove invalid performer names from the end of names, in place."""
    while names and not is_valid_performer_name(names[-1]):
        names.pop()


def _match_action_title(
    text: str,
    known_index: Mapping[str, List[tuple[str, str]]],
    known_set: Mapping[str, str],
) -> Optional[List[str]]:
    """Match 'Performer fucks/breeds/... Performer' style titles; None when the pattern does not apply."""
    m_act = _ACTION_TITLE_RE.search(text)
    if not m_act:
        return None
    left = m_act.group(1).strip()
    right = (m_act.group(2) or '').strip()
    left_names = _expand_split_names(left, known_index, known_set)
    right_clean = _expand_compact_name(_clean_name_candidate(right, known_index), known_set) if right else ''
    if not (left_names and all(is_valid_performer_name(n) for n in left_names)):
        return None
    if right_clean and is_valid_performer_name(right_clean):
        return normalize_performers(left_names + [right_clean], alias_map=known_set)
    if (
        len(left_names) >= 2
        or any(len(n.split()) >= 2 for n in left_names)
        or any(n.lower() in known_set for n in left_names)
        or (len(left_names) == 1 and len(left_names[0]) >= 6 and left_names[0].lower() not in TITLE_STOPWORDS)
    ):
        return normalize_performers(left_names, alias_map=known_set)
    return None


def _match_dash_parts(
    text: str,
    known_index: Mapping[str, List[tuple[str, str]]],
    known_set: Mapping[str, str],
) -> Optional[List[str]]:
    """Match performer lists found in dash- or colon-separated title parts; None when no candidate wins."""
    parts = _DASH_PARTS_RE.split(text)
    if len(parts) < 2 and _COLON_SPLIT_RE.search(text):
        parts = _COLON_SPLIT_RE.split(text, maxsplit=1)
    if len(parts) < 2:
        return None

    candidates = []
    for idx, part in enumerate(parts):
        cand = part.strip()
        if not cand:
            continue
        action_cand = _dash_action_candidate(cand, idx, known_index, known_set)
        if action_cand is not None:
            candidates.append(action_cand)
        if '&' in cand or ',' in cand or _NAME_LIST_RE.search(cand):
            list_cand = _dash_list_candidate(cand, idx, known_index, known_set)
            if list_cand is not None:
                candidates.append(list_cand)
        elif known_set:
            known_cand = _dash_known_candidate(cand, idx, known_index, known_set)
            if known_cand is not None:
                candidates.append(known_cand)

    return _pick_dash_candidate(candidates)


def _dash_action_candidate(
    cand: str,
    idx: int,
    known_index: Mapping[str, List[tuple[str, str]]],
    known_set: Mapping[str, str],
) -> Optional[tuple[tuple[int, int, int, int], List[str]]]:
    """Score a dash part that contains an action verb followed by a performer."""
    m_cand_act = _ACTION_TITLE_RE.search(cand)
    if not m_cand_act:
        return None
    c_left = m_cand_act.group(1).strip()
    c_right = (m_cand_act.group(2) or '').strip()
    c_left_names = _expand_split_names(c_left, known_index, known_set)
    c_right_clean = _expand_compact_name(_clean_name_candidate(c_right, known_index), known_set) if c_right else ''
    if not (c_left_names and all(is_valid_performer_name(n) for n in c_left_names)):
        return None
    if not (c_right_clean and is_valid_performer_name(c_right_clean)):
        return None
    c_all = normalize_performers(c_left_names + [c_right_clean], alias_map=known_set)
    multi_words = sum(1 for n in c_all if len(n.split()) >= 2)
    score = (multi_words, len(c_all), len(c_all), idx)
    return score, c_all


def _dash_list_candidate(
    cand: str,
    idx: int,
    known_index: Mapping[str, List[tuple[str, str]]],
    known_set: Mapping[str, str],
) -> Optional[tuple[tuple[int, int, int, int], List[str]]]:
    """Score a dash part that is a comma or ampersand separated performer list."""
    raw_names = _expand_split_names(cand, known_index, known_set)
    _drop_trailing_invalid(raw_names)
    if not (raw_names and len(raw_names) >= 2 and all(is_valid_performer_name(n) for n in raw_names)):
        return None
    multi_words = sum(1 for n in raw_names if len(n.split()) >= 2)
    known_words = sum(1 for n in raw_names if n.lower() in known_set)
    score = (multi_words, known_words, len(raw_names), idx)
    return score, normalize_performers(raw_names, alias_map=known_set)


def _dash_known_candidate(
    cand: str,
    idx: int,
    known_index: Mapping[str, List[tuple[str, str]]],
    known_set: Mapping[str, str],
) -> Optional[tuple[tuple[int, int, int, int], List[str]]]:
    """Score a single dash part that is a known performer name."""
    cleaned_cand = _clean_name_candidate(cand, known_index)
    if not (is_valid_performer_name(cleaned_cand) and cleaned_cand.lower() in known_set):
        return None
    preferred = cleaned_cand if any(c.isupper() for c in cleaned_cand) else known_set[cleaned_cand.lower()]
    multi_words = 1 if len(preferred.split()) >= 2 else 0
    score = (multi_words, 1, 1, idx)
    return score, normalize_performers([preferred], alias_map=known_set)


def _pick_dash_candidate(candidates: list) -> Optional[List[str]]:
    """Choose the best dash-part candidate; None means no candidate is strong enough to return."""
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    best_score, best_performers = candidates[0]
    if best_score[0] > 0:
        return best_performers
    if best_score[1] >= len(best_performers) or (best_score[1] >= 1 and len(best_performers) == 2):
        return best_performers
    return None


def _match_whole_text_list(
    text: str,
    known_index: Mapping[str, List[tuple[str, str]]],
    known_set: Mapping[str, str],
) -> Optional[List[str]]:
    """Match a whole title, or its trailing part, as a performer list; None when it does not apply."""
    if not ('&' in text or ',' in text or _NAME_LIST_RE.search(text)):
        return None
    raw_names = _expand_split_names(text, known_index, known_set)
    _drop_trailing_invalid(raw_names)
    if not (raw_names and len(raw_names) >= 2):
        return None
    if all(is_valid_performer_name(n) for n in raw_names):
        if any(len(n.split()) >= 2 for n in raw_names) or any(n.lower() in known_set for n in raw_names):
            return normalize_performers(raw_names, alias_map=known_set)
        return None
    if all(is_valid_performer_name(n) for n in raw_names[1:]):
        found_first = _find_leading_first_name(raw_names[0], known_index, known_set)
        if found_first:
            combined = [found_first] + raw_names[1:]
            if any(len(n.split()) >= 2 for n in combined) or any(n.lower() in known_set for n in combined):
                return normalize_performers(combined, alias_map=known_set)
    return None


def _find_leading_first_name(
    first_part: str,
    known_index: Mapping[str, List[tuple[str, str]]],
    known_set: Mapping[str, str],
) -> Optional[str]:
    """Find a valid performer name formed by the last 3 or 2 words of first_part."""
    words = first_part.split()
    for wlen in (3, 2):
        if len(words) >= wlen:
            cand = " ".join(words[-wlen:])
            cand_clean = _expand_compact_name(_clean_name_candidate(cand, known_index), known_set)
            if is_valid_performer_name(cand_clean) and (
                (cand_clean[0].isupper() and all(w[0].isupper() for w in cand_clean.split() if w))
                or (known_set and cand_clean.lower() in known_set)
            ):
                return cand_clean
    return None


def _match_compact_sequence(
    text: str,
    known_index: Mapping[str, List[tuple[str, str]]],
    known_set: Mapping[str, str],
) -> Optional[List[str]]:
    """Match a run of space-separated CamelCase or compact performer names at the end of the title."""
    tokens = text.split()
    while tokens and (STANDALONE_SCENE_RE.match(tokens[-1]) or _RESOLUTION_RE.match(tokens[-1])):
        tokens.pop()
    if len(tokens) < 2:
        return None
    cand_perfs = [
        _expand_compact_name(_clean_name_candidate(t, known_index), known_set)
        for t in tokens
    ]
    if all(
        is_valid_performer_name(c)
        and (len(c.split()) >= 2 or (known_set and c.lower() in known_set))
        for c in cand_perfs
    ) and any(
        re.search(r'[a-z][A-Z]', t) or '_' in t or (known_set and c.lower() in known_set)
        for t, c in zip(tokens, cand_perfs)
    ):
        return normalize_performers(cand_perfs, alias_map=known_set)
    return None


def _match_known_dictionary(text: str, known_set: Mapping[str, str]) -> Optional[List[str]]:
    """Match known performer names anywhere in the text, preferring earliest and longest spans."""
    candidates = _collect_dictionary_candidates(text, known_set)
    if candidates:
        candidates.sort()
        found = []
        seen_lower = set()
        accepted_spans = []
        for start, _, end, k_low, display in candidates:
            if len(found) >= 5:
                break
            if any(start < accepted_end and end > accepted_start for accepted_start, accepted_end in accepted_spans):
                continue
            accepted_spans.append((start, end))
            if k_low not in seen_lower:
                seen_lower.add(k_low)
                found.append(display)
        if found:
            return normalize_performers(found, alias_map=known_set)
    return None


def _collect_dictionary_candidates(text: str, known_set: Mapping[str, str]) -> list:
    """Collect (start, -length, end, key, display) tuples for every accepted known-name match in text."""
    candidates = []
    text_lower = text.lower()
    for k_low, display in known_set.items():
        first_w = k_low.split()[0] if k_low else ''
        if first_w and first_w not in text_lower:
            continue
        # Avoid single common words
        is_single_word = len(k_low.split()) == 1
        if is_single_word and (len(k_low) < 6 or k_low in TITLE_STOPWORDS):
            continue
        pattern = _known_boundary_pattern(k_low)
        for match in pattern.finditer(text):
            if is_single_word and _rejects_single_word_match(text, match.start(), match.end(), k_low, known_set):
                continue
            span_length = match.end() - match.start()
            candidates.append((match.start(), -span_length, match.end(), k_low, display))
    return candidates


def _rejects_single_word_match(
    text: str,
    start: int,
    end: int,
    k_low: str,
    known_set: Mapping[str, str],
) -> bool:
    """Return True when a single-word known name match lacks enough context to be accepted."""
    is_distinctive = (
        len(k_low) >= 9
        or k_low.startswith(('mr', 'the', 'dr'))
        or bool(re.search(r'[\d._\-]|xxx', k_low))
    )
    if is_distinctive:
        return False
    rest = text[end:]
    m_after = re.match(r'^\s+([A-Z][a-zA-Z0-9]+)(?:\s+([A-Z][a-zA-Z0-9]+))?', rest)
    if m_after:
        w1 = m_after.group(1)
        w2 = m_after.group(2)
        if not (w1 and w2) and not (w1 and w1.lower() in known_set):
            return True
    before = text[:start]
    return bool(re.search(r'\b[A-Z][a-z]+\s+$', before))


def strip_performer_credits(
    text: str,
    performers: Iterable[str],
    segment_match: Callable[[str, Iterable[str]], bool],
) -> str:
    """Remove a performer-only prefix or suffix while preserving title text."""
    perfs_list = list(performers)
    if not perfs_list:
        return text

    parts = _DASH_SPLIT_RE.split(text)
    if len(parts) >= 3:
        segments = parts[::2]
        lead = 0
        while lead < len(segments) and segment_match(segments[lead].strip(), perfs_list):
            lead += 1
        trail = len(segments)
        while trail > lead and segment_match(segments[trail - 1].strip(), perfs_list):
            trail -= 1
        if lead or trail < len(segments):
            start = 2 * lead
            end = 2 * trail - 1 if trail < len(segments) else len(parts)
            cleaned = ''.join(parts[start:end]).strip()
            if len(cleaned) >= 3 and not _HASHTAG_ONLY_RE.match(cleaned):
                return cleaned

    if _COLON_SPLIT_RE.search(text):
        left, right = _COLON_SPLIT_RE.split(text, maxsplit=1)
        left = left.strip()
        right = right.strip()
        left_is_perfs = segment_match(left, perfs_list)
        right_is_perfs = segment_match(right, perfs_list)
        if left_is_perfs and not right_is_perfs and len(right) >= 3 and not _HASHTAG_ONLY_RE.match(right):
            return right
        if right_is_perfs and not left_is_perfs and len(left) >= 3 and not _HASHTAG_ONLY_RE.match(left):
            return left

    return text
