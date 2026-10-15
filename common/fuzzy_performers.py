#!/usr/bin/env python3
"""Match and merge alternate performer names.

Finds and merges performer name variants by category:
1. DIACRITICS (suggestion for manual review):
   Differences in diacritics / accents (e.g. Artyóm Ivanov vs Artyom Ivanov,
   Exequiel Rodríguez Balut vs Exequiel Rodriguez Balut).
2. SPACING_CASE_PUNCT (suggestion for manual review):
   Missing spaces, camelCase, letter case, dots, hyphens
   (e.g. Boston Jock vs BostonJock, Adam Tyme vs adam tyme, Curved King vs CurvedKing).
3. SUFFIX_NICKNAME (suggestion for manual review):
   Nicknames/suffixes without parentheses and attached digits/tags
   (e.g. Dillon Diaz VIP vs Dillon Diaz, AfroBlackXXX vs Afro Black,
   Briankinney7 vs Brian Kinney, HeightsHoundsXX vs Heights Hounds).
4. SIMILARITY (Confidence: 0.85-0.98 / Medium):
   Typos, missing/extra letters, high lexical similarity
   (e.g. Julian Cesar vs Jullian Cesar, Tripp Tallon vs Trip Tallon,
   Seb Leblan vs Seb Leblanc, Damian Dark vs Damien Dark).

CLI usage:
  python3 fuzzy_performers.py --report                 # Readable console report
  python3 fuzzy_performers.py --json                   # Report in JSON format
  # Merges are processed via the admin panel queue, handled by the sync container.
"""

import argparse
import hashlib  # noqa: F401 - fuzzy_merge resolves this from the facade globals
import json
import os
import re
import sys
import time  # noqa: F401 - fuzzy_merge resolves this from the facade globals
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from common.logging_setup import configure_logging

try:
    from common.db import Connection, connect, table_names
except ImportError:
    try:
        from db import Connection, connect, table_names  # type: ignore[no-redef]
    except ImportError:
        Connection = Any  # type: ignore[no-redef, misc]
        connect = None  # type: ignore[assignment]
        table_names = None  # type: ignore[assignment]

try:
    from common.performers import is_valid_performer_name, is_safe_alias
except ImportError:
    try:
        from performers import is_valid_performer_name, is_safe_alias
    except ImportError:
        def is_valid_performer_name(name: str) -> bool:
            return bool(name and len(name.strip()) >= 2)
        def is_safe_alias(alias: str, canonical: str = "") -> bool:
            return True

# Sciezki domyslne
DB_URL = os.getenv("DATABASE_URL", "")
MEDIA_DIR = Path(os.getenv("MEDIA_DIR", "./media/adult"))

# Modul sync

logger = configure_logging("fuzzy_performers")

# Common suffixes and markers in performer names
NOISE_SUFFIXES = [
    "onlyfans",
    "official",
    "model",
    "raw",
    "vip",
    "xxx",
    "xx",
    "ofc",
    "hd",
    "tv",
]


def strip_accents(text: str) -> str:
    """Usuwa znaki diakrytyczne / akcenty zachowujac litery bazowe."""
    if not text:
        return ""
    return "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    )


def normalize_clean_key(text: str) -> str:
    """Returns normalized key: no accents, lowercase, alphanumeric characters only."""
    if not text:
        return ""
    return re.sub(r"[^a-z0-9]", "", strip_accents(text).lower())


def _json_list(value: Any) -> list:
    """Read a JSON/JSONB list whether the driver returns decoded data or text."""
    if isinstance(value, list):
        return value
    if not isinstance(value, str) or not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def split_camel_or_delimiter(name: str) -> str:
    """Splits joined words camelCase, digits, or 'x' connector (e.g. JordanxBrandt -> Jordan Brandt)."""
    if not name:
        return ""
    # JordanxBrandt -> Jordan Brandt
    res = re.sub(r"([a-z])x([A-Z])", r"\1 \2", name)
    # BostonJock -> Boston Jock
    res = re.sub(r"([a-z])([A-Z])", r"\1 \2", res)
    # Ryan.Evans42 -> Ryan Evans 42
    res = re.sub(r"([a-zA-Z])(\d+)", r"\1 \2", res)
    # Delimitery kropka/podkreslenie/myslnik -> spacja
    res = re.sub(r"[\._\-]+", " ", res)
    return " ".join(res.split())


