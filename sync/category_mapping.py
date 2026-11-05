"""Category mapping and normalization for Adult-Hub.

Reduces raw provider categories (106+) to a clean set of 23 unified genres
and prevents provider names (studios) from polluting the Jellyfin genres list.
"""

import re
from typing import Optional, Set, Iterable

# Provider names that should NEVER be genres (they belong in <studio> and <tag>)
# are rejected by the static exclusion tables below; there is no runtime
# registration hook (the retired web extension was its only caller).

if __package__:
    from .category_mapping_data import ALL_TARGET_CATEGORIES, EXCLUDED_TAGS, RAW_TO_MAPPED_CATEGORY
else:
    from category_mapping_data import ALL_TARGET_CATEGORIES, EXCLUDED_TAGS, RAW_TO_MAPPED_CATEGORY


def _clean_slug(raw: str) -> str:
    """Normalize slugs like 'anal-9ft75' or 'big-dick-dljhx' to 'anal', 'big dick'."""
    s = raw.strip().lower()
    # Remove hash suffixes like '-9ft75'
    parts = s.split("-")
    if len(parts) > 1 and len(parts[-1]) == 5 and parts[-1].isalnum():
        parts = parts[:-1]
    return " ".join(parts)


# Ordered keyword heuristics used by map_category. The first rule with a matching keyword wins.
_CATEGORY_KEYWORD_RULES = (
    (("blowjob", "deepthroat", "rimming", "rimjob", "oral", "selfsuck"), "Oral & Rimming"),
    (("bareback", "breeding", "creampie"), "Bareback & Breeding"),
    (("cumshot", "facial"), "Cumshots"),
    (("anal", "double penetration", "flip fuck"), "Anal"),
    (("threesome", "trio", "3way", "3-way"), "Threesome"),
    (("foursome", "gangbang", "group", "orgy", "orgies"), "Group"),
    (("muscle", "hunk", "jock", "workout"), "Muscle & Jocks"),
    (("bear", "chubby", "hairy"), "Bear & Chubby"),
    (("daddy", "mature", "dadson"), "Daddy & Mature"),
    (("twink", "young", "college", "lad"), "Twink & Young"),
    (("big dick", "big cock", "size kink", "size diff"), "Big Dick & Size"),
    (("onlyfans", "amateur", "homemade", "tiktok", "leaked"), "Amateur & OnlyFans"),
    (("pov", "hidden camera"), "POV & Voyeur"),
    (("cruising", "gloryhole", "glory hole", "outdoor", "public"), "Public & Cruising"),
    (("fetish", "fisting", "poppers", "pissing", "sissy", "worship", "toy"), "Fetish & Kink"),
    (("rough", "verbal", "roleplay", "role play", "uniform", "taboo"), "BDSM & Roleplay"),
    (("massage", "shower"), "Massage & Shower"),
    (("latino", "latin"), "Latino"),
    (("interracial", "black"), "Black & Interracial"),
    (("asian", "european"), "Asian & International"),
    (("straight", "bisexual"), "Straight & Bisexual"),
    (("trans", "femboy"), "Trans & Femboy"),
    (("compilation", "shorts"), "Compilations & Media"),
    (("solo", "jerk", "handjob", "edging"), "Solo & Masturbation"),
)


def _normalize_raw_category(category: str) -> Optional[str]:
    """Strip whitespace and Telegram '#' prefixes. Returns None for empty or 'browse' labels."""
    raw = category.strip()
    if not raw or raw.lower().startswith("browse"):
        return None

    # Strip leading '#' if present (Telegram hashtags: #bareback -> bareback)
    if raw.startswith("#"):
        raw = raw[1:].strip()
        if not raw:
            return None
    return raw


def _lookup_mapped_category(raw: str, low: str) -> Optional[str]:
    """Resolve a normalized category through the lookup tables and keyword heuristics."""
    # Check direct exact match in dictionary
    if low in RAW_TO_MAPPED_CATEGORY:
        return RAW_TO_MAPPED_CATEGORY[low]

    # Check if already a target category
    if raw in ALL_TARGET_CATEGORIES:
        return raw

    # Try slug normalization (e.g. 'anal-9ft75' -> 'anal')
    cleaned = _clean_slug(raw)
    if cleaned in RAW_TO_MAPPED_CATEGORY:
        return RAW_TO_MAPPED_CATEGORY[cleaned]

    # Keyword heuristics for novel upstream categories (first matching rule wins)
    for keywords, target in _CATEGORY_KEYWORD_RULES:
        if any(k in low for k in keywords):
            return target

    return None


