"""Shared deterministic performer-name helpers."""

import html
import re
from functools import lru_cache
from typing import Dict, Iterable, List, Optional


NATIONALITY_PREFIXES = (
    # Compound / multi-word first
    "african american", "american latino", "canadian latino", "cuban american",
    "cubano americano", "cuban-american", "uk latino", "middle eastern",
    "puerto rican", "puerto ricans", "amerian latinos", "siberian russian",
    "siberian-russian", "slovak-hungarian", "spanish-uruguayan",
    "venezolano-brasileiro", "venezuelan-brazilian", "french-portuguese",
    "italian-african american", "portuguese-algerian", "russian-latvian",
    "colombian-middle eastern", "asian-filipino",
    # Latin America
    "peruano", "peruanos", "peruana", "peruanas", "peruvian", "peruvians",
    "colombiano", "colombianos", "colombiana", "colombianas", "colombian", "colombians",
    "mexicano", "mexicanos", "mexicana", "mexicanas", "mexican", "mexicans",
    "argentino", "argentinos", "argentina", "argentinas", "argentine", "argentines", "argentinian", "argentinians", "argenitnos",
    "brasileiro", "brasileiros", "brasileira", "brasileiras", "brazilian", "brazilians", "brasileio", "brasilerio",
    "venezolano", "venezolanos", "venezolana", "venezolanas", "venezuelan", "venezuelans", "venezuolano",
    "chileno", "chilenos", "chilena", "chilenas", "chilean", "chileans",
    "cubano", "cubanos", "cubana", "cubanas", "cuban", "cubans",
    "dominicano", "dominicanos", "dominicana", "dominicanas", "dominican", "dominicans",
    "puertorriqueno", "puertorriquenos", "puertorriquena", "puertorriquenas",
    "uruguayo", "uruguayos", "uruguayan", "uruguayans",
    "paraguayo", "paraguayan", "boliviano", "bolivian", "ecuatoriano", "ecuadorian",
    "gringo", "gringos", "carioca", "cariocas",
    "latino", "latinos", "latina", "latinas", "latin",
    # Europe
    "espanol", "espanoles", "espanola", "espanolas", "spanish", "spaniard", "spaniards", "spansih",
    "italiano", "italianos", "italiana", "italianas", "italian", "italians",
    "portugues", "portugueses", "portuguesa", "portuguesas", "portuguese",
    "frances", "franceses", "francesa", "francesas", "french",
    "english", "englsih", "british", "britanico", "britanicos",
    "scottish", "scots", "scot", "escoces", "escoceses",
    "irish", "irlandes", "irlandeses", "welsh",
    "aleman", "alemanes", "alemana", "alemanas", "german", "germans",
    "austriaco", "austriacos", "austrian", "austrians", "suizo", "swiss",
    "belga", "belgas", "belgian", "belgians", "holandes", "holandeses", "dutch",
    "checo", "checos", "czech", "czechs",
    "eslovaco", "eslovacos", "slovak", "slovaks",
    "polaco", "polacos", "polaca", "polacas", "polish",
    "hungaro", "hungaros", "hungarian", "hungarians",
    "rumano", "rumanos", "romanian", "romanians", "bulgaro", "bulgarian", "bulgarians",
    "ruso", "rusos", "rusa", "rusas", "russian", "russians", "siberian",
    "ucraniano", "ucranianos", "ukrainian", "ukrainians",
    "griego", "griegos", "greek", "greeks",
    "sueco", "suecos", "swedish", "swedes",
    "noruego", "noruegos", "norwegian", "norwegians",
    "danes", "daneses", "danish", "finnish",
    "croata", "croatian", "serbio", "serbian",
    # North America
    "americano", "americanos", "americana", "americanas", "american", "americans", "amerian",
    "canadiense", "canadienses", "canadian", "canadians",
    # Asia & Oceania
    "asiatico", "asiaticos", "asiatica", "asiaticas", "asian", "asians",
    "vietnamita", "vietnamitas", "vietnamese", "vietnames",
    "tailandes", "tailandeses", "thai", "thais",
    "filipino", "filipinos", "filipina", "filipinas", "pinoy", "pinoys",
    "indonesio", "indonesian", "indonesians",
    "japones", "japoneses", "japonesa", "japanese",
    "chino", "chinos", "china", "chinas", "chinese",
    "coreano", "coreanos", "coreana", "korean", "koreans",
    "indio", "indios", "india", "indias", "indian", "indians", "pakistani",
    "turco", "turcos", "turkish", "turks",
    "israeli", "israelis",
    "arabe", "arabes", "arabian", "arabic", "arab", "arabs",
    "australiano", "australianos", "australian", "australians", "aussie", "aussies",
    # Africa
    "africano", "africanos", "africana", "african", "africans",
    "marroqui", "moroccan", "moroccans", "argelino", "algerian", "egipcio", "egyptian",
)