def extract_suffix_stem(name: str) -> Tuple[Optional[str], Optional[str]]:
    """Wykrywa i oddziela przyrostek szumu (np. VIP, XXX, doklejone cyfry).

    Zwraca (stem_name, suffix) lub (None, None).
    """
    clean_k = normalize_clean_key(name)
    if not clean_k or len(clean_k) < 4:
        return None, None

    # 1. Doklejony przyrostek szumu na koncu (np. AfroBlackXXX -> afroblack)
    for sfx in NOISE_SUFFIXES:
        if clean_k.endswith(sfx) and len(clean_k) > len(sfx) + 3:
            stem = clean_k[:-len(sfx)]
            return stem, sfx.upper()

    # 2. Doklejone cyfry na koncu (np. Briankinney7 -> briankinney)
    m_num = re.match(r"^(.*?)(\d+)$", clean_k)
    if m_num and len(m_num.group(1)) >= 4:
        return m_num.group(1), m_num.group(2)

    # 3. Oddzielne slowo na koncu bedace szumem (np. Dillon Diaz VIP -> dilliondiaz)
    m_word = re.match(
        r"^(.*?)\s+(?:vip|xxx|xx|official|model|raw|hd|tv|onlyfans)$",
        name.strip(),
        re.IGNORECASE,
    )
    if m_word:
        base = m_word.group(1).strip()
        stem_k = normalize_clean_key(base)
        if len(stem_k) >= 3:
            sfx_text = name.strip()[len(base):].strip().upper()
            return stem_k, sfx_text

    return None, None


def score_canonical(
    name: str,
    has_photo: bool,
    has_bio: bool,
    video_count: int,
    is_known_canonical: bool = False,
) -> float:
    """Ocenia kandydature imienia na nazwe kanoniczna."""
    score = 0.0
    if is_known_canonical:
        score += 200.0
    if has_bio:
        score += 100.0
    if has_photo:
        score += 80.0

    # Spacje miedzy czlonami (prawdziwe imie i nazwisko vs login/handle)
    words = name.split()
    if len(words) >= 2:
        score += 50.0

    # Prawidlowa wielkosc liter (Title Case)
    if name.istitle():
        score += 30.0
    elif name.islower():
        score -= 30.0
    elif name.isupper():
        score -= 20.0

    # Kary za obecnosc szumu na koncu
    k = normalize_clean_key(name)
    for sfx in NOISE_SUFFIXES:
        if k.endswith(sfx) and len(k) > len(sfx) + 2:
            score -= 40.0
            break
    if re.search(r"\d+$", name):
        score -= 35.0
    if re.search(r"[\?!_\.]", name):
        score -= 25.0

    # Library video count bonus
    score += min(video_count * 2.0, 60.0)

    # Preserve proper accents (e.g. Artyóm vs Artyom)
    if strip_accents(name) != name:
        score += 10.0

    return score


def find_fuzzy_performer_candidates(
    db_url: Optional[str] = None,
    similarity_threshold: float = 0.88,
) -> Dict[str, Any]:
    """Compatibility facade for the fuzzy performer candidate scan."""
    if __package__:
        from .fuzzy_matching import find_fuzzy_performer_candidates as implementation
    else:
        from fuzzy_matching import find_fuzzy_performer_candidates as implementation
    return implementation(db_url, similarity_threshold, _core=globals())


def make_backup(db_url: Optional[str] = None, backup_dir: Optional[Path] = None) -> Path:
    """Create a compressed PostgreSQL backup with pg_dump (see ``common.db``)."""
    try:
        from common.db import pg_backup
    except ImportError:
        from db import pg_backup  # type: ignore[no-redef]
    return Path(str(pg_backup(db_url or DB_URL or None, backup_dir,
                              prefix="adulthub-pre-fuzzy-merge")))


