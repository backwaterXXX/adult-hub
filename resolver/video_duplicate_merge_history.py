"""Read-only history of active duplicate decisions with shared OSHASH."""
from __future__ import annotations

import re

from common import db
from video_duplicate_scoring import _prepare


def _load_page_videos(conn, db_api, page_ids) -> dict[str, dict]:
    """Load display rows and OSHASH/PHASH fingerprints for the videos on one page."""
    placeholders = ",".join(["%s"] * len(page_ids))
    columns = set(db_api.table_columns(conn, "videos"))
    optional = {"provider": "''", "title": "''", "slug": "''", "duration": "NULL",
                "performers": "'[]'", "studio": "''", "thumbnail": "NULL"}
    select = ["id"] + [name if name in columns else f"{fallback} AS {name}"
                        for name, fallback in optional.items()]
    if "url" in columns:
        select.append("url AS source_url")
    elif "source_url" in columns:
        select.append("source_url")
    else:
        select.append("'' AS source_url")
    videos = {str(row["id"]): dict(row) for row in conn.execute(
        f"SELECT {', '.join(select)} FROM videos WHERE id IN ({placeholders})", list(page_ids)
    ).fetchall()}
    _prepare(list(videos.values()))
    if "video_fingerprints" in db_api.table_names(conn):
        for video_id, algorithm, fingerprint in conn.execute(
            "SELECT video_id,algorithm,fingerprint FROM video_fingerprints "
            "WHERE video_id=ANY(%s) AND algorithm=ANY(%s)",
            (list(page_ids), ["OSHASH", "PHASH"]),
        ).fetchall():
            key = str(algorithm or "").lower()
            value = str(fingerprint or "").lower()
            if key in {"oshash", "phash"} and re.fullmatch(r"[0-9a-f]{16}", value):
                videos[str(video_id)][key] = value
    return videos


def find_active_oshash_merges(
    db_url: str | None, limit: int = 25, offset: int = 0, *, db_api=db, public_row_fn,
) -> dict:
    """List active reversible duplicate decisions with an exact shared OSHASH."""
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    conn = _decision_conn(db_url, db_api=db_api)
    try:
        has_evidence_col = "evidence" in db_api.table_columns(conn, "video_duplicate_decisions")
        evidence_expr = "COALESCE(d.evidence, '')" if has_evidence_col else "''"
        decisions = conn.execute(
            f"""SELECT d.operation_id,d.keeper_id,d.loser_id,d.created_at,fl.fingerprint,{evidence_expr}
               FROM video_duplicate_decisions d
               LEFT JOIN video_fingerprints fk ON fk.video_id=d.keeper_id AND fk.algorithm='OSHASH'
               LEFT JOIN video_fingerprints fl ON fl.video_id=d.loser_id AND fl.algorithm='OSHASH'
                                           AND fl.fingerprint=fk.fingerprint
               JOIN videos vk ON vk.id=d.keeper_id AND vk.provider='telegram'
               JOIN videos vl ON vl.id=d.loser_id AND vl.provider='telegram'
               WHERE d.state='active'
                 AND ((fl.fingerprint IS NOT NULL AND fl.fingerprint=fk.fingerprint)
                      OR {evidence_expr}='telegram_file_id')
               ORDER BY d.created_at DESC,d.operation_id,d.loser_id"""
        ).fetchall()
        by_operation: dict[str, dict] = {}
        for operation_id, keeper_id, loser_id, created_at, fingerprint, evidence in decisions:
            operation_id, keeper_id, loser_id = str(operation_id), str(keeper_id), str(loser_id)
            has_valid_oshash = bool(fingerprint and re.fullmatch(r"[0-9a-fA-F]{16}", str(fingerprint)))
            if not has_valid_oshash and str(evidence) != "telegram_file_id":
                continue
            group = by_operation.setdefault(operation_id, {
                "operation_id": operation_id, "keeper_id": keeper_id,
                "created_at": int(created_at), "oshashes": set(), "loser_ids": [],
            })
            if has_valid_oshash:
                group["oshashes"].add(str(fingerprint).lower())
            group["loser_ids"].append(loser_id)

        all_operations = sorted(by_operation.values(), key=lambda row: (-row["created_at"], row["operation_id"]))
        total = len(all_operations)
        page = all_operations[offset:offset + limit]
        if not page:
            return {"groups": [], "total": total, "offset": offset, "limit": limit, "scanned": len(decisions)}

        page_ids = {group["keeper_id"] for group in page}
        page_ids.update(loser_id for group in page for loser_id in group["loser_ids"])
        videos = _load_page_videos(conn, db_api, page_ids)

        operation_ids = list(dict.fromkeys(group["operation_id"] for group in page))
        operation_sizes = {
            str(operation_id): int(size)
            for operation_id, size in conn.execute(
                "SELECT operation_id,count(*) FROM video_duplicate_decisions "
                "WHERE operation_id=ANY(%s) AND state='active' GROUP BY operation_id",
                (operation_ids,),
            ).fetchall()
        }
        groups = []
        for group in page:
            keeper = videos.get(group["keeper_id"])
            losers = [videos[loser_id] for loser_id in group["loser_ids"] if loser_id in videos]
            if keeper is None or not losers:
                continue
            groups.append({
                "operation_id": group["operation_id"], "created_at": group["created_at"],
                "keeper": public_row_fn(keeper), "losers": [public_row_fn(row) for row in losers],
                "oshashes": sorted(group["oshashes"]),
                "operation_size": operation_sizes.get(str(group["operation_id"]), 0),
            })
        return {"groups": groups, "total": total, "offset": offset, "limit": limit,
                "scanned": len(decisions)}
    finally:
        conn.close()


def _decision_conn(db_url: str | None, *, db_api=db):
    conn = db_api.connect(db_url)
    conn.row_factory = db_api.Record
    return conn


