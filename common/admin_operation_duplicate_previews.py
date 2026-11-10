"""Revision-checked duplicate hide previews for Admin v2."""

from __future__ import annotations

import time
import uuid

from psycopg.types.json import Jsonb

from common import db
from common.admin_operations import (
    MAX_EXACT_MERGE_SNAPSHOT_VIDEOS,
    MAX_QUARANTINE_BATCH,
    PLAN_TTL_SECONDS,
    _active_hide_job,
    _video_snapshots,
)


def _normalize_hide_request(keeper_id, loser_ids, rules_version):
    """Return (keeper_id, ids, error); error is None when the request is valid."""
    keeper_id = str(keeper_id or "").strip()
    if not keeper_id or len(keeper_id) > 512 or not isinstance(loser_ids, list) or not 1 <= len(loser_ids) <= MAX_QUARANTINE_BATCH:
        return keeper_id, [], {"outcome": "invalid", "message": f"Specify one keeper and between 1 and {MAX_QUARANTINE_BATCH} items to hide."}
    ids = [str(value).strip() for value in loser_ids]
    if any(not value or len(value) > 512 for value in ids) or len(set(ids)) != len(ids) or keeper_id in ids:
        return keeper_id, ids, {"outcome": "invalid", "message": "Keeper and item IDs must be unique, non-empty values."}
    if rules_version is not None and (type(rules_version) is not int or rules_version < 1):
        return keeper_id, ids, {"outcome": "invalid", "message": "Specify a valid duplicate rules version."}
    return keeper_id, ids, None


def _hide_candidate_conflict(video_id: str, snapshot, keeper_id: str, dismissed_pairs) -> dict | None:
    if snapshot["active_decision"] or snapshot["source_disabled"]:
        return {"outcome": "conflict", "message": "A selected item is already hidden or belongs to a disabled source."}
    if video_id != keeper_id and snapshot["shared_paths"]:
        return {"outcome": "conflict", "message": "An item to hide shares a library file with another item."}
    if video_id != keeper_id:
        pair_left, pair_right = sorted((keeper_id, video_id))
        if (pair_left, pair_right) in dismissed_pairs:
            return {"outcome": "conflict", "message": "This pair is marked as different videos."}
    return None


def _load_optional_config(config_path: str | None):
    if not config_path:
        return None
    from common.config_loader import load_config
    return load_config(config_path, force_reload=True)


def preview_hide(db_url: str | None, keeper_id: str, loser_ids: list[str], config_path: str | None = None,
                 rules_version: int | None = None) -> dict:
    """Persist a bounded, revision-checked hide plan for one keeper and explicit losers."""
    keeper_id, ids, error = _normalize_hide_request(keeper_id, loser_ids, rules_version)
    if error is not None:
        return error
    conn = db.connect(db_url)
    conn.row_factory = db.Record
    try:
        config = _load_optional_config(config_path)
        revisions: dict[str, str] = {}
        items = []
        all_ids = [keeper_id, *ids]
        snapshots = _video_snapshots(conn, all_ids, config)
        already_queued = _active_hide_job(conn, keeper_id, ids) is not None
        dismissed_rows = conn.execute(
            "SELECT left_id,right_id FROM video_duplicate_dismissals "
            "WHERE active=1 AND ((left_id=%s AND right_id=ANY(%s)) "
            "OR (right_id=%s AND left_id=ANY(%s)))",
            (keeper_id, ids, keeper_id, ids),
        ).fetchall()
        dismissed_pairs = {(str(row[0]), str(row[1])) for row in dismissed_rows}
        for video_id in all_ids:
            revision, snapshot = snapshots.get(video_id, (None, None))
            if revision is None:
                conn.rollback()
                return {"outcome": "not_found", "video_id": video_id}
            conflict = _hide_candidate_conflict(video_id, snapshot, keeper_id, dismissed_pairs)
            if conflict is not None:
                conn.rollback()
                return conflict
            revisions[video_id] = revision
            if video_id != keeper_id:
                items.append({"video_id": video_id,
                              "title": str(snapshot["video"].get("title") or "Untitled"),
                              "file_count": len(snapshot["files"]),
                              "already_queued": already_queued})
        from common.duplicate_evidence import pair_evidence
        for item in items:
            proof = pair_evidence(conn, keeper_id, item["video_id"])
            item["evidence"] = proof or {"type": "operator_review"}
        plan_id = uuid.uuid4().hex
        now = int(time.time())
        expires_at = now + PLAN_TTL_SECONDS
        conn.execute(
            "INSERT INTO admin_operation_plans "
            "(plan_id,operation_type,state,scope,revisions,preview,created_at,expires_at) "
            "VALUES (%s,'hide','previewed',%s,%s,%s,to_timestamp(%s),to_timestamp(%s))",
            (plan_id, Jsonb({"keeper_id": keeper_id, "loser_ids": ids, "operator_review": True,
                             **({"rules_version": rules_version} if rules_version is not None else {})}), Jsonb(revisions),
             Jsonb({"items": items, "keeper_id": keeper_id}), now, expires_at),
        )
        conn.commit()
        return {"outcome": "previewed", "plan_id": plan_id, "expires_at": expires_at,
                "operation_type": "hide", "items": items, "reversible": True}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _candidate_groups(candidates):
    candidate_groups = []
    for pair in candidates:
        members = pair.get("members") or [pair.get("left"), pair.get("right")]
        members = [member for member in members if isinstance(member, dict) and member.get("id")]
        if len(members) < 2:
            continue
        candidate_groups.append((pair, members))
    return candidate_groups