def trigger_jellyfin_refresh(url: Optional[str] = None, api_key: Optional[str] = None) -> bool:
    """Sends a Library/Refresh request to the Jellyfin API."""
    url = (url or os.getenv("JELLYFIN_URL", "")).strip()
    api_key = (api_key or os.getenv("JELLYFIN_API_KEY", "")).strip()

    if not url or not api_key:
        env_file = Path(__file__).parent / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.startswith("JELLYFIN_URL="):
                    url = line.split("=", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("JELLYFIN_API_KEY="):
                    api_key = line.split("=", 1)[1].strip().strip('"').strip("'")

    if not url or not api_key:
        logger.warning("JELLYFIN_URL / JELLYFIN_API_KEY are not configured")
        return False

    try:
        import urllib.request
        req = urllib.request.Request(
            f"{url.rstrip('/')}/Library/Refresh",
            headers={"Authorization": f'MediaBrowser Token="{api_key}"'},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            logger.info("Jellyfin Library/Refresh trigger: HTTP %s", resp.status)
            return resp.status in (200, 204)
    except Exception as e:
        logger.warning("Could not refresh Jellyfin: %s", e)
        return False


def _fuzzy_merge_implementation():
    if __package__:
        from . import fuzzy_merge
    else:
        import fuzzy_merge
    return fuzzy_merge


def preview_performer_merge(db_url: Optional[str], alias: str, canonical: str) -> Dict[str, Any]:
    return _fuzzy_merge_implementation().preview_performer_merge(
        db_url, alias, canonical, _core=globals(),
    )


def performer_merge_revision(conn: Connection, alias: str, canonical: str) -> str:
    return _fuzzy_merge_implementation().performer_merge_revision(conn, alias, canonical, _core=globals())


def _name_exists(conn: Connection, table: str, name: str) -> bool:
    return _fuzzy_merge_implementation()._name_exists(conn, table, name, _core=globals())


def execute_performer_merges(
    db_url: Optional[str],
    media_dir: Path,
    merges: List[Tuple[str, str]],
    skip_backup: bool = False,
) -> Dict[str, Any]:
    return _fuzzy_merge_implementation().execute_performer_merges(
        db_url, media_dir, merges, skip_backup=skip_backup, _core=globals(),
    )



def print_cli_report(data: Dict[str, Any]):
    """Prints a formatted text report in the terminal."""
    stats = data.get("stats", {})
    groups = data.get("groups", [])

    print("\n" + "=" * 78)
    print(" PERFORMER NAME MATCHING REPORT (ADULT HUB)")
    print("=" * 78)
    print(f" Unique performers in database: {stats.get('total_unique_performers', 0)}")
    print(f" Potential duplicate groups: {stats.get('total_groups', 0)}")
    print(f"   • Strong suggestions (review each pair): {stats.get('high_confidence_groups', 0)}")
    print(f"   • Medium confidence (review typos):  {stats.get('medium_confidence_groups', 0)}")
    print("-" * 78)

    categories = [
        ("diacritics", "1. DIACRITICS (ACCENTS)"),
        ("spacing_case_punct", "2. CASE / SPACING / DELIMITERS"),
        ("suffix_nickname", "3. SUFFIXES / NICKNAMES (VIP, XXX, DIGITS)"),
        ("similarity", "4. LEXICAL SIMILARITY (TYPOS)"),
    ]

    for cat_key, cat_title in categories:
        cat_groups = [g for g in groups if g["category"] == cat_key]
        if not cat_groups:
            continue

        print(f"\n{cat_title} ({len(cat_groups)} groups):")
        for g in cat_groups:
            canon = g["canonical"]
            v_count = g["canonical_video_count"]
            badge = "★ Canonical" if (g["canonical_has_photo"] or g["canonical_has_bio"]) else "Target"
            var_names = [f"'{v['name']}' ({v['video_count']}f)" for v in g["variants"]]
            print(f"  [{g['confidence_level'].upper()}] {badge}: '{canon}' ({v_count}f)  <--  Variants: {', '.join(var_names)}")

    print("\n" + "=" * 78)
    print("Review and apply merges from the admin panel; sync processes the durable job queue.")
    print("=" * 78 + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Match and merge alternate performer names."
    )
    parser.add_argument("--db-url", default=DB_URL, help="PostgreSQL URL (or DATABASE_URL env)")
    parser.add_argument("--media", default=str(MEDIA_DIR), help="Path to media directory")
    parser.add_argument("--threshold", type=float, default=0.88, help="Similarity threshold (0.80-0.95)")
    parser.add_argument("--report", action="store_true", help="Print report to console")
    parser.add_argument("--json", action="store_true", help="Print report in JSON format")
    args = parser.parse_args()

    # Analysis
    logger.info("Analyzing database (threshold: %.2f)...", args.threshold)
    analysis = find_fuzzy_performer_candidates(
        args.db_url or None, similarity_threshold=args.threshold
    )

    if not analysis.get("ok"):
        logger.error("Analysis failed: %s", analysis.get("error"))
        return 1

    if args.json:
        print(json.dumps(analysis, indent=2, ensure_ascii=False))
        return 0

    # Default or when --report is requested
    print_cli_report(analysis)
    return 0


if __name__ == "__main__":
    sys.exit(main())