# Sort longest first to guarantee compound/longer prefixes match before shorter ones
NATIONALITY_PREFIXES = tuple(sorted(set(NATIONALITY_PREFIXES), key=len, reverse=True))

DEMONYM_DESCRIPTORS = {
    "hunk", "hunks", "jock", "jocks", "twink", "twinks", "twunk", "twunks",
    "bear", "bears", "otter", "otters", "daddy", "daddies", "pup", "pups",
    "guy", "guys", "boy", "boys", "muscle", "tatted",
}


@lru_cache(maxsize=16384)
def strip_nationality_prefix(name: str) -> str:
    """Strip leading nationality / demonym prefixes and optional following descriptors.

    E.g.
      'Peruvians JeffreyDair' -> 'JeffreyDair'
      'Peruvian Jeffreydair' -> 'Jeffreydair'
      'Vietnames hunk Kenvin' -> 'Kenvin'
      'Vietnamese hunk Kenvin' -> 'Kenvin'
      'Asian Hunk Alex Chou' -> 'Alex Chou'
      'Canadian Latino Kenzo Alvarez' -> 'Kenzo Alvarez'
      'Slovak-Hungarian Kyle Brady' -> 'Kyle Brady'
      'Englsih Paddy O'Brian' -> 'Paddy O'Brian'
    """
    if not name:
        return ""
    curr = name.strip()
    changed = True
    while changed:
        changed = False
        low = curr.lower()
        for prefix in NATIONALITY_PREFIXES:
            if low.startswith(prefix + " ") or low.startswith(prefix + "-"):
                rest = curr[len(prefix):].lstrip(" -").strip()
                if rest:
                    curr = rest
                    changed = True
                    break
        if changed:
            parts = re.split(r'[\s-]+', curr)
            while parts and len(parts) >= 2 and parts[0].lower() in DEMONYM_DESCRIPTORS:
                curr = curr[len(parts[0]):].lstrip(" -").strip()
                parts = re.split(r'[\s-]+', curr)
    return curr


def prune_redundant_performers(names: Iterable[str]) -> List[str]:
    """Remove only explicit nationality-prefixed duplicates.

    A single-word stage name can be a different person from someone whose
    full name contains that word. Overlap needs source-text spans to resolve.
    """
    if not names:
        return []
    normalized: List[str] = []
    for n in names:
        n_str = str(n).strip()
        if n_str and n_str not in normalized:
            normalized.append(n_str)
    if len(normalized) < 2:
        return normalized

    # Drop nationality-prefixed duplicates if bare performer is also present
    # (e.g. ['Colombiano Flaquito Vergon', 'Flaquito Vergon'] -> keep 'Flaquito Vergon')
    to_drop_nat = set()
    for n1 in normalized:
        bare = strip_nationality_prefix(n1)
        if bare != n1 and bare:
            if any(bare.lower() == n2.lower() for n2 in normalized):
                to_drop_nat.add(n1)
                continue
        n1_low = n1.lower()
        for prefix in NATIONALITY_PREFIXES:
            if n1_low.startswith(prefix + " ") or n1_low.startswith(prefix + "-"):
                b = n1[len(prefix):].lstrip(" -").strip()
                if any(b.lower() == n2.lower() for n2 in normalized):
                    to_drop_nat.add(n1)
                    break

    return [n for n in normalized if n not in to_drop_nat]

SCENE_SUFFIX_RE = re.compile(
    r'[\s._-]+(?:s|sc|scene|ep|episode|pt|part)[.\s_-]*\d+$',
    re.IGNORECASE,
)
STANDALONE_SCENE_RE = re.compile(
    r'^(?:s|sc|scene|ep|episode|pt|part)[.\s_-]*\d+$',
    re.IGNORECASE,
)