def _batch_candidate_groups(candidate_groups):
    """Split groups into batches whose combined snapshot video IDs stay under the limit."""
    candidate_batches = []
    batch_groups = []
    batch_video_ids = []
    for pair, members in candidate_groups:
        member_ids = list(dict.fromkeys(str(member["id"]) for member in members))
        combined_ids = list(dict.fromkeys([*batch_video_ids, *member_ids]))
        if batch_groups and len(combined_ids) > MAX_EXACT_MERGE_SNAPSHOT_VIDEOS:
            candidate_batches.append((batch_groups, batch_video_ids))
            batch_groups = []
            batch_video_ids = []
            combined_ids = member_ids
        batch_groups.append((pair, members))
        batch_video_ids = combined_ids
    if batch_groups:
        candidate_batches.append((batch_groups, batch_video_ids))
    return candidate_batches


def _exact_merge_score(member, snapshots) -> int:
    video_id = str(member["id"])
    snapshot = snapshots[video_id][1]
    return ((1000 if snapshot["files"] else 0)
            + len(member.get("performers") or []) * 10
            + (50 if member.get("studio") else 0)
            + min(len(member.get("title") or ""), 50))


def _select_exact_group(conn, pair, members, snapshots):
    """Return (keeper_id, losers) for a safe exact group, or None when the group is skipped."""
    live = [member for member in members if str(member["id"]) in snapshots
            and not snapshots[str(member["id"])][1]["active_decision"]]
    if len(live) < 2:
        return None
    if any(snapshots[str(member["id"])][1]["source_disabled"] for member in live):
        return None

    live.sort(key=lambda member: (-_exact_merge_score(member, snapshots), str(member["id"])))
    keeper_id = str(live[0]["id"])
    dismissed = {tuple(sorted(map(str, values))) for values in pair.get("dismissed_pairs", [])
                 if isinstance(values, (list, tuple)) and len(values) == 2}
    from common.duplicate_evidence import pair_evidence
    losers = [str(member["id"]) for member in live[1:]
              if tuple(sorted((keeper_id, str(member["id"])))) not in dismissed
              and pair_evidence(conn, keeper_id, str(member["id"]))]
    if not losers:
        return None
    return keeper_id, losers


def _group_items(losers, snapshots, keeper_id, revisions):
    """Build preview items for one group; revisions is updated in place, as before."""
    group_items = []
    for loser_id in losers:
        revision, snapshot = snapshots[loser_id]
        if snapshot["shared_paths"]:
            continue
        group_items.append({"video_id": loser_id,
                            "title": str(snapshot["video"].get("title") or "Untitled"),
                            "file_count": len(snapshot["files"]),
                            "keeper_id": keeper_id})
        revisions[loser_id] = revision
    if len(group_items) != len(losers):
        return None
    revisions[keeper_id] = snapshots[keeper_id][0]
    return group_items


def _collect_exact_merge_groups(conn, candidates, config):
    """Return (groups, revisions, items, truncated) for the exact-merge plan."""
    groups = []
    revisions: dict[str, str] = {}
    items = []
    truncated = False
    total_losers = 0
    candidate_groups = _candidate_groups(candidates)
    for batch_groups, batch_video_ids in _batch_candidate_groups(candidate_groups):
        snapshots = _video_snapshots(conn, batch_video_ids, config)
        for pair, members in batch_groups:
            selection = _select_exact_group(conn, pair, members, snapshots)
            if selection is None:
                continue
            keeper_id, losers = selection
            if total_losers + len(losers) > MAX_QUARANTINE_BATCH:
                truncated = True
                return groups, revisions, items, truncated
            group_items = _group_items(losers, snapshots, keeper_id, revisions)
            if group_items is None:
                continue
            groups.append({"keeper_id": keeper_id, "loser_ids": losers})
            items.extend(group_items)
            total_losers += len(losers)
    return groups, revisions, items, truncated


def preview_exact_merge_plan(db_url: str | None, config_path: str | None = None) -> dict:
    """Preview a bounded set of complete exact-file duplicate groups."""
    from resolver.video_duplicates import scan_duplicate_index
    scan = scan_duplicate_index(db_url)
    if not scan.get("complete"):
        return {"outcome": "conflict", "message": "The duplicate scan is incomplete. Narrow the catalog and try again."}
    candidates = [pair for pair in scan.get("pairs", []) if pair.get("category") == "Same file"]
    if not candidates:
        return {"outcome": "empty", "message": "No exact duplicate files are available to merge."}
    conn = db.connect(db_url)
    conn.row_factory = db.Record
    try:
        config = _load_optional_config(config_path)
        groups, revisions, items, truncated = _collect_exact_merge_groups(conn, candidates, config)
        if not groups:
            conn.rollback()
            return {"outcome": "empty", "message": "No safe exact duplicate groups are available to merge."}
        plan_id = uuid.uuid4().hex
        now = int(time.time())
        expires_at = now + PLAN_TTL_SECONDS
        conn.execute(
            "INSERT INTO admin_operation_plans "
            "(plan_id,operation_type,state,scope,revisions,preview,created_at,expires_at) "
            "VALUES (%s,'hide_batch','previewed',%s,%s,%s,to_timestamp(%s),to_timestamp(%s))",
            (plan_id, Jsonb({"groups": groups}), Jsonb(revisions),
             Jsonb({"items": items, "truncated": truncated, "scanned_groups": len(candidates)}), now, expires_at),
        )
        conn.commit()
        return {"outcome": "previewed", "plan_id": plan_id, "expires_at": expires_at,
                "operation_type": "hide_batch", "items": items, "truncated": truncated,
                "scanned_groups": len(candidates), "reversible": True}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
