"""Aggregated, identifier-free status for external metadata enrichment."""

from __future__ import annotations

from common import db


def enrichment_status(db_url: str | None) -> dict:
    """Return catalog and per-source queue counts without exposing entity keys."""
    conn = db.connect(db_url)
    try:
        catalog = conn.execute("""
            SELECT
                (SELECT count(*) FROM videos WHERE provider='telegram') AS videos,
                (SELECT count(DISTINCT lower(trim(actor)))
                   FROM videos v
                   CROSS JOIN LATERAL jsonb_array_elements_text(
                       CASE WHEN jsonb_typeof(v.performers)='array' THEN v.performers ELSE '[]'::jsonb END
                   ) AS actors(actor)
                  WHERE trim(actor)<>''
                    AND lower(trim(actor)) NOT IN ('fucked','fuck','fucking','fucker')
                    AND lower(trim(actor)) !~ '\\.(mp4|mkv|avi|mov|wmv|flv|webm|m4v|ts)$'
                ) AS performers
        """).fetchone()
        rows = conn.execute("""
            SELECT entity_type,source,count(*)::bigint AS attempts,
                   count(*) FILTER (WHERE outcome='found')::bigint AS found,
                   count(*) FILTER (WHERE outcome='no_match')::bigint AS no_match,
                   count(*) FILTER (WHERE outcome='error')::bigint AS errors,
                   count(*) FILTER (WHERE next_attempt_at<=CURRENT_TIMESTAMP)::bigint AS due,
                   max(extract(epoch FROM last_attempt_at)) AS last_attempt_at
            FROM external_metadata_attempts
            GROUP BY entity_type,source
            ORDER BY entity_type,source
        """).fetchall()
        sources = []
        for entity_type, source, attempts, found, no_match, errors, due, last_attempt_at in rows:
            sources.append({
                "entity_type": str(entity_type), "source": str(source),
                "catalog_total": int(catalog[0] if entity_type == "video" else catalog[1]),
                "attempts": int(attempts), "found": int(found),
                "no_match": int(no_match), "errors": int(errors), "due": int(due),
                "remaining": max(0, int(catalog[0] if entity_type == "video" else catalog[1]) - int(attempts)) + int(due),
                "last_attempt_at": float(last_attempt_at) if last_attempt_at is not None else None,
            })
        totals = {
            "sources": len(sources),
            "attempts": sum(row[2] for row in rows),
            "found": sum(row[3] for row in rows),
            "no_match": sum(row[4] for row in rows),
            "errors": sum(row[5] for row in rows),
            "due": sum(row[6] for row in rows),
        }
        return {
            "available": True,
            "catalog": {"videos": int(catalog[0]), "performers": int(catalog[1])},
            "totals": totals,
            "sources": sources,
        }
    finally:
        conn.close()