def map_category(category: Optional[str]) -> Optional[str]:
    """Map a category name, slug or label to one of the 23 unified categories.

    Returns None if the category is excluded (e.g. provider name, junk tag, or invalid).
    """
    if not category:
        return None

    raw = _normalize_raw_category(category)
    if raw is None:
        return None

    low = raw.lower()

    # Exclude technical/junk tags
    if low in EXCLUDED_TAGS:
        return None

    return _lookup_mapped_category(raw, low)


# Title regex rules used by guess_categories_from_title. Every matching rule adds its category.
_TITLE_CATEGORY_RULES = (
    (r'\b(fuck|fucks|fucking|fucked|pounded|pounding|plowed|penetrat|ass|bottom|bottoms|bottoming|drilled|hole)\b', 'Anal'),
    (r'\b(bareback|breed|breeds|breeding|bred|creampie|raw)\b', 'Bareback & Breeding'),
    (r'\b(blowjob|deepthroat|bj|oral|sucking|sucks|suck|rim|rimming|lick|licking)\b', 'Oral & Rimming'),
    (r'\b(cum|cums|cumming|cumshot|load|facial|swallow)\b', 'Cumshots'),
    (r'\b(jerk|jerks|jerking|jerkoff|handjob|stroke|stroking|solo|masturbat|edge|edging)\b', 'Solo & Masturbation'),
    (r'\b(threesome|trio|3way|3-way)\b', 'Threesome'),
    (r'\b(foursome|gangbang|group|orgy|double dipping|dped)\b', 'Group'),
    (r'\b(muscle|muscular|jock|hunk|gym|workout|bicep|bodybuilder)\b', 'Muscle & Jocks'),
    (r'\b(bear|chubby|hairy|otter|beard|chub)\b', 'Bear & Chubby'),
    (r'\b(daddy|mature|older|dilf|gramps|silver|father)\b', 'Daddy & Mature'),
    (r'\b(twink|twinks|boy|boys|college|young|teen|lad)\b', 'Twink & Young'),
    (r'\b(big|huge|thick|monster|giant|cock|dick|penis|endowed|size)\b', 'Big Dick & Size'),
    (r'\b(dildo|toy|toys|feet|foot|fist|fisting|kink|fetish|leather|rubber|spank|bondage|cage|caged|poppers)\b', 'Fetish & Kink'),
    (r'\b(onlyfans|amateur|homemade|tiktok|webcam|cam|leaked|live|ig)\b', 'Amateur & OnlyFans'),
    (r'\b(pov)\b', 'POV & Voyeur'),
    (r'\b(public|outdoor|outdoors|park|car|beach|cruising|forest|woods|gloryhole)\b', 'Public & Cruising'),
    (r'\b(massage|shower|sauna|bath)\b', 'Massage & Shower'),
    (r'\b(latino|latin)\b', 'Latino'),
    (r'\b(black|ebony|interracial|bbc)\b', 'Black & Interracial'),
    (r'\b(asian)\b', 'Asian & International'),
    (r'\b(straight|bi|bisexual)\b', 'Straight & Bisexual'),
)


def guess_categories_from_title(title: str) -> Set[str]:
    """Infer relevant categories from a video title when upstream provider categories are absent."""
    if not title:
        return {"Amateur & OnlyFans"}
    t = title.lower()
    res = set()
    for pattern, category in _TITLE_CATEGORY_RULES:
        if re.search(pattern, t):
            res.add(category)
    if not res:
        res.add('Amateur & OnlyFans')
    return res


def map_categories(categories: Iterable[str], title: Optional[str] = None) -> Set[str]:
    """Map an iterable of categories, returning a deduplicated set of target categories.

    If categories resolve to empty and a title is provided, falls back to inferring categories from the title.
    """
    mapped: Set[str] = set()
    for cat in categories:
        if not cat:
            continue
        c_low = cat.strip().lower()
        if c_low in ("group & threesome", "group and threesome"):
            if title and re.search(r'\b(threesome|trio|3way|3-way)\b', title.lower()):
                mapped.add("Threesome")
            else:
                mapped.add("Group")
            continue
        m = map_category(cat)
        if m:
            mapped.add(m)
    if title:
        t_low = title.lower()
        if re.search(r'\b(foursome|gangbang|group|orgy|orgies)\b', t_low):
            mapped.add("Group")
        if re.search(r'\b(threesome|3way|3-way)\b', t_low):
            mapped.add("Threesome")
    if not mapped and title:
        mapped = guess_categories_from_title(title)
    return mapped
