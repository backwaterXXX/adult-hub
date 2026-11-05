"""Read-only people catalog for Admin v2's Organize workspace."""

from __future__ import annotations

import json
from pathlib import PurePosixPath
import logging
from fastapi import APIRouter, Request
from admin_v2_errors import V2ErrorRoute

from common import db
from admin_library_v2 import _safe_thumbnail

logger = logging.getLogger(__name__)


def build_router(admin):
    router = APIRouter(route_class=V2ErrorRoute)
    _route_quarantine(router, admin)
    _route_file_quarantine(router, admin)
    _route_people(router, admin)
    _route_person_detail(router, admin)
    _route_person_profile(router, admin)
    _route_update_person_profile(router, admin)
    return router


def _route_quarantine(router, admin):
    @router.get("/api/v2/organize/quarantine")
    async def quarantine(request: Request, offset: int = 0, limit: int = 50):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"ok": False, "error": "Admin is only available locally."}, status_code=403)
        if offset < 0 or offset > 1_000_000 or limit < 1 or limit > 100:
            return admin.JSONResponse({"ok": False, "error": "Invalid quarantine query."}, status_code=400)
        try:
            report = await admin.asyncio.to_thread(admin.admin_data.quarantine_report,
                                                   admin.HUB_DB_URL, limit, offset)
            return {"ok": True, **report}
        except Exception:
            logger.warning("Request failed in quarantine", exc_info=True)
            return admin.JSONResponse({"ok": False, "error": "Could not load quarantine history."}, status_code=503)


def _project_quarantine_files(files):
    projected = []
    for item in files:
        path = item.get("path") if isinstance(item, dict) else None
        parsed = PurePosixPath(path) if isinstance(path, str) else None
        if (parsed is None or parsed.is_absolute() or ".." in parsed.parts or "\\" in path
                or parsed.suffix.lower() not in {".strm", ".nfo", ".jpg"}):
            continue
        projected.append({"path": path, "size": int(item.get("size") or 0)})
    return projected


def _quarantine_file_items(rows):
    items = []
    for operation_id, raw_files, created_at in rows:
        files = raw_files if isinstance(raw_files, list) else json.loads(raw_files or "[]")
        projected = _project_quarantine_files(files)
        items.append({"operation_id": str(operation_id), "files": projected,
                      "file_count": len(projected), "created_at": created_at.isoformat()
                      if hasattr(created_at, "isoformat") else str(created_at)})
    return items


def _route_file_quarantine(router, admin):
    @router.get("/api/v2/organize/quarantine/files")
    async def file_quarantine(request: Request, offset: int = 0, limit: int = 50):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"ok": False, "error": "Admin is only available locally."}, status_code=403)
        if offset < 0 or offset > 1_000_000 or limit < 1 or limit > 100:
            return admin.JSONResponse({"ok": False, "error": "Invalid file quarantine query."}, status_code=400)
        conn = None
        try:
            conn = db.connect(admin.HUB_DB_URL)
            total = int(conn.execute("SELECT count(*) FROM admin_orphan_quarantine WHERE state='active'").fetchone()[0])
            rows = conn.execute("SELECT operation_id,files,created_at FROM admin_orphan_quarantine "
                                "WHERE state='active' ORDER BY created_at DESC,operation_id LIMIT %s OFFSET %s",
                                (limit, offset)).fetchall()
            items = _quarantine_file_items(rows)
            return {"ok": True, "items": items, "total": total, "offset": offset, "limit": limit,
                    "truncated": offset + len(items) < total}
        except Exception:
            logger.warning("Request failed in file_quarantine", exc_info=True)
            return admin.JSONResponse({"ok": False, "error": "Could not load file quarantine history."}, status_code=503)
        finally:
            if conn is not None:
                conn.close()