def strip_scene_suffix(name: str) -> str:
    """Strip trailing scene / season / episode / part numbers (e.g. 'Adam s1' -> 'Adam')."""
    if not name:
        return ""
    curr = name.strip()
    while True:
        stripped = SCENE_SUFFIX_RE.sub('', curr).strip()
        if stripped == curr:
            break
        curr = stripped
        if not curr:
            break
    return curr


def normalize_performers(names, alias_map: Optional[dict] = None, *, preserve_literal: bool = False) -> List[str]:
    """Deduplicate performer names, preserving first-seen order.

    Strips whitespace, unescapes HTML entities, strips parenthesized handles
    (e.g. 'Adam Snow (AdamSnowXXX)' -> 'Adam Snow'), strips trailing scene/part
    suffixes (e.g. 'Chris Blades s4' -> 'Chris Blades'), strips nationality/demonym
    prefixes and descriptors (e.g. 'Peruvian Jeffreydair' -> 'Jeffreydair',
    'Vietnamese hunk Kenvin' -> 'Kenvin'), maps aliases to canonical names when
    alias_map is provided, drops empty entries, and prunes explicit duplicates.
    Accepts None or any iterable; always returns a new list.
    """
    if preserve_literal:
        seen = set()
        literal = []
        for raw in names or []:
            if raw is None:
                continue
            name = html.unescape(str(raw)).strip()
            key = name.casefold()
            if name and key not in seen:
                seen.add(key)
                literal.append(name)
        return literal

    seen_lower: Dict[str, int] = {}
    out: List[str] = []
    for raw in names or []:
        if raw is None:
            continue
        name = html.unescape(str(raw)).strip()
        m = re.match(r"^(.*?)\s*[\(\[][^()\[\]]*[\)\]]\s*$", name)
        if m and m.group(1).strip():
            name = m.group(1).strip()
        name = strip_scene_suffix(name)
        if not name or STANDALONE_SCENE_RE.match(name):
            continue
        if alias_map:
            canonical = alias_map.get(name.lower()) or alias_map.get(name)
            if canonical and str(canonical).strip():
                c_str = str(canonical).strip()
                if c_str.lower() == name.lower() and not any(c.isupper() for c in c_str) and any(c.isupper() for c in name):
                    pass
                else:
                    name = c_str
        bare = strip_nationality_prefix(name)
        if bare != name:
            if not bare:
                continue
            bare = strip_scene_suffix(bare)
            if is_valid_performer_name(bare) or (alias_map and bare.lower() in alias_map):
                name = bare
                if alias_map:
                    canonical = alias_map.get(name.lower()) or alias_map.get(name)
                    if canonical and str(canonical).strip():
                        c_str = str(canonical).strip()
                        if not (c_str.lower() == name.lower() and not any(c.isupper() for c in c_str) and any(c.isupper() for c in name)):
                            name = c_str
            else:
                # Discard nationality prefix combined with non-performer descriptor (e.g. 'Korean Twunk', 'Mexican Republic')
                continue
        name = strip_scene_suffix(name)
        if name and not STANDALONE_SCENE_RE.match(name):
            name_low = name.lower()
            if (
                name_low in {"fucked", "fuck", "fucking", "fucker", "fuckers"}
                or name_low in SINGLE_WORD_STOPWORDS
                or MEDIA_EXT_RE.search(name)
            ):
                continue
            if name_low in seen_lower:
                idx = seen_lower[name_low]
                if not any(c.isupper() for c in out[idx]) and any(c.isupper() for c in name):
                    out[idx] = name
                continue
            seen_lower[name_low] = len(out)
            out.append(name)
    return prune_redundant_performers(out)

TITLE_STOPWORDS = {
    "part", "episode", "scene", "vol", "volume", "chapter",
    "hd", "4k", "720p", "1080p", "fhd", "uhd", "qhd", "sd", "remastered", "trailer", "bonus", "raw",
    "update", "updated", "our", "little", "secret", "file",
    "sunday", "funday", "special", "edition", "editions", "interview", "backstage",
    "behind", "scenes", "the", "a", "an", "and", "or", "in", "on", "at",
    "to", "for", "with", "from", "by", "of", "about",
    "fucks", "fuck", "fucking", "fucked", "fucker", "fuckers",
    "cock", "dick", "ass", "hole", "cum", "suck", "sucking", "sucked",
    "hunting", "master", "apartment", "preview",
    "cut", "cuts", "editor", "editors", "director", "directors", "extended",
    "version", "versions", "compilation", "remix", "webrip", "rip",
    "upload", "uploader",
    "bareback", "barebacked", "creampie", "creampied", "breed", "breeds", "breeding",
    "bottom", "bottoms", "top", "tops", "erection",
    "rs", "xl", "xxl", "xxxl", "nailed", "cocksucker", "onlyfans", "justforfans",
    "sex", "gay", "twunk", "twunks", "republic", "bb", "ff",
}

