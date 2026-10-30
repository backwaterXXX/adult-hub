"""Aggregated, identifier-free status for external metadata enrichment."""

from __future__ import annotations

from common import db


def enrichment_status(db_url: str | None) -> dict:
    """Return catalog and per-source queue counts without exposing entity keys."""
    conn = db.connect(db_url)
    try:
        rows = conn.execute("""
            WITH performers AS MATERIALIZED (
                SELECT DISTINCT lower(trim(actor)) AS name
                FROM videos v
                CROSS JOIN LATERAL jsonb_array_elements_text(
                    CASE WHEN jsonb_typeof(v.performers)='array' THEN v.performers ELSE '[]'::jsonb END
                ) AS actors(actor)
                WHERE trim(actor)<>''
                  AND lower(trim(actor)) NOT IN ('fucked','fuck','fucking','fucker')
                  AND lower(trim(actor)) !~ '\\.(mp4|mkv|avi|mov|wmv|flv|webm|m4v|ts)$'
            ), catalog AS (
                SELECT (SELECT count(*) FROM videos WHERE provider='telegram')::bigint AS videos,
                       (SELECT count(*) FROM performers)::bigint AS performers
            ), source_attempts AS (
                SELECT a.entity_type,a.source,count(*)::bigint AS attempts,
                   count(*) FILTER (WHERE a.outcome='found')::bigint AS found,
                   count(*) FILTER (WHERE a.outcome='no_match')::bigint AS no_match,
                   count(*) FILTER (WHERE a.outcome='error')::bigint AS errors,
                   count(*) FILTER (WHERE a.next_attempt_at<=CURRENT_TIMESTAMP)::bigint AS due,
                   max(extract(epoch FROM a.last_attempt_at)) AS last_attempt_at,
                   count(*) FILTER (WHERE v.id IS NOT NULL OR p.name IS NOT NULL)::bigint AS catalog_checked,
                   count(*) FILTER (WHERE (v.id IS NOT NULL OR p.name IS NOT NULL)
                                    AND a.next_attempt_at<=CURRENT_TIMESTAMP)::bigint AS catalog_due
            FROM external_metadata_attempts a
            LEFT JOIN videos v ON a.entity_type='video' AND v.id=a.entity_key AND v.provider='telegram'
            LEFT JOIN performers p ON a.entity_type='performer' AND p.name=a.entity_key
            GROUP BY a.entity_type,a.source
            )
            SELECT c.videos,c.performers,a.entity_type,a.source,a.attempts,a.found,a.no_match,
                   a.errors,a.due,a.last_attempt_at,a.catalog_checked,a.catalog_due
            FROM catalog c LEFT JOIN source_attempts a ON TRUE
            ORDER BY a.entity_type,a.source
        """).fetchall()
        sources = []
        catalog_videos = int(rows[0][0]) if rows else 0
        catalog_performers = int(rows[0][1]) if rows else 0
        for row in rows:
            (_, _, entity_type, source, attempts, found, no_match, errors, due,
             last_attempt_at, catalog_checked, catalog_due) = row
            if entity_type is None:
                continue
            catalog_total = catalog_videos if entity_type == "video" else catalog_performers
            sources.append({
                "entity_type": str(entity_type), "source": str(source),
                "catalog_total": catalog_total,
                "attempts": int(attempts), "catalog_checked": int(catalog_checked), "catalog_due": int(catalog_due), "found": int(found),
                "no_match": int(no_match), "errors": int(errors), "due": int(due),
                "remaining": max(0, catalog_total - int(catalog_checked)) + int(catalog_due),
                "last_attempt_at": float(last_attempt_at) if last_attempt_at is not None else None,
            })
        attempts_rows = [row for row in rows if row[2] is not None]
        totals = {
            "sources": len(sources),
            "attempts": sum(row[4] for row in attempts_rows),
            "found": sum(row[5] for row in attempts_rows),
            "no_match": sum(row[6] for row in attempts_rows),
            "errors": sum(row[7] for row in attempts_rows),
            "due": sum(row[8] for row in attempts_rows),
        }
        return {
            "available": True,
            "catalog": {"videos": catalog_videos, "performers": catalog_performers},
            "totals": totals,
            "sources": sources,
        }
    finally:
        conn.close()