def _people_sources(tables):
    """Describe which optional people tables exist and how they join into the catalog query."""
    sources = {
        "joins": [],
        "group_fields": ["p.performer_name"],
        "bio_flag": "FALSE",
        "photo_flag": "FALSE",
        "aliases_expr": "'[]'::jsonb",
        "alias_search": [],
        "mapped_aliases_available": "performer_aliases" in tables,
    }
    if "performer_bios" in tables:
        sources["joins"].append("LEFT JOIN performer_bios b ON b.name = p.performer_name")
        sources["bio_flag"] = "(b.bio IS NOT NULL AND btrim(b.bio) <> '')"
        sources["aliases_expr"] = "COALESCE(b.aliases, '[]'::jsonb)"
        sources["group_fields"].extend(["b.name", "b.bio", "b.aliases"])
        sources["alias_search"].append(
            "EXISTS (SELECT 1 FROM performer_bios pb, "
            "jsonb_array_elements_text(COALESCE(pb.aliases, '[]'::jsonb)) AS alias_values(alias_value) "
            "WHERE pb.name = p.performer_name AND alias_values.alias_value ILIKE %s)"
        )
    if sources["mapped_aliases_available"]:
        sources["alias_search"].append(
            "EXISTS (SELECT 1 FROM performer_aliases pa "
            "WHERE lower(pa.canonical) = lower(p.performer_name) AND pa.alias ILIKE %s)"
        )
    if "performer_photos" in tables:
        sources["joins"].append("LEFT JOIN performer_photos ph ON ph.name = p.performer_name")
        sources["photo_flag"] = "(ph.thumb IS NOT NULL AND btrim(ph.thumb) <> '')"
        sources["group_fields"].extend(["ph.name", "ph.thumb"])
    return sources


def _people_where(q, alias_search):
    where = "WHERE btrim(p.performer_name) <> ''"
    params = []
    if q:
        search_clauses = ["p.performer_name ILIKE %s", *alias_search]
        where += " AND (" + " OR ".join(search_clauses) + ")"
        params.extend([f"%{q}%"] * len(search_clauses))
    return where, params


def _mapped_aliases_by_name(conn, rows):
    # Aliases of the mapped table are collected for the returned page alone. In
    # the grouped query PostgreSQL evaluates them per skipped row, which made
    # offset 2000 cost 2.8s where offset 0 cost 0.2s. Matching stays in SQL so
    # the page's names are compared with the same lower() the old subquery used.
    mapped_by_name = {}
    for page_name, mapped in conn.execute(
        "SELECT n.name, jsonb_agg(pa.alias ORDER BY lower(pa.alias), pa.alias) "
        "FROM unnest(%s::text[]) AS n(name) "
        "JOIN performer_aliases pa ON lower(pa.canonical) = lower(n.name) "
        "GROUP BY n.name",
        ([str(row[0]) for row in rows],),
    ).fetchall():
        mapped_by_name[page_name] = mapped
    return mapped_by_name


