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


def preview_hide(db_url: str | None, keeper_id: str, loser_ids: list[str], config_path: str | None = None,
                 rules_version: int | None = None) -> dict:
    """Persist a bounded, revision-checked hide plan for one keeper and explicit losers."""
    keeper_id = str(keeper_id or "").strip()
    if not keeper_id or len(keeper_id) > 512 or not isinstance(loser_ids, list) or not 1 <= len(loser_ids) <= MAX_QUARANTINE_BATCH:
        return {"outcome": "invalid", "message": f"Specify one keeper and between 1 and {MAX_QUARANTINE_BATCH} items to hide."}
    ids = [str(value).strip() for value in loser_ids]
    if any(not value or len(value) > 512 for value in ids) or len(set(ids)) != len(ids) or keeper_id in ids:
        return {"outcome": "invalid", "message": "Keeper and item IDs must be unique, non-empty values."}
    if rules_version is not None and (type(rules_version) is not int or rules_version < 1):
        return {"outcome": "invalid", "message": "Specify a valid duplicate rules version."}
    conn = db.connect(db_url)
    conn.row_factory = db.Record
    try:
        config = None
        if config_path:
            from common.config_loader import load_config
            config = load_config(config_path, force_reload=True)
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
            if snapshot["active_decision"] or snapshot["source_disabled"]:
                conn.rollback()
                return {"outcome": "conflict", "message": "A selected item is already hidden or belongs to a disabled source."}
            if video_id != keeper_id and snapshot["shared_paths"]:
                conn.rollback()
                return {"outcome": "conflict", "message": "An item to hide shares a library file with another item."}
            revisions[video_id] = revision
            if video_id != keeper_id:
                pair_left, pair_right = sorted((keeper_id, video_id))
                if (pair_left, pair_right) in dismissed_pairs:
                    conn.rollback()
                    return {"outcome": "conflict", "message": "This pair is marked as different videos."}
                items.append({"video_id": video_id,
                              "title": str(snapshot["video"].get("title") or "Untitled"),
                              "file_count": len(snapshot["files"]),
                              "already_queued": already_queued})
        plan_id = uuid.uuid4().hex
        now = int(time.time())
        expires_at = now + PLAN_TTL_SECONDS
        conn.execute(
            "INSERT INTO admin_operation_plans "
            "(plan_id,operation_type,state,scope,revisions,preview,created_at,expires_at) "
            "VALUES (%s,'hide','previewed',%s,%s,%s,to_timestamp(%s),to_timestamp(%s))",
            (plan_id, Jsonb({"keeper_id": keeper_id, "loser_ids": ids,
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

def preview_exact_merge_plan(db_url: str | None, config_path: str | None = None) -> dict:
    """Preview a bounded set of complete exact-file duplicate groups."""
    from video_duplicates import scan_duplicate_index
    scan = scan_duplicate_index(db_url)
    if not scan.get("complete"):
        return {"outcome": "conflict", "message": "The duplicate scan is incomplete. Narrow the catalog and try again."}
    candidates = [pair for pair in scan.get("pairs", []) if pair.get("category") == "Same file"]
    if not candidates:
        return {"outcome": "empty", "message": "No exact duplicate files are available to merge."}
    conn = db.connect(db_url)
    conn.row_factory = db.Record
    try:
        config = None
        if config_path:
            from common.config_loader import load_config
            config = load_config(config_path, force_reload=True)
        groups = []
        revisions: dict[str, str] = {}
        items = []
        truncated = False
        total_losers = 0
        candidate_groups = []
        for pair in candidates:
            members = pair.get("members") or [pair.get("left"), pair.get("right")]
            members = [member for member in members if isinstance(member, dict) and member.get("id")]
            if len(members) < 2:
                continue
            candidate_groups.append((pair, members))

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

        stop = False
        for batch_groups, batch_video_ids in candidate_batches:
            snapshots = _video_snapshots(conn, batch_video_ids, config)
            for pair, members in batch_groups:
                live = [member for member in members if str(member["id"]) in snapshots
                        and not snapshots[str(member["id"])][1]["active_decision"]]
                if len(live) < 2:
                    continue
                if any(snapshots[str(member["id"])][1]["source_disabled"] for member in live):
                    continue

                def score(member, snapshots=snapshots):
                    video_id = str(member["id"])
                    snapshot = snapshots[video_id][1]
                    return ((1000 if snapshot["files"] else 0)
                            + len(member.get("performers") or []) * 10
                            + (50 if member.get("studio") else 0)
                            + min(len(member.get("title") or ""), 50))

                live.sort(key=lambda member: (-score(member), str(member["id"])))
                keeper_id = str(live[0]["id"])
                dismissed = {tuple(sorted(map(str, values))) for values in pair.get("dismissed_pairs", [])
                             if isinstance(values, (list, tuple)) and len(values) == 2}
                losers = [str(member["id"]) for member in live[1:]
                          if tuple(sorted((keeper_id, str(member["id"])))) not in dismissed]
                if not losers:
                    continue
                if total_losers + len(losers) > MAX_QUARANTINE_BATCH:
                    truncated = True
                    stop = True
                    break
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
                    continue
                revisions[keeper_id] = snapshots[keeper_id][0]
                groups.append({"keeper_id": keeper_id, "loser_ids": losers})
                items.extend(group_items)
                total_losers += len(losers)
            if stop:
                break
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
