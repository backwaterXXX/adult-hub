"""Production studio detection and tagging for videos.

Scans video titles and existing metadata to detect known production
studios (producers/labels) and attaches them as tags (e.g. 'Studio: BelAmi')
without overwriting the 'studio' field (which stores the material source/channel).
"""
from __future__ import annotations

import re
from typing import Iterable, List, Optional, Set

# Canonical production studios and common variations/abbreviations
KNOWN_PRODUCTION_STUDIOS: dict[str, list[str]] = {
    "BelAmi": ["belami", "bel ami", "belamionline"],
    "Sean Cody": ["sean cody", "seancody"],
    "Men.com": ["men.com", "men com", "mencom"],
    "Corbin Fisher": ["corbin fisher", "corbinfisher"],
    "CockyBoys": ["cockyboys", "cocky boys"],
    "Helix Studios": ["helix studios", "helix"],
    "Falcon Studios": ["falcon studios", "falcon"],
    "Lucas Entertainment": ["lucas entertainment", "lucas", "lucas raunch"],
    "NextDoorStudios": ["nextdoorstudios", "next door studios", "nextdoor"],
    "Treasure Island Media": ["treasure island media", "treasure island"],
    "Raging Stallion": ["raging stallion", "ragingstallion"],
    "Active Duty": ["active duty", "activeduty"],
    "Cobra Video": ["cobra video", "cobravideo"],
    "Hot House": ["hot house", "hothouse"],
    "Kristen Bjorn": ["kristen bjorn", "kristenbjorn"],
    "Staxus": ["staxus"],
    "UK Naked Men": ["uk naked men", "uknakedmen"],
    "Broke Straight Boys": ["broke straight boys", "brokestraightboys"],
    "Chaos Men": ["chaos men", "chaosmen"],
    "Gay Room": ["gay room", "gayroom"],
    "Fratmen": ["fratmen", "frat men"],
    "Squirtz": ["squirtz"],
    "Blake Mason": ["blake mason", "blakemason"],
    "Titan Media": ["titan media", "titan"],
    "Dragon Media": ["dragon media"],
    "MenAtPlay": ["menatplay", "men at play", "men-at-play"],
    "Tim Tales": ["tim tales", "timtales"],
    "Latin Leche": ["latin leche", "latinleche"],
    "Bi Latin Men": ["bi latin men", "bilatinmen"],
    "Raw Fuck Club": ["raw fuck club", "rawfuckclub"],
    "Fraternity X": ["fraternity x", "fraternityx"],
    "Dark Alley Media": ["dark alley media", "dark alley"],
    "Cutler's Den": ["cutler's den", "cutlers den"],
    "Citebeur": ["citebeur"],
    "Macho Fucker": ["macho fucker", "machofucker"],
    "Bromo": ["bromo"],
    "Pride Studios": ["pride studios"],
    "Sketchy Sex": ["sketchy sex", "sketchysex"],
    "Eric Videos": ["eric videos"],
    "Dick Wadd": ["dick wadd"],
    "Wurstfilm": ["wurstfilm"],
    "FuckerMate": ["fuckermate", "fucker mate"],
    "Boys Halfway House": ["boys halfway house"],
    "Raw Road Nation": ["raw road nation"],
    "Irmaos Dotados": ["irmaos dotados", "irmãos dotados"],
}

# Precompile regex patterns for boundary-matched detection
_STUDIO_PATTERNS: list[tuple[str, re.Pattern]] = []
for canonical, variants in KNOWN_PRODUCTION_STUDIOS.items():
    sorted_variants = sorted(variants, key=len, reverse=True)
    esc = [re.escape(v) for v in sorted_variants]
    pat = re.compile(rf"(?i)\b(?:{'|'.join(esc)})\b")
    _STUDIO_PATTERNS.append((canonical, pat))

_DRAGON_PREFIX_RE = re.compile(r'(?i)^(?:\[dragon\]|dragon\s*[-–—])')
_RS_PREFIX_RE = re.compile(r'(?i)(?:^|\[|\()\s*rs\s*(?:\]|\)|[-–—])')


def detect_production_studios(title: str, existing_tags: Optional[Iterable[str]] = None) -> List[str]:
    """Detect known production studios from title and raw tags.

    Returns a list of formatted studio tags (e.g. ['Studio: BelAmi']).
    """
    detected: Set[str] = set()
    text_to_search = str(title or "")
    if existing_tags:
        text_to_search += " " + " ".join(str(t) for t in existing_tags if t)

    if not text_to_search.strip():
        return []

    for canonical, pattern in _STUDIO_PATTERNS:
        if pattern.search(text_to_search):
            detected.add(f"Studio: {canonical}")

    if _DRAGON_PREFIX_RE.search(str(title or "").strip()):
        detected.add("Studio: Dragon Media")

    if _RS_PREFIX_RE.search(str(title or "").strip()):
        detected.add("Studio: Raging Stallion")

    return sorted(detected)


def detect_production_studio_names(title: str, existing_tags: Optional[Iterable[str]] = None) -> List[str]:
    """Detect known production studios from title and raw tags.

    Returns canonical studio names (e.g. ['BelAmi']).
    """
    raw_studios = detect_production_studios(title, existing_tags)
    return [s.split("Studio: ", 1)[1].strip() for s in raw_studios if s.startswith("Studio: ")]