def _decode_alias_list(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = []
    return value


def _combine_aliases(name, aliases, mapped_aliases):
    combined_aliases = []
    for alias in [*(aliases if isinstance(aliases, list) else []),
                  *(mapped_aliases if isinstance(mapped_aliases, list) else [])]:
        if isinstance(alias, str) and alias.strip() and alias.strip().casefold() != str(name).casefold():
            if all(alias.strip().casefold() != existing.casefold() for existing in combined_aliases):
                combined_aliases.append(alias.strip())
    return combined_aliases


def _person_item(row, mapped_by_name):
    name, count, has_bio, has_photo, aliases = row
    aliases = _decode_alias_list(aliases)
    mapped_aliases = _decode_alias_list(mapped_by_name.get(name) or [])
    return {"name": str(name), "video_count": int(count),
            "has_bio": bool(has_bio), "has_photo": bool(has_photo),
            "aliases": _combine_aliases(name, aliases, mapped_aliases)}


def _people_page(conn, tables, q, offset, limit):
    sources = _people_sources(tables)
    where, params = _people_where(q, sources["alias_search"])
    join_sql = " ".join(sources["joins"])
    total = conn.execute(
        f"SELECT count(DISTINCT p.performer_name) FROM video_performers p {where}", params
    ).fetchone()[0]
    rows = conn.execute(
        "SELECT p.performer_name, count(DISTINCT p.video_id), "
        f"{sources['bio_flag']}, {sources['photo_flag']}, {sources['aliases_expr']} "
        f"FROM video_performers p {join_sql} {where} "
        f"GROUP BY {', '.join(sources['group_fields'])} "
        "ORDER BY lower(p.performer_name), p.performer_name LIMIT %s OFFSET %s",
        [*params, limit, offset],
    ).fetchall()
    mapped_by_name = {}
    if sources["mapped_aliases_available"] and rows:
        mapped_by_name = _mapped_aliases_by_name(conn, rows)
    items = [_person_item(row, mapped_by_name) for row in rows]
    return {"ok": True, "items": items, "total": int(total), "offset": offset,
            "limit": limit, "available": True}


def _route_people(router, admin):
    @router.get("/api/v2/organize/people")
    async def people(request: Request, q: str = "", offset: int = 0, limit: int = 50):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"ok": False, "error": "Admin is only available locally."}, status_code=403)
        q = q.strip()
        if len(q) > 200 or offset < 0 or offset > 1_000_000 or limit < 1 or limit > 100:
            return admin.JSONResponse({"ok": False, "error": "Invalid people catalog query."}, status_code=400)
        conn = None
        try:
            conn = db.connect(admin.HUB_DB_URL)
            tables = set(db.table_names(conn))
            if "video_performers" not in tables:
                return {"ok": True, "items": [], "total": 0, "offset": offset, "limit": limit,
                        "available": False}
            return _people_page(conn, tables, q, offset, limit)
        except Exception:
            logger.warning("Request failed in people", exc_info=True)
            return admin.JSONResponse({"ok": False, "error": "Could not load the people catalog."}, status_code=503)
        finally:
            if conn is not None:
                conn.close()


def _route_person_detail(router, admin):
    @router.get("/api/v2/organize/people/{name}")
    async def person_detail(request: Request, name: str, offset: int = 0, limit: int = 50):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"ok": False, "error": "Admin is only available locally."}, status_code=403)
        if not name or len(name) > 200 or offset < 0 or offset > 1_000_000 or limit < 1 or limit > 100:
            return admin.JSONResponse({"ok": False, "error": "Invalid performer query."}, status_code=400)
        conn = None
        try:
            conn = db.connect(admin.HUB_DB_URL)
            if "video_performers" not in set(db.table_names(conn)):
                return admin.JSONResponse({"ok": False, "error": "People catalog is unavailable."}, status_code=503)
            total = conn.execute(
                "SELECT count(DISTINCT v.id) FROM video_performers p JOIN videos v ON v.id=p.video_id "
                "WHERE p.performer_name=%s",
                (name,),
            ).fetchone()[0]
            if not total:
                return admin.JSONResponse({"ok": False, "error": "Performer not found."}, status_code=404)
            rows = conn.execute(
                "SELECT v.id, v.title, v.studio, v.duration, v.thumbnail "
                "FROM video_performers p JOIN videos v ON v.id=p.video_id "
                "WHERE p.performer_name=%s "
                "GROUP BY v.id, v.title, v.studio, v.duration, v.thumbnail "
                "ORDER BY lower(COALESCE(v.title,'')), COALESCE(v.title,''), v.id LIMIT %s OFFSET %s",
                (name, limit, offset),
            ).fetchall()
            return {"ok": True, "name": name, "filmography": [
                {"id": row[0], "title": row[1], "studio": row[2], "duration": row[3],
                 "thumbnail": _safe_thumbnail(row[4])} for row in rows
            ], "total": int(total), "offset": offset, "limit": limit}
        except Exception:
            logger.warning("Request failed in person_detail", exc_info=True)
            return admin.JSONResponse({"ok": False, "error": "Could not load performer details."}, status_code=503)
        finally:
            if conn is not None:
                conn.close()


