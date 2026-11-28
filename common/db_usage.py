"""Operator-facing PostgreSQL table and database size report."""

from __future__ import annotations

from common import db


def read_db_usage(db_url: str | None, *, limit: int = 20) -> dict:
    """Return the database size and the largest application tables in bytes."""
    safe_limit = max(1, min(int(limit), 100))
    if not db_url:
        return {"available": False, "database_bytes": 0, "tables": []}
    try:
        conn = db.connect(db_url)
    except Exception:
        return {"available": False, "database_bytes": 0, "tables": []}
    try:
        database_bytes = int(conn.execute(
            "SELECT pg_database_size(current_database())"
        ).fetchone()[0] or 0)
        rows = conn.execute("""
            SELECT t.relname,
                   pg_total_relation_size(t.relid) AS total_bytes,
                   pg_relation_size(t.relid) AS table_bytes,
                   pg_indexes_size(t.relid) AS index_bytes,
                   CASE WHEN c.reltoastrelid = 0 THEN 0
                        ELSE pg_total_relation_size(c.reltoastrelid) END AS toast_bytes,
                   t.n_live_tup, t.n_dead_tup
            FROM pg_stat_user_tables t
            JOIN pg_class c ON c.oid = t.relid
            ORDER BY pg_total_relation_size(t.relid) DESC, t.relname
            LIMIT %s
        """, (safe_limit,)).fetchall()
    except Exception:
        return {"available": False, "database_bytes": 0, "tables": []}
    finally:
        conn.close()
    return {
        "available": True,
        "database_bytes": database_bytes,
        "tables": [
            {
                "name": str(row[0]),
                "total_bytes": int(row[1] or 0),
                "table_bytes": int(row[2] or 0),
                "index_bytes": int(row[3] or 0),
                "toast_bytes": int(row[4] or 0),
                "live_rows": int(row[5] or 0),
                "dead_rows": int(row[6] or 0),
            }
            for row in rows
        ],
    }
