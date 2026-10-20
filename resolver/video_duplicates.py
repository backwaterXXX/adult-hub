"""Conservative duplicate suggestions and durable review decisions."""

from __future__ import annotations

import re
from common import db
from collections import defaultdict
from urllib.parse import urlsplit
from common.job_queue import enqueue_job, retry_job  # noqa: F401 - compatibility export used by Admin routes

from video_duplicate_merge_history import (
    _decision_conn as _decision_conn_impl,
    find_active_oshash_merges as _find_active_oshash_merges_impl,
)

from video_duplicate_scoring import (
    EPISODE_PART_SCORE_PENALTY as EPISODE_PART_SCORE_PENALTY,
    MAX_HEURISTIC_CANDIDATES as MAX_HEURISTIC_CANDIDATES,
    MIN_SCORE as MIN_SCORE,
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


def _collect_duplicate_candidates(db_url: str | None, category: str = "") -> tuple[list[dict], int, bool]:
    """Compute the full, unpaginated candidate list shared by readers."""
    try:
        conn = db.connect(db_url)
    except Exception:
        return [], 0, False
    conn.row_factory = db.Record
    aliases: dict[str, str] = {}
    try:
        cols = set(db.table_columns(conn, "videos"))
        if "id" not in cols:
            return [], 0, True
        optional = {"provider": "''", "title": "''", "slug": "''", "duration": "NULL",
                    "performers": "'[]'", "studio": "''", "thumbnail": "NULL"}
        select = ["id"] + [n if n in cols else f"{fallback} AS {n}" for n, fallback in optional.items()]
        if "url" in cols:
            select.append("url AS source_url")
        elif "source_url" in cols:
            select.append("source_url")
        else:
            select.append("'' AS source_url")
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
        if "performer_aliases" in tables_available:
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
        oshashes: dict[str, str] = {}
        phashes: dict[str, str] = {}
        if "video_fingerprints" in tables_available:
            for video_id, algorithm, fingerprint in conn.execute(
                "SELECT video_id, algorithm, fingerprint FROM video_fingerprints "
                "WHERE algorithm=ANY(%s)", (["OSHASH", "PHASH"],)
            ).fetchall():
                value = str(fingerprint or "").strip().lower()
                if not video_id or not re.fullmatch(r"[0-9a-f]{16}", value):
                    continue
                target = oshashes if algorithm == "OSHASH" else phashes if algorithm == "PHASH" else None
                if target is not None:
                    target[str(video_id)] = value
        for row in rows:
            video_id = str(row.get("id") or "")
            row["oshash"] = oshashes.get(video_id)
            row["phash"] = phashes.get(video_id)
        # Decision tables may not exist until the first sync migration.
        tables = tables_available
        decisions = set()
        if "video_duplicate_decisions" in tables:
            decisions = {r[0] for r in conn.execute(
                "SELECT loser_id FROM video_duplicate_decisions WHERE state='active'")}
        dismissed = set()
        if "video_duplicate_dismissals" in tables:
            dismissed = {tuple(sorted((r[0], r[1]))) for r in conn.execute(
                "SELECT left_id, right_id FROM video_duplicate_dismissals WHERE active=1")}
        dismissed_by_video = _index_dismissals(dismissed)
    finally:
        conn.close()

    _prepare(rows, aliases)
    found: list[dict] = []
    file_groups: dict[str, list[int]] = defaultdict(list)
    hash_groups: dict[str, list[int]] = defaultdict(list)
    for i, row in enumerate(rows):
        fid = _telegram_file_id(row)
        if fid is not None:
            file_groups[fid].append(i)
        oshash = oshashes.get(str(row.get("id") or ""))
        if row.get("provider") == "telegram" and oshash:
            hash_groups[oshash].append(i)

    # Group repeated Telegram IDs as proven same-file entries. Shared OSHASH
    # values join the same review group but remain suggestions because OSHASH
    # is not a cryptographic identity. Unioning keys avoids duplicate cards.
    parents = list(range(len(rows)))

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
    for indexes in exact_group_indexes:
        union(indexes)

    components: dict[int, list[int]] = defaultdict(list)
    grouped_indexes = {index for indexes in exact_group_indexes for index in indexes}
    for index in sorted(grouped_indexes):
        components[find(index)].append(index)

    exact_members: set[int] = set()
    for indexes in components.values():
        members = [i for i in indexes if rows[i]["id"] not in decisions]
        if len(members) < 2:
            continue
        exact_members.update(members)
        live_ids = {rows[i]["id"] for i in members}
        possible_pairs = len(live_ids) * (len(live_ids) - 1) // 2
        group_dismissals = _dismissals_for_group(live_ids, dismissed_by_video)
        dismissed_count = len(group_dismissals)
        if len(live_ids) < 2 or dismissed_count >= possible_pairs:
            continue
        # One O(N) group record, never the N(N-1)/2 set of possible pairs.
        representative = None
        for left_pos, left_i in enumerate(members):
            for right_i in members[left_pos + 1:]:
                ids = tuple(sorted((rows[left_i]["id"], rows[right_i]["id"])))
                if ids not in dismissed:
                    representative = (left_i, right_i)
                    break
            if representative:
                break
        if not representative:
            continue
        base = rows[representative[0]]
        member_file_ids = {_telegram_file_id(rows[i]) for i in members}
        member_file_ids.discard(None)
        hash_counts: dict[str, int] = defaultdict(int)
        for i in members:
            fingerprint = oshashes.get(str(rows[i]["id"]))
            if fingerprint:
                hash_counts[fingerprint] += 1
        shared_oshashes = sorted(fingerprint for fingerprint, count in hash_counts.items() if count > 1)
        same_telegram_file = len(member_file_ids) == 1
        warnings = []
        member_rows = [rows[i] for i in members]
        durations = [duration for row in member_rows if (duration := _duration_values(row)) is not None]
        if len(set(durations)) > 1:
            warnings.append(f"Duration differs across messages ({min(durations)}–{max(durations)} s)")
        casts = {frozenset(row["performer_norm"]) for row in member_rows if row["performer_norm"]}
        if len(casts) > 1:
            warnings.append("Cast differs across messages")
        if same_telegram_file:
            file_id = next(iter(member_file_ids))
        else:
            file_id = None
        if file_id and any(row.get("slug") and str(row["slug"]) != file_id for row in member_rows):
            warnings.append("Inconsistent metadata: slug differs from file ID in ref")
        if len(member_file_ids) > 1 and shared_oshashes:
            reasons = ["Matching OSHASH fingerprint across Telegram messages; review before merging"]
        else:
            reasons = ["Same Telegram file, different messages"]
        same_file = same_telegram_file
        found.append({
            "left": _public_row(base), "right": _public_row(rows[representative[1]]),
            "members": [_public_row(rows[i]) for i in members],
            "dismissed_pairs": [list(pair) for pair in sorted(group_dismissals)],
            "score": 100.0 if same_file else 92.0, "score_max": 100,
            "category": "Same file" if same_file else "Strong suggestion",
            "evidence": "telegram_file_id" if same_telegram_file else "oshash",
            "rules_version": RULES_VERSION,
            "components": ({"telegram_file_id": file_id} if same_telegram_file else
                           {"oshashes": shared_oshashes} if shared_oshashes else {}),
            "reasons": [*reasons, *sorted(set(warnings))],
        })

    if category == "Same file":
        # This category is defined entirely by validated Telegram file IDs;
        # heuristic title/cast candidates cannot affect its results.
        candidate_pairs, scan_complete = (), True
    else:
        candidate_pairs, scan_complete = _candidate_pairs(rows)
    for i, j in candidate_pairs:
        left, right = rows[i], rows[j]
        if i in exact_members or j in exact_members:
            continue
        if left["id"] in decisions or right["id"] in decisions:
            continue
        if tuple(sorted((left["id"], right["id"]))) in dismissed:
            continue
        shared = left["performer_norm"] & right["performer_norm"]
        if len(left["performer_norm"]) >= 2 and len(right["performer_norm"]) >= 2 and len(shared) >= 2:
            score = _score(left, right, shared)
            if not score:
                continue
            names = [left["performer_display"].get(n, n) for n in sorted(shared)]
            score["reasons"] = ["Matching cast and similar duration; review both items",
                                 "Shared cast: " + ", ".join(names[:5]),
                                 f"Duration differs by {score['components']['duration_delta']} s"]
            if score["components"].get("episode_part_penalty"):
                score["reasons"].append(
                    "Different S-number parts may be different episodes; match confidence reduced")
            score["evidence"] = "cast_duration"
        else:
            score = _score_solo(left, right)
            if not score:
                continue
        score["left"], score["right"] = _public_row(left), _public_row(right)
        found.append(score)

    category_rank = {"Same file": 0, "Strong suggestion": 1, "Needs review": 2}
    found.sort(key=lambda x: (category_rank[x["category"]], -x["score"],
                              x["left"]["id"], x["right"]["id"]))
    return found, scanned, scan_complete


def find_duplicate_candidates(db_url: str | None, limit: int = 200, offset: int = 0,
                              category: str = "") -> dict:
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    category = str(category or "")
    wanted_category = DUPLICATE_CATEGORY_FILTERS.get(category)
    found, scanned, scan_complete = _collect_duplicate_candidates(db_url, wanted_category or "")
    if wanted_category:
        found = [pair for pair in found if pair.get("category") == wanted_category]
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