def _route_person_profile(router, admin):
    @router.get("/api/v2/organize/people/{name}/profile")
    async def person_profile(request: Request, name: str):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"ok": False, "error": "Admin is only available locally."}, status_code=403)
        if not name or len(name) > 200:
            return admin.JSONResponse({"ok": False, "error": "Invalid performer name."}, status_code=400)
        conn = None
        try:
            from common.performer_profiles import read_profile
            conn = db.connect(admin.HUB_DB_URL)
            exists = conn.execute(
                "SELECT 1 FROM video_performers WHERE lower(performer_name)=lower(%s) LIMIT 1", (name,)
            ).fetchone()
            if not exists:
                return admin.JSONResponse({"ok": False, "error": "Performer not found."}, status_code=404)
            return {"ok": True, "profile": read_profile(conn, name)}
        except Exception:
            logger.warning("Request failed in person_profile", exc_info=True)
            return admin.JSONResponse({"ok": False, "error": "Could not load performer profile."}, status_code=503)
        finally:
            if conn is not None:
                conn.close()


async def _read_profile_update_body(admin, request):
    """Return (body, rejection). A non-None rejection must be returned to the client."""
    content_length = request.headers.get("content-length")
    try:
        if content_length is not None and int(content_length) > 32 * 1024:
            return None, admin.JSONResponse({"ok": False, "error": "Request is too large."}, status_code=413)
    except ValueError:
        return None, admin.JSONResponse({"ok": False, "error": "Invalid request size."}, status_code=400)
    raw = bytearray()
    try:
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 32 * 1024:
                return None, admin.JSONResponse({"ok": False, "error": "Request is too large."}, status_code=413)
            raw.extend(chunk)
        body = json.loads(raw)
    except Exception:
        body = None
    return body, None


def _profile_update_rejection(admin, body):
    """Return a 400 response for an invalid profile body, or None when it is valid."""
    allowed = {"expected_revision", "bio", "photo", "aliases", "reset_fields"}
    if (not isinstance(body, dict) or set(body) - allowed
            or not {"expected_revision", "bio", "photo", "aliases"}.issubset(body)):
        return admin.JSONResponse({"ok": False, "error": "Invalid performer profile data."}, status_code=400)
    reset_fields = body.get("reset_fields", [])
    if not isinstance(reset_fields, list) or any(not isinstance(field, str) for field in reset_fields):
        return admin.JSONResponse({"ok": False, "error": "Invalid profile reset fields."}, status_code=400)
    return None


def _save_profile_update(admin, name, body):
    reset_fields = body.get("reset_fields", [])
    conn = None
    try:
        from common.performer_profiles import (
            PerformerProfileConflict, PerformerProfileError, update_profile,
        )
        conn = db.connect(admin.HUB_DB_URL)
        with conn:
            performer = conn.execute(
                "SELECT performer_name FROM video_performers WHERE lower(performer_name)=lower(%s) "
                "ORDER BY performer_name LIMIT 1 FOR UPDATE",
                (name,),
            ).fetchone()
            if not performer:
                return admin.JSONResponse({"ok": False, "error": "Performer not found."}, status_code=404)
            profile = update_profile(
                conn, name=str(performer[0]), expected_revision=body["expected_revision"],
                profile={"bio": body["bio"], "photo": body["photo"], "aliases": body["aliases"]},
                reset_fields=reset_fields,
            )
        return {"ok": True, "profile": profile}
    except PerformerProfileConflict as exc:
        return admin.JSONResponse({"ok": False, "error": str(exc), "revision": exc.revision}, status_code=409)
    except PerformerProfileError as exc:
        return admin.JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception:
        logger.warning("Request failed in update_person_profile", exc_info=True)
        return admin.JSONResponse({"ok": False, "error": "Could not update performer profile."}, status_code=503)
    finally:
        if conn is not None:
            conn.close()


def _route_update_person_profile(router, admin):
    @router.put("/api/v2/organize/people/{name}/profile")
    async def update_person_profile(request: Request, name: str):
        denied = admin._write_denied(request)
        if denied is not None:
            return denied
        if not name or len(name) > 200:
            return admin.JSONResponse({"ok": False, "error": "Invalid performer name."}, status_code=400)
        body, rejection = await _read_profile_update_body(admin, request)
        if rejection is not None:
            return rejection
        rejection = _profile_update_rejection(admin, body)
        if rejection is not None:
            return rejection
        return _save_profile_update(admin, name, body)
