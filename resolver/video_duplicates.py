"""Conservative duplicate suggestions and durable review decisions."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from common import db
from collections import defaultdict
from urllib.parse import urlsplit
from common.duplicate_evidence import extract_part_identity
from common.job_queue import enqueue_job, retry_job  # noqa: F401 - compatibility export used by Admin routes

from video_duplicate_merge_history import (
    _decision_conn as _decision_conn_impl,
    find_active_oshash_merges as _find_active_oshash_merges_impl,
)

from video_duplicate_scoring import (
    EPISODE_PART_SCORE_PENALTY as EPISODE_PART_SCORE_PENALTY,
    MAX_HEURISTIC_CANDIDATES as MAX_HEURISTIC_CANDIDATES,
    MIN_SCORE as MIN_SCORE,
    PHASH_NEAR_MAX_DISTANCE as PHASH_NEAR_MAX_DISTANCE,
    PHASH_NEAR_SOLO_MAX_DURATION_DELTA as PHASH_NEAR_SOLO_MAX_DURATION_DELTA,
    PHASH_NEAR_SOLO_TITLE_SIMILARITY as PHASH_NEAR_SOLO_TITLE_SIMILARITY,
    RULES_VERSION as RULES_VERSION,
    _candidate_pairs as _candidate_pairs,
    _clean_title_for_solo as _clean_title_for_solo,
    _duration_values as _duration_values,
    _dismissals_for_group as _dismissals_for_group,
    _has_different_season_markers as _has_different_season_markers,
    _index_dismissals as _index_dismissals,
    _names as _names,
    _normalize_name as _normalize_name,
    _prepare as _prepare,
    _score as _score,
    _score_solo as _score_solo,
    _telegram_file_id as _telegram_file_id,
)

logger = logging.getLogger(__name__)


def _public_row(row: dict) -> dict:
    source_url = row.get("source_url", "")
    if not source_url and row.get("provider") == "telegram":
        video_id = str(row.get("id") or "")
        parts = video_id[len("telegram:"):].rsplit(":", 2) if video_id.startswith("telegram:") else []
        if (len(parts) == 3 and parts[0].lstrip("-").isdigit()
                and parts[1].isascii() and parts[1].isdigit()):
            channel = parts[0][4:] if parts[0].startswith("-100") else parts[0].lstrip("-")
            source_url = f"https://t.me/c/{channel}/{parts[1]}"
    try:
        parsed_url = urlsplit(str(source_url))
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            source_url = ""
    except ValueError:
        source_url = ""
    try:
        from admin_library_v2 import _safe_thumbnail
        thumbnail = _safe_thumbnail(row.get("thumbnail"))
    except Exception:
        thumbnail = None
    return {
        "id": str(row.get("id") or ""), "title": str(row.get("title") or ""),
        "provider": str(row.get("provider") or ""), "duration": row.get("duration"),
        "oshash": row.get("oshash"), "phash": row.get("phash"),
        "studio": str(row.get("studio") or ""),
        "thumbnail": thumbnail,
        "performers": sorted(row.get("performer_display", {}).values(), key=str.casefold),
        "source_url": source_url,
    }


DUPLICATE_CATEGORY_FILTERS = {
    "same_file": "Same file",
    "strong": "Strong suggestion",
    "review": "Needs review",
}


@dataclass
class _DuplicateInputs:
    rows: list[dict]
    scanned: int
    aliases: dict[str, str]
    oshashes: dict[str, str]
    phashes: dict[str, str]
    decisions: set
    dismissed: set
    dismissed_by_video: dict


def _select_columns(cols: set[str]) -> list[str]:
    optional = {"provider": "''", "title": "''", "slug": "''", "duration": "NULL",
                "performers": "'[]'", "studio": "''", "thumbnail": "NULL",
                "scene_match_source": "''", "scene_match_external_id": "NULL",
                "source_text": "''", "source_filename": "''"}
    select = ["id"] + [n if n in cols else f"{fallback} AS {n}" for n, fallback in optional.items()]
    if "url" in cols:
        select.append("url AS source_url")
    elif "source_url" in cols:
        select.append("source_url")
    else:
        select.append("'' AS source_url")
    return select


def _load_performer_aliases(conn, tables_available: set[str]) -> dict[str, str]:
    if "performer_aliases" not in tables_available:
        return {}
    choices: dict[str, set[str]] = defaultdict(set)
    for alias, canonical in conn.execute("SELECT alias, canonical FROM performer_aliases"):
        alias_norm, canonical_norm = _normalize_name(str(alias or "")), _normalize_name(str(canonical or ""))
        if alias_norm and canonical_norm:
            choices[alias_norm].add(canonical_norm)
    aliases = {alias: next(iter(canonicals)) for alias, canonicals in choices.items()
               if len(canonicals) == 1}
    for canonicals in choices.values():
        for canonical in canonicals:
            aliases.setdefault(canonical, canonical)
    return aliases


def _load_fingerprints(conn, tables_available: set[str]) -> tuple[dict[str, str], dict[str, str]]:
    oshashes: dict[str, str] = {}
    phashes: dict[str, str] = {}
    if "video_fingerprints" not in tables_available or "video_phash_provenance" not in tables_available:
        return oshashes, phashes
    current_phash_generator = (
        "stash-phash-689a6b400625d6da2afb58a7f908c88bcdb6f8ab-v2:"
        "resolver-ffmpeg-stash-v2"
    )
    for video_id, algorithm, fingerprint in conn.execute(
        """SELECT video_id,algorithm,fingerprint FROM video_fingerprints WHERE algorithm='OSHASH'
           UNION ALL
           SELECT f.video_id,f.algorithm,f.fingerprint FROM video_fingerprints f
           JOIN video_phash_provenance p ON p.video_id=f.video_id
             AND p.fingerprint=f.fingerprint AND p.generator_version=%s
           WHERE f.algorithm='PHASH'""", (current_phash_generator,)
    ).fetchall():
        value = str(fingerprint or "").strip().lower()
        if not video_id or not re.fullmatch(r"[0-9a-f]{16}", value):
            continue
        target = oshashes if algorithm == "OSHASH" else phashes if algorithm == "PHASH" else None
        if target is not None:
            target[str(video_id)] = value
    return oshashes, phashes


def _load_duplicate_inputs(db_url: str | None, category: str) -> tuple[_DuplicateInputs | None, bool]:
    """Read catalog rows and decision state. Returns ``(None, scan_complete)`` on early exit."""
    try:
        conn = db.connect(db_url)
    except Exception:
        logger.debug("Ignored exception in _collect_duplicate_candidates", exc_info=True)
        return None, False
    conn.row_factory = db.Record
    try:
        cols = set(db.table_columns(conn, "videos"))
        if "id" not in cols:
            return None, True
        select = _select_columns(cols)
        # A same-file candidate can only be proven from Telegram provider IDs.
        # Keep the full catalog count for API diagnostics, but avoid decoding,
        # normalizing, and indexing unrelated providers for this category.
        only_telegram = category == "Same file" and "provider" in cols
        if only_telegram:
            scanned = int(conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0])
            query = f"SELECT {', '.join(select)} FROM videos WHERE provider='telegram' ORDER BY id"
        else:
            query = f"SELECT {', '.join(select)} FROM videos ORDER BY id"
        rows = [dict(r) for r in conn.execute(query)]
        if not only_telegram:
            scanned = len(rows)
        tables_available = db.table_names(conn)
        aliases = _load_performer_aliases(conn, tables_available)
        oshashes, phashes = _load_fingerprints(conn, tables_available)
        for row in rows:
            video_id = str(row.get("id") or "")
            row["oshash"] = oshashes.get(video_id)
            row["phash"] = phashes.get(video_id)
        # Decision tables may not exist until the first sync migration.
        decisions = set()
        if "video_duplicate_decisions" in tables_available:
            decisions = {r[0] for r in conn.execute(
                "SELECT loser_id FROM video_duplicate_decisions WHERE state='active'")}
        dismissed = set()
        if "video_duplicate_dismissals" in tables_available:
            dismissed = {tuple(sorted((r[0], r[1]))) for r in conn.execute(
                "SELECT left_id, right_id FROM video_duplicate_dismissals WHERE active=1")}
        dismissed_by_video = _index_dismissals(dismissed)
    finally:
        conn.close()
    inputs = _DuplicateInputs(
        rows=rows, scanned=scanned, aliases=aliases, oshashes=oshashes, phashes=phashes,
        decisions=decisions, dismissed=dismissed, dismissed_by_video=dismissed_by_video,
    )
    return inputs, True


def _group_rows(inputs: _DuplicateInputs):
    """Index rows by exact identity keys (Telegram file, OSHASH, pHash+duration, scene ID+duration)."""
    rows, oshashes, phashes = inputs.rows, inputs.oshashes, inputs.phashes
    file_groups: dict[str, list[int]] = defaultdict(list)
    hash_groups: dict[str, list[int]] = defaultdict(list)
    phash_groups: dict[str, list[int]] = defaultdict(list)
    scene_groups: dict[str, list[int]] = defaultdict(list)
    for i, row in enumerate(rows):
        fid = _telegram_file_id(row)
        if fid is not None:
            file_groups[fid].append(i)
        oshash = oshashes.get(str(row.get("id") or ""))
        if row.get("provider") == "telegram" and oshash:
            hash_groups[oshash].append(i)
        phash = phashes.get(str(row.get("id") or ""))
        if phash and not str(row.get("scene_match_source") or "").casefold().endswith(
                "split_title_duration_v1"):
            duration = _duration_values(row)
            if duration is not None:
                phash_groups[f"{phash}:{duration}"].append(i)
        scene_id = str(row.get("scene_match_external_id") or "").strip()
        source = str(row.get("scene_match_source") or "").casefold()
        if scene_id and not source.endswith("split_title_duration_v1"):
            provider = "theporndb" if "theporndb" in source or "tpdb" in source else "stashdb" if "stashdb" in source else ""
            if provider:
                duration = _duration_values(row)
                if duration is not None:
                    scene_groups[f"{provider}:{scene_id}:{duration}"].append(i)
    return file_groups, hash_groups, phash_groups, scene_groups


def _shared_values(members: list[int], rows: list[dict], lookup: dict[str, str]) -> list[str]:
    """Fingerprints that more than one member carries, sorted."""
    counts: dict[str, int] = defaultdict(int)
    for i in members:
        fingerprint = lookup.get(str(rows[i]["id"]))
        if fingerprint:
            counts[fingerprint] += 1
    return sorted(fingerprint for fingerprint, count in counts.items() if count > 1)


def _representative_pair(rows: list[dict], members: list[int], dismissed: set) -> tuple[int, int] | None:
    """First member pair, in order, that has not been dismissed."""
    for left_pos, left_i in enumerate(members):
        for right_i in members[left_pos + 1:]:
            ids = tuple(sorted((rows[left_i]["id"], rows[right_i]["id"])))
            if ids not in dismissed:
                return left_i, right_i
    return None


def _exact_group_warnings(member_rows: list[dict], file_id: str | None) -> list[str]:
    warnings = []
    durations = [duration for row in member_rows if (duration := _duration_values(row)) is not None]
    if len(set(durations)) > 1:
        warnings.append(f"Duration differs across messages ({min(durations)}–{max(durations)} s)")
    casts = {frozenset(row["performer_norm"]) for row in member_rows if row["performer_norm"]}
    if len(casts) > 1:
        warnings.append("Cast differs across messages")
    if file_id and any(row.get("slug") and str(row["slug"]) != file_id for row in member_rows):
        warnings.append("Inconsistent metadata: slug differs from file ID in ref")
    return warnings


def _shared_stashdb_scene(member_rows: list[dict]) -> bool:
    shared_scene_ids = {
        str(row.get("scene_match_external_id") or "").strip()
        for row in member_rows
        if str(row.get("scene_match_source") or "").casefold().startswith("stashdb")
    }
    return (
        len(shared_scene_ids) == 1 and "" not in shared_scene_ids
        and all(str(row.get("scene_match_source") or "").casefold().startswith("stashdb")
                for row in member_rows)
    )


def _exact_group_record(inputs: _DuplicateInputs, members: list[int], group_dismissals) -> dict | None:
    """Build the review card for one exact-identity group, or None if every pair was dismissed."""
    rows, oshashes, phashes = inputs.rows, inputs.oshashes, inputs.phashes
    representative = _representative_pair(rows, members, inputs.dismissed)
    if representative is None:
        return None
    base = rows[representative[0]]
    member_file_ids = {_telegram_file_id(rows[i]) for i in members}
    member_file_ids.discard(None)
    shared_oshashes = _shared_values(members, rows, oshashes)
    same_telegram_file = len(member_file_ids) == 1
    file_id = next(iter(member_file_ids)) if same_telegram_file else None
    member_rows = [rows[i] for i in members]
    warnings = _exact_group_warnings(member_rows, file_id)
    shared_phashes = _shared_values(members, rows, phashes)
    has_shared_stashdb_scene = _shared_stashdb_scene(member_rows)
    if len(shared_phashes) == 1:
        reasons = ["Matching pHash across videos; automatic match requires runtime and part checks"]
    elif len(member_file_ids) > 1 and shared_oshashes:
        reasons = ["Matching OSHASH fingerprint across Telegram messages"]
    else:
        reasons = ["Same Telegram file, different messages"]
    same_file = same_telegram_file
    return {
        "left": _public_row(base), "right": _public_row(rows[representative[1]]),
        "members": [_public_row(row) for row in member_rows],
        "dismissed_pairs": [list(pair) for pair in sorted(group_dismissals)],
        "score": 100.0 if same_file else 92.0, "score_max": 100,
        "category": "Same file" if same_file else "Strong suggestion",
        "evidence": "telegram_file_id" if same_telegram_file else
                    "phash" if len(shared_phashes) == 1 else
                    "stashdb_scene" if has_shared_stashdb_scene
                    else "oshash",
        "rules_version": RULES_VERSION,
        "components": ({"telegram_file_id": file_id} if same_telegram_file else
                       {"phash": shared_phashes[0]} if len(shared_phashes) == 1 else
                       {"oshashes": shared_oshashes} if shared_oshashes else {}),
        "reasons": [*reasons, *sorted(set(warnings))],
    }


def _connected_components(row_count: int, file_groups, hash_groups, phash_groups,
                          scene_groups, category: str) -> dict[int, list[int]]:
    """Union exact-identity groups into components, keyed by root index."""
    # Group repeated Telegram IDs as proven same-file entries. Shared OSHASH
    # values join the same review group but remain suggestions because OSHASH
    # is not a cryptographic identity. Unioning keys avoids duplicate cards.
    parents = list(range(row_count))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(indexes: list[int]) -> None:
        if len(indexes) < 2:
            return
        root = find(indexes[0])
        for index in indexes[1:]:
            other = find(index)
            if other != root:
                parents[other] = root

    exact_group_indexes = list(file_groups.values())
    if category != "Same file":
        exact_group_indexes.extend(hash_groups.values())
        exact_group_indexes.extend(phash_groups.values())
        exact_group_indexes.extend(scene_groups.values())
    for indexes in exact_group_indexes:
        union(indexes)

    components: dict[int, list[int]] = defaultdict(list)
    grouped_indexes = {index for indexes in exact_group_indexes for index in indexes}
    for index in sorted(grouped_indexes):
        components[find(index)].append(index)
    return components


def _exact_candidates(inputs: _DuplicateInputs, category: str) -> tuple[list[dict], set[int]]:
    """Group exact-identity rows and build one card per connected group.

    Returns the cards and the row indexes that belong to an exact group, so the
    heuristic passes can skip pairs that are already covered.
    """
    rows = inputs.rows
    file_groups, hash_groups, phash_groups, scene_groups = _group_rows(inputs)
    components = _connected_components(len(rows), file_groups, hash_groups, phash_groups,
                                       scene_groups, category)

    found: list[dict] = []
    exact_members: set[int] = set()
    for indexes in components.values():
        members = [i for i in indexes if rows[i]["id"] not in inputs.decisions]
        if len(members) < 2:
            continue
        exact_members.update(members)
        live_ids = {rows[i]["id"] for i in members}
        possible_pairs = len(live_ids) * (len(live_ids) - 1) // 2
        group_dismissals = _dismissals_for_group(live_ids, inputs.dismissed_by_video)
        if len(live_ids) < 2 or len(group_dismissals) >= possible_pairs:
            continue
        # One O(N) group record, never the N(N-1)/2 set of possible pairs.
        record = _exact_group_record(inputs, members, group_dismissals)
        if record is not None:
            found.append(record)
    return found, exact_members


def _pair_score(left: dict, right: dict) -> dict | None:
    """Cast-and-duration score when both rows have a cast, otherwise the solo score."""
    shared = left["performer_norm"] & right["performer_norm"]
    if len(left["performer_norm"]) >= 2 and len(right["performer_norm"]) >= 2 and len(shared) >= 2:
        score = _score(left, right, shared)
        if not score:
            return None
        names = [left["performer_display"].get(n, n) for n in sorted(shared)]
        score["reasons"] = ["Matching cast and similar duration; review both items",
                             "Shared cast: " + ", ".join(names[:5]),
                             f"Duration differs by {score['components']['duration_delta']} s"]
        if score["components"].get("episode_part_penalty"):
            score["reasons"].append(
                "Different S-number parts may be different episodes; match confidence reduced")
        score["evidence"] = "cast_duration"
        return score
    return _score_solo(left, right) or None


def _cast_duration_candidates(inputs: _DuplicateInputs, candidate_pairs, exact_members: set[int],
                              found: list[dict]) -> None:
    rows, decisions, dismissed = inputs.rows, inputs.decisions, inputs.dismissed
    for i, j in candidate_pairs:
        left, right = rows[i], rows[j]
        if i in exact_members and j in exact_members:
            continue
        if left["id"] in decisions or right["id"] in decisions:
            continue
        if tuple(sorted((left["id"], right["id"]))) in dismissed:
            continue
        score = _pair_score(left, right)
        if not score:
            continue
        score["left"], score["right"] = _public_row(left), _public_row(right)
        found.append(score)


def _near_phash_distance(left: dict, right: dict, left_duration, left_hash: int,
                         left_split, left_part, phashes: dict[str, str]) -> int | None:
    """Hamming distance for a near-pHash pair, or None when the pair is not a candidate."""
    right_duration = _duration_values(right)
    if (left_duration is None or right_duration is None
            or abs(left_duration - right_duration) > min(15, max(5, .01 * max(left_duration, right_duration)))):
        return None
    right_split, right_part = extract_part_identity(right)
    distance = (left_hash ^ int(phashes[str(right["id"])], 16)).bit_count()
    if not 0 < distance <= PHASH_NEAR_MAX_DISTANCE:
        return None
    part_conflict = (left_split or right_split) and (not left_part or left_part != right_part)
    if part_conflict:
        return None
    return distance


def _add_phash_near_candidate(found: list[dict], left: dict, right: dict, distance: int,
                              pair_ids: tuple[str, str]) -> None:
    score = {"score": 90.0 if distance <= 2 else 85.0, "score_max": 100,
             "category": "Strong suggestion", "rules_version": RULES_VERSION,
             "evidence": "phash_near", "components": {"phash_distance": distance},
             "reasons": [f"Similar pHash (Hamming distance {distance}) and matching duration"]}
    score["left"], score["right"] = _public_row(left), _public_row(right)
    existing = next((candidate for candidate in found
                     if {str(candidate.get("left", {}).get("id")),
                         str(candidate.get("right", {}).get("id"))} == set(pair_ids)), None)
    if existing:
        if existing.get("evidence") not in {
                "telegram_file_id", "oshash", "phash", "stashdb_scene", "visual_sequence"}:
            existing["evidence"] = "phash_near"
            existing.setdefault("components", {})["phash_distance"] = distance
            existing.setdefault("reasons", []).append(
                f"Similar pHash (Hamming distance {distance}) and matching duration")
    else:
        found.append(score)


def _phash_near_candidates(inputs: _DuplicateInputs, found: list[dict]) -> bool:
    """Add near-pHash pairs. Returns True when the candidate cap stopped the scan."""
    rows, phashes = inputs.rows, inputs.phashes
    # Stash-compatible pHash values are 64-bit hashes. Compare nearby
    # values only among videos with compatible runtimes and safe part IDs.
    hash_rows = [i for i, row in enumerate(rows)
                 if phashes.get(str(row.get("id") or ""))]
    near_count = 0
    for offset, i in enumerate(hash_rows):
        left = rows[i]
        left_duration = _duration_values(left)
        left_hash = int(phashes[str(left["id"])], 16)
        left_split, left_part = extract_part_identity(left)
        for j in hash_rows[offset + 1:]:
            right = rows[j]
            distance = _near_phash_distance(left, right, left_duration, left_hash,
                                            left_split, left_part, phashes)
            if distance is None:
                continue
            pair_ids = tuple(sorted((str(left["id"]), str(right["id"]))))
            if pair_ids in inputs.dismissed or left["id"] in inputs.decisions or right["id"] in inputs.decisions:
                continue
            _add_phash_near_candidate(found, left, right, distance, pair_ids)
            near_count += 1
            if near_count >= MAX_HEURISTIC_CANDIDATES:
                return True
    return False


def _collect_duplicate_candidates(db_url: str | None, category: str = "") -> tuple[list[dict], int, bool]:
    """Compute the full, unpaginated candidate list shared by readers."""
    inputs, scan_complete = _load_duplicate_inputs(db_url, category)
    if inputs is None:
        return [], 0, scan_complete
    _prepare(inputs.rows, inputs.aliases)
    found, exact_members = _exact_candidates(inputs, category)
    if category == "Same file":
        # This category is defined entirely by validated Telegram file IDs;
        # heuristic title/cast candidates cannot affect its results.
        candidate_pairs, scan_complete = (), True
    else:
        candidate_pairs, scan_complete = _candidate_pairs(inputs.rows)
    _cast_duration_candidates(inputs, candidate_pairs, exact_members, found)
    if category != "Same file" and _phash_near_candidates(inputs, found):
        scan_complete = False

    category_rank = {"Same file": 0, "Strong suggestion": 1, "Needs review": 2}
    found.sort(key=lambda x: (category_rank[x["category"]], -x["score"],
                              x["left"]["id"], x["right"]["id"]))
    return found, inputs.scanned, scan_complete


def find_duplicate_candidates(db_url: str | None, limit: int = 200, offset: int = 0,
                              category: str = "", *, include_same_telegram_file: bool = True) -> dict:
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    category = str(category or "")
    wanted_category = DUPLICATE_CATEGORY_FILTERS.get(category)
    found, scanned, scan_complete = _collect_duplicate_candidates(db_url, wanted_category or "")
    if wanted_category:
        found = [pair for pair in found if pair.get("category") == wanted_category]
    if not include_same_telegram_file and wanted_category != "Same file":
        found = [pair for pair in found if pair.get("evidence") != "telegram_file_id"]
    return {"pairs": found[offset:offset + limit], "total": len(found),
            "truncated": offset + limit < len(found), "offset": offset,
            "category": category, "scanned": scanned, "complete": scan_complete,
            "candidate_limit_reached": not scan_complete, "rules_version": RULES_VERSION}


def scan_duplicate_index(db_url: str | None) -> dict:
    """Full, unpaginated candidate index for server-side decision validation.

    Decision handlers must not validate against a truncated page, or a valid
    pair beyond the first page would be rejected.
    """
    found, scanned, scan_complete = _collect_duplicate_candidates(db_url)
    return {"pairs": found, "total": len(found), "scanned": scanned,
            "complete": scan_complete, "candidate_limit_reached": not scan_complete,
            "rules_version": RULES_VERSION}


def find_active_oshash_merges(db_url: str | None, limit: int = 25, offset: int = 0) -> dict:
    return _find_active_oshash_merges_impl(
        db_url, limit, offset, db_api=db, public_row_fn=_public_row,
    )


def _decision_conn(db_url: str | None) -> db.Connection:
    return _decision_conn_impl(db_url, db_api=db)