SINGLE_WORD_STOPWORDS = {
    "rs", "xl", "xxl", "xxxl", "dad", "daddy", "daddies", "stepdad", "stepdads", "papa",
    "brother", "uncle", "son", "doctor", "friend", "neighbor", "suite", "bunk",
    "hard", "wet", "out", "mix", "pup", "bred", "who", "all", "get", "got", "can",
    "boy", "boys", "man", "men", "guy", "guys", "bear", "bears", "jock", "jocks",
    "twink", "twinks", "twunk", "twunks", "otter", "otters", "hunk", "hunks", "top", "tops", "bottom", "bottoms",
    "vers", "load", "loads", "fist", "fisting", "orgy", "gangbang", "duo", "trio", "solo",
    "raw", "bareback", "creampie", "dp", "dps", "studio", "studios", "amateur",
    "series", "onlyfans", "justforfans", "fan", "fans", "site", "web", "group", "fit",
    "hunter", "red", "nailed", "cocksucker", "editor", "cut", "preview", "trailer",
    "dj", "jc", "jd", "tj", "ok", "dim", "sim", "rew",
    "sex", "gay", "republic", "bb", "ff",
    "hd", "fhd", "uhd", "qhd", "sd", "4k", "720p", "1080p",
    # Studio names and labels — not performer mononyms
    "lucas", "falcon", "corbin", "sean", "tim", "bunker", "keptsecret", "frisky",
    "staxus", "eurocreme", "helix", "cockyboys", "cazzo", "menatplay", "nextdoorstudios",
    "boundgods", "dragon", "putinho", "gaycest", "voyr", "rfcc", "adulttime",
    # Nationality/ethnicity descriptors — not performer names
    "aussie", "aussies", "black", "carioca", "brasileiros", "brasileiro", "polaco", "venezuelan",
    "venezuelans", "venezolano", "venezolanos", "colombiano", "colombianos", "peruano",
    "peruanos", "brazilian", "brazilians", "mexicano", "mexicanos", "argentino", "argentinos",
    "espanol", "espanoles", "spanish", "italian", "italiano", "italianos", "american",
    "americans", "portuguese", "french", "english", "british", "latino", "latinos",
    "russian", "chilean", "chileans", "cuban", "cubans", "german", "germans", "west", "latin",
    "arab", "arabs", "asian", "asians", "czech", "czechs", "slovak", "slovaks",
    "hungarian", "hungarians", "polish", "ukrainian", "ukrainians", "greek", "greeks",
    "turkish", "turks", "irish", "scottish", "dutch", "belgian", "belgians", "swedish",
    "norwegian", "norwegians", "danish", "finnish", "thai", "thais", "filipino", "filipinos",
    "vietnamese", "vietnames", "canadian", "canadians", "uruguayan", "uruguayans",
    "paraguayan", "bolivian", "ecuadorian",
    # Category / action / title terms polluting performer extractions
    "handjob", "hardcore", "massage", "threesome", "cumshot", "party", "horny",
    "session", "enough", "bare", "pound", "pounds", "spunk", "used", "butt",
    "down", "fest", "flood", "hide", "seek", "service", "addiction", "again",
    "away", "birthday", "breakfast", "blur", "knockout", "otro", "real", "good",
    "dads", "bang", "fuckin", "buddy", "initiate", "stranger", "boned", "inseminate",
    "need", "club", "talvez", "bunkmate", "stooge", "pulse", "loud", "feeding",
    "penthouse", "pigboy",
    # First name / common single words that are not performer mononyms
    "jordan", "connor",
}

