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


def map_category(category: Optional[str]) -> Optional[str]:
    """Map a category name, slug or label to one of the 23 unified categories.

    Returns None if the category is excluded (e.g. provider name, junk tag, or invalid).
    """
    if not category:
        return None

    raw = category.strip()
    if not raw or raw.lower().startswith("browse"):
        return None

    # Strip leading '#' if present (Telegram hashtags: #bareback -> bareback)
    if raw.startswith("#"):
        raw = raw[1:].strip()
        if not raw:
            return None

    low = raw.lower()

    # Exclude technical/junk tags
    if low in EXCLUDED_TAGS:
        return None

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

    # Keyword heuristics for novel upstream categories
    if any(k in low for k in ("blowjob", "deepthroat", "rimming", "rimjob", "oral", "selfsuck")):
        return "Oral & Rimming"
    if any(k in low for k in ("bareback", "breeding", "creampie")):
        return "Bareback & Breeding"
    if any(k in low for k in ("cumshot", "facial")):
        return "Cumshots"
    if any(k in low for k in ("anal", "double penetration", "flip fuck")):
        return "Anal"
    if any(k in low for k in ("threesome", "trio", "3way", "3-way")):
        return "Threesome"
    if any(k in low for k in ("foursome", "gangbang", "group", "orgy", "orgies")):
        return "Group"
    if any(k in low for k in ("muscle", "hunk", "jock", "workout")):
        return "Muscle & Jocks"
    if any(k in low for k in ("bear", "chubby", "hairy")):
        return "Bear & Chubby"
    if any(k in low for k in ("daddy", "mature", "dadson")):
        return "Daddy & Mature"
    if any(k in low for k in ("twink", "young", "college", "lad")):
        return "Twink & Young"
    if any(k in low for k in ("big dick", "big cock", "size kink", "size diff")):
        return "Big Dick & Size"
    if any(k in low for k in ("onlyfans", "amateur", "homemade", "tiktok", "leaked")):
        return "Amateur & OnlyFans"
    if any(k in low for k in ("pov", "hidden camera")):
        return "POV & Voyeur"
    if any(k in low for k in ("cruising", "gloryhole", "glory hole", "outdoor", "public")):
        return "Public & Cruising"
    if any(k in low for k in ("fetish", "fisting", "poppers", "pissing", "sissy", "worship", "toy")):
        return "Fetish & Kink"
    if any(k in low for k in ("rough", "verbal", "roleplay", "role play", "uniform", "taboo")):
        return "BDSM & Roleplay"
    if any(k in low for k in ("massage", "shower")):
        return "Massage & Shower"
    if any(k in low for k in ("latino", "latin")):
        return "Latino"
    if any(k in low for k in ("interracial", "black")):
        return "Black & Interracial"
    if any(k in low for k in ("asian", "european")):
        return "Asian & International"
    if any(k in low for k in ("straight", "bisexual")):
        return "Straight & Bisexual"
    if any(k in low for k in ("trans", "femboy")):
        return "Trans & Femboy"
    if any(k in low for k in ("compilation", "shorts")):
        return "Compilations & Media"
    if any(k in low for k in ("solo", "jerk", "handjob", "edging")):
        return "Solo & Masturbation"

    return None


def guess_categories_from_title(title: str) -> Set[str]:
    """Infer relevant categories from a video title when upstream provider categories are absent."""
    if not title:
        return {"Amateur & OnlyFans"}
    t = title.lower()
    res = set()
    if re.search(r'\b(fuck|fucks|fucking|fucked|pounded|pounding|plowed|penetrat|ass|bottom|bottoms|bottoming|drilled|hole)\b', t):
        res.add('Anal')
    if re.search(r'\b(bareback|breed|breeds|breeding|bred|creampie|raw)\b', t):
        res.add('Bareback & Breeding')
    if re.search(r'\b(blowjob|deepthroat|bj|oral|sucking|sucks|suck|rim|rimming|lick|licking)\b', t):
        res.add('Oral & Rimming')
    if re.search(r'\b(cum|cums|cumming|cumshot|load|facial|swallow)\b', t):
        res.add('Cumshots')
    if re.search(r'\b(jerk|jerks|jerking|jerkoff|handjob|stroke|stroking|solo|masturbat|edge|edging)\b', t):
        res.add('Solo & Masturbation')
    if re.search(r'\b(threesome|trio|3way|3-way)\b', t):
        res.add('Threesome')
    if re.search(r'\b(foursome|gangbang|group|orgy|double dipping|dped)\b', t):
        res.add('Group')
    if re.search(r'\b(muscle|muscular|jock|hunk|gym|workout|bicep|bodybuilder)\b', t):
        res.add('Muscle & Jocks')
    if re.search(r'\b(bear|chubby|hairy|otter|beard|chub)\b', t):
        res.add('Bear & Chubby')
    if re.search(r'\b(daddy|mature|older|dilf|gramps|silver|father)\b', t):
        res.add('Daddy & Mature')
    if re.search(r'\b(twink|twinks|boy|boys|college|young|teen|lad)\b', t):
        res.add('Twink & Young')
    if re.search(r'\b(big|huge|thick|monster|giant|cock|dick|penis|endowed|size)\b', t):
        res.add('Big Dick & Size')
    if re.search(r'\b(dildo|toy|toys|feet|foot|fist|fisting|kink|fetish|leather|rubber|spank|bondage|cage|caged|poppers)\b', t):
        res.add('Fetish & Kink')
    if re.search(r'\b(onlyfans|amateur|homemade|tiktok|webcam|cam|leaked|live|ig)\b', t):
        res.add('Amateur & OnlyFans')
    if re.search(r'\b(pov)\b', t):
        res.add('POV & Voyeur')
    if re.search(r'\b(public|outdoor|outdoors|park|car|beach|cruising|forest|woods|gloryhole)\b', t):
        res.add('Public & Cruising')
    if re.search(r'\b(massage|shower|sauna|bath)\b', t):
        res.add('Massage & Shower')
    if re.search(r'\b(latino|latin)\b', t):
        res.add('Latino')
    if re.search(r'\b(black|ebony|interracial|bbc)\b', t):
        res.add('Black & Interracial')
    if re.search(r'\b(asian)\b', t):
        res.add('Asian & International')
    if re.search(r'\b(straight|bi|bisexual)\b', t):
        res.add('Straight & Bisexual')
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
