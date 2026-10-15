"""Static category vocabulary used by the mapping and normalization logic."""

from typing import Dict, List, Optional, Set

# Non-genre / junk tags that should be omitted entirely
EXCLUDED_TAGS: Set[str] = {
    "promotion",
    "uncategorized",
    "latest",
    "browse",
}

# The 24 canonical target categories
ALL_TARGET_CATEGORIES: List[str] = [
    "Amateur & OnlyFans",
    "Anal",
    "Asian & International",
    "Bareback & Breeding",
    "BDSM & Roleplay",
    "Bear & Chubby",
    "Big Dick & Size",
    "Black & Interracial",
    "Compilations & Media",
    "Cumshots",
    "Daddy & Mature",
    "Fetish & Kink",
    "Group",
    "Latino",
    "Massage & Shower",
    "Muscle & Jocks",
    "Oral & Rimming",
    "POV & Voyeur",
    "Public & Cruising",
    "Solo & Masturbation",
    "Straight & Bisexual",
    "Threesome",
    "Trans & Femboy",
    "Twink & Young",
]

# Explicit mapping of all known category variants and slugs
RAW_TO_MAPPED_CATEGORY: Dict[str, Optional[str]] = {
    # 1. Amateur & OnlyFans
    "amateur": "Amateur & OnlyFans",
    "homemade": "Amateur & OnlyFans",
    "onlyfans": "Amateur & OnlyFans",
    "tiktok": "Amateur & OnlyFans",
    "leaked": "Amateur & OnlyFans",
    "casting": "Amateur & OnlyFans",

    # 2. Anal
    "anal": "Anal",
    "ass play": "Anal",
    "flip fuck": "Anal",
    "double penetration": "Anal",
    "dp": "Anal",
    "duo": "Anal",

    # 3. Asian & International
    "asian": "Asian & International",
    "european": "Asian & International",

    # 4. Bareback & Breeding
    "bareback": "Bareback & Breeding",
    "breeding": "Bareback & Breeding",
    "creampie": "Bareback & Breeding",
    "internal cumshot": "Bareback & Breeding",

    # 5. BDSM & Roleplay
    "rough": "BDSM & Roleplay",
    "verbal": "BDSM & Roleplay",
    "role play": "BDSM & Roleplay",
    "roleplay": "BDSM & Roleplay",
    "uniforms": "BDSM & Roleplay",
    "uniform": "BDSM & Roleplay",
    "taboo": "BDSM & Roleplay",

    # 6. Bear & Chubby
    "bear": "Bear & Chubby",
    "chubby": "Bear & Chubby",
    "hairy": "Bear & Chubby",

    # 7. Big Dick & Size
    "big dick": "Big Dick & Size",
    "big cock": "Big Dick & Size",
    "big": "Big Dick & Size",
    "size kink": "Big Dick & Size",
    "size difference": "Big Dick & Size",
    "uncut": "Big Dick & Size",

    # 8. Black & Interracial
    "black": "Black & Interracial",

    # 9. Compilations & Media
    "compilation": "Compilations & Media",
    "compilations": "Compilations & Media",
    "shorts": "Compilations & Media",
    "short": "Compilations & Media",
    "4k": "Compilations & Media",
    "studio": "Compilations & Media",
    "animation": "Compilations & Media",

    # 10. Cumshots
    "cumshot": "Cumshots",
    "cumshots": "Cumshots",
    "oral cumshot": "Cumshots",
    "facial": "Cumshots",

    # 11. Daddy & Mature
    "daddy": "Daddy & Mature",
    "mature": "Daddy & Mature",
    "dadson": "Daddy & Mature",
    "old & young": "Daddy & Mature",
    "old and young": "Daddy & Mature",

    # 12. Fetish & Kink
    "fetish": "Fetish & Kink",
    "feet": "Fetish & Kink",
    "fisting": "Fetish & Kink",
    "poppers": "Fetish & Kink",
    "pissing": "Fetish & Kink",
    "caged": "Fetish & Kink",
    "sissy": "Fetish & Kink",
    "crossdressing": "Fetish & Kink",
    "worship": "Fetish & Kink",
    "toys": "Fetish & Kink",
    "toy": "Fetish & Kink",
    "tattoo": "Fetish & Kink",
    "tattoos": "Fetish & Kink",

    # 13. Group
    "group": "Group",
    "group sex": "Group",
    "foursome": "Group",
    "gangbang": "Group",
    "orgy": "Group",
    "orgies": "Group",

    # 14. Threesome
    "threesome": "Threesome",
    "trio": "Threesome",
    "3way": "Threesome",
    "3-way": "Threesome",

    # 14. Latino
    "latin": "Latino",
    "latino": "Latino",

    # 15. Massage & Shower
    "massage": "Massage & Shower",
    "shower": "Massage & Shower",

    # 16. Muscle & Jocks
    "muscle": "Muscle & Jocks",
    "hunk": "Muscle & Jocks",
    "jock": "Muscle & Jocks",
    "jocks": "Muscle & Jocks",
    "workout": "Muscle & Jocks",

    # 17. Oral & Rimming
    "oral": "Oral & Rimming",
    "blowjob": "Oral & Rimming",
    "deepthroat": "Oral & Rimming",
    "rimming": "Oral & Rimming",
    "rimjob": "Oral & Rimming",
    "self sucking": "Oral & Rimming",
    "selfsuck": "Oral & Rimming",
    "cum eating": "Oral & Rimming",

    # 18. POV & Voyeur
    "pov": "POV & Voyeur",
    "hidden camera": "POV & Voyeur",

    # 19. Public & Cruising
    "public": "Public & Cruising",
    "public sex": "Public & Cruising",
    "outdoor": "Public & Cruising",
    "outdoors": "Public & Cruising",
    "cruising": "Public & Cruising",
    "glory hole": "Public & Cruising",
    "gloryhole": "Public & Cruising",
    "anonymous": "Public & Cruising",

    # 20. Solo & Masturbation
    "solo": "Solo & Masturbation",
    "jerk off": "Solo & Masturbation",
    "handjob": "Solo & Masturbation",
    "edging": "Solo & Masturbation",

    # 21. Straight & Bisexual
    "straight man": "Straight & Bisexual",
    "straight": "Straight & Bisexual",
    "bisexual": "Straight & Bisexual",
    "bi": "Straight & Bisexual",

    # 22. Trans & Femboy
    "transman": "Trans & Femboy",
    "trans": "Trans & Femboy",
    "femboy": "Trans & Femboy",

    # 23. Twink & Young
    "twink": "Twink & Young",
    "youngtwink": "Twink & Young",
    "young": "Twink & Young",
    "college": "Twink & Young",
    "lad": "Twink & Young",

    # Telegram hashtag mappings & variants
    "of": "Amateur & OnlyFans",
    "jff": "Amateur & OnlyFans",
    "tt": "Amateur & OnlyFans",
    "meninosonline": "Amateur & OnlyFans",
    "butt": "Anal",
    "bdsm": "BDSM & Roleplay",
    "spanking": "BDSM & Roleplay",
    "hard": "BDSM & Roleplay",
    "hairywolf": "Bear & Chubby",
    "dotado": "Big Dick & Size",
    "dotadao": "Big Dick & Size",
    "dotadão": "Big Dick & Size",
    "irmaosdotados": "Big Dick & Size",
    "irmãosdotados": "Big Dick & Size",
    "bigcock": "Big Dick & Size",
    "bigdick": "Big Dick & Size",
    # Interracial & International pairings
    "b_w": "Black & Interracial",
    "l_w": "Black & Interracial",
    "b_l": "Black & Interracial",
    "b_l_w": "Black & Interracial",
    "b_a": "Black & Interracial",
    "l_w_a": "Black & Interracial",
    "b_w_b": "Black & Interracial",
    "l_w_l": "Black & Interracial",
    "b_a_b": "Black & Interracial",
    "interracial": "Black & Interracial",
    "w_a": "Asian & International",
    "l_a": "Asian & International",
    "l_a_l": "Asian & International",
    "a_w_a": "Asian & International",
    "asia": "Asian & International",
    "ru": "Asian & International",
    "retro": "Compilations & Media",
    "cross_g": "Daddy & Mature",
    "men": "Daddy & Mature",
    "fist": "Fetish & Kink",
    "foot": "Fetish & Kink",
    "piss": "Fetish & Kink",
    "fap": "Solo & Masturbation",
    "ts": "Trans & Femboy",
    "voyr": "POV & Voyeur",
    "hotboys": "Twink & Young",
    "cum": "Cumshots",

    # Explicit exclusions (neutral; web provider keys arrive via
    # register_provider_names() -> EXTRA_EXCLUDED and are checked below).
    "promotion": None,
    "uncategorized": None,
    "latest": None,
    "vídeo": None,
    "video": None,
    "fullhd": None,
    "ad": None,
    "selection": None,
    "sc": None,
    "ev": None,
}