STUDIO_NAMES = {
    "lucas entertainment", "naked sword", "nakedsword", "corbin fisher",
    "sean cody", "helix studios", "falcon studios", "treasure island media",
    "dark alley media", "raging stallion", "raging stallion studios",
    "bel ami", "latin leche", "bi latin men", "men over 30", "menover30",
    "menatplay", "men at play", "cocky boys", "cockyboys", "chaos men", "chaosmen",
    "disruptive films", "boundgods", "bound gods", "kink men", "next door studios",
    "nextdoorstudios", "next door buddies", "raw fuck club", "prime pork",
    "cazzo club", "cazzo film", "staxus", "eurocreme", "tim tales", "timtales", "the bunker",
    "keptsecret", "keptsecretxxx", "kept secret", "frisky fans",
    "trailer trash boys", "raw road nation", "latin boys", "latinboys",
    "breed me raw", "fuckermate", "fucker mate", "noir male", "hot house", "hothouse",
    "dragon", "dragon media", "rawfuckclub", "lucas ent",
    "eric videos", "ericvideos", "kristen bjorn", "william higgins",
    "randy blue", "randyblue", "icon male", "iconmale",
    "xvideos red", "xvideosred", "xvideos", "meninos online",
    "masqulin", "maskurbate", "citebeur", "alphamales",
    "seancody", "nextdoor", "cazzo", "bromo",
    "belamionline", "disruptivefilms", "bilatinmen",
    "putinho videos", "putinho vídeos", "putinho",
    "putinho vídeos gays sexo", "putinho videos gays sexo",
    "vídeos gays sexo", "videos gays sexo",
    "adulttime", "adult time",
    "whynotbi", "why not bi",
    "rawstrokes",
    "dreamboybondage", "dream boy bondage",
    "meninosonline",
    "gaycest archive", "gaycest",
    "voyr",
    "rfcc",
    "fkm", "fsm",
    "mol", "lb",
}

STUDIO_SUFFIXES = (
    " studio", " studios", " entertainment", " media", " films",
    " productions", " production",
)

SCENE_CODE_RE = re.compile(
    r"^(?:[A-Z]{2,4}\d{2,}|SC\d+|MO\d+|CFS\d+|ACM\d+|CAPY[-_]?\d+|[A-Z]+\d+[-_]\d+)$",
    re.IGNORECASE,
)

MEDIA_EXT_RE = re.compile(r"\.(?:mp4|mkv|avi|mov|wmv|flv|webm|m4v|ts|mpg|mpeg)(?:\b|$)", re.IGNORECASE)
MEDIA_EXT_SUFFIX_RE = re.compile(
    r"(\.(?:part\d+|720p|1080p|2160p|4k))?\.(?:mp4|mkv|avi|mov|wmv|flv|webm|m4v|ts|mpg|mpeg)$",
    re.IGNORECASE,
)
MEDIA_EXT_WORDS = {"mp4", "mkv", "wmv", "webm", "flv", "m4v"}

PARTICLES = {"the", "de", "von", "la", "van", "del", "da", "di"}

ACTION_VERBS = r"(?:flip fucks|flip fuck|flipfuck\w*|fucked by|fuck & DP|fucks & DPs|fucks|fuck|fucked|breeds with|breeds|breed|bottoms for|bottoms|takes|rides|dp\'s|dps|dpd|dp)"

_NON_PERFORMER_NAME_KEYS = {"onlyfans", "of"}


def is_non_performer_name(name: str) -> bool:
    """Return true for exact platform labels that can appear in credits."""
    key = re.sub(r"[^a-z0-9]", "", str(name or "").casefold())
    return key in _NON_PERFORMER_NAME_KEYS


def is_valid_performer_name(name: str) -> bool:
    """Validate that candidate string looks like a performer name (not title/words)."""
    name = (name or "").strip()
    if not name or len(name) < 2:
        return False
    if is_non_performer_name(name):
        return False
    if any(c in name for c in "()[]{}<>"):
        return False
    if MEDIA_EXT_RE.search(name):
        return False
    clean_name = re.sub(r'^[^\w]+|[^\w]+$', '', name)
    if clean_name != name:
        return False
    clean_low = clean_name.lower()
    if clean_low in STUDIO_NAMES or clean_low in SINGLE_WORD_STOPWORDS:
        return False
    if any(clean_low.endswith(sfx) for sfx in STUDIO_SUFFIXES):
        return False
    if SCENE_CODE_RE.match(clean_name):
        return False
    if SCENE_SUFFIX_RE.search(clean_name) or STANDALONE_SCENE_RE.match(clean_name):
        return False
    words = clean_name.split()
    if not (1 <= len(words) <= 4):
        return False
    if len(words) == 1:
        if len(clean_name) <= 2:
            return False
        if clean_low in SINGLE_WORD_STOPWORDS:
            return False
    for i, w in enumerate(words):
        clean_w = re.sub(r'^[^\w]+|[^\w]+$', '', w)
        if not clean_w:
            return False
        w_low = clean_w.lower()
        if len(words) >= 2 and w_low in PARTICLES:
            continue
        w_base = re.sub(r"['’‘]s$", "", w_low)
        w_alnum = re.sub(r"[^a-z0-9]", "", w_low)
        w_base_alnum = re.sub(r"[^a-z0-9]", "", w_base)
        if (
            w_low in TITLE_STOPWORDS
            or w_base in TITLE_STOPWORDS
            or w_alnum in TITLE_STOPWORDS
            or w_base_alnum in TITLE_STOPWORDS
            or w_low in MEDIA_EXT_WORDS
            or w_alnum in MEDIA_EXT_WORDS
            or STANDALONE_SCENE_RE.match(clean_w)
        ):
            return False
        if clean_w.isdigit():
            return False
        # Must start with uppercase or be an alphanumeric code like 23CM / XXX or word with letters in multi-word name
        if not (clean_w[0].isupper() or (len(clean_w) > 1 and clean_w.isupper()) or (len(words) >= 2 and words[0][0].isupper() and any(c.isalpha() for c in clean_w))):
            return False
    return True

def is_performer_segment(text: str, performers: Iterable[str]) -> bool:
    """Check if a text segment consists exclusively of performer names from `performers`."""
    if not text or not performers:
        return False
    t_clean = text.strip()
    if (t_clean.startswith("[") and t_clean.endswith("]")) or (t_clean.startswith("(") and t_clean.endswith(")")):
        return False
    members = [m.strip() for m in re.split(r'[,&]|\b[aA][nN][dD]\b', text) if m.strip()]
    if not members:
        return False
    perfs_lower = [p.lower() for p in performers]

    def member_matches(m: str) -> bool:
        m_clean = strip_scene_suffix(m)
        m_low = m_clean.lower()
        if m_low in perfs_lower:
            return True
        bare_m = strip_nationality_prefix(m_clean)
        bare_m_low = bare_m.lower()
        if bare_m_low in perfs_lower:
            return True
        for p in perfs_lower:
            if strip_nationality_prefix(p).lower() in (m_low, bare_m_low):
                return True
        for nat in NATIONALITY_PREFIXES:
            if m_low.startswith(nat + " ") and m_low[len(nat):].strip() in perfs_lower:
                return True
            if any(p.startswith(nat + " ") and p[len(nat):].strip() == m_low for p in perfs_lower):
                return True
        return False

    return all(member_matches(m) for m in members)


def is_safe_alias(alias: str, canonical: str) -> bool:
    """Validate that alias is safe to store in performer_aliases table.

    Rejects generic single-word names (e.g. 'leon', 'jack', 'matt', 'chris', 'dave', 'tom')
    which cause false-positive performer attribution across unrelated videos.
    Multi-word aliases and distinctive social handles (with digits, punctuation, or
    suffixes like xxx/vip) are permitted.
    """
    a = (alias or "").strip().lower()
    c = (canonical or "").strip()
    if is_non_performer_name(a) or is_non_performer_name(c):
        return False
    if not a or not c or a == c.lower():
        return False
    if not is_valid_performer_name(c) or SCENE_SUFFIX_RE.search(c) or STANDALONE_SCENE_RE.match(c):
        return False
    if any(ch in c for ch in "()[]{}<>"):
        return False
    if any(ch in a for ch in "[]{}<>"):
        return False
    if a in SINGLE_WORD_STOPWORDS or a in TITLE_STOPWORDS:
        return False
    parts = a.split()
    if len(parts) >= 2:
        return True
    # Allow single-word aliases that are concatenated forms of multi-word canonical names
    # (e.g. 'bostonjock' for 'Boston Jock', 'tripptallon' for 'Tripp Tallon')
    c_parts = c.split()
    if len(c_parts) >= 2 and re.sub(r'[^a-z0-9]', '', a) == re.sub(r'[^a-z0-9]', '', c.lower()):
        return True
    # Single-word alias: only allow distinct handles (contains digits, punctuation ._-,
    # suffixes like xxx/vip/ofc, or length >= 12)
    if re.search(r'[\d._\-]', a) or a.endswith(('xxx', 'xx', 'vip', 'ofc')) or len(a) >= 12:
        return True
    return False
