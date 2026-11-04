"""Read-only people catalog for Admin v2's Organize workspace."""

from __future__ import annotations

import json
from pathlib import PurePosixPath
from fastapi import APIRouter, Request
from admin_v2_errors import V2ErrorRoute

from common import db
from admin_library_v2 import _safe_thumbnail
import logging

logger = logging.getLogger(__name__)



def build_router(admin):
    router = APIRouter(route_class=V2ErrorRoute)

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
        except Exception as _exc:
            logger.debug("Ignored exception in %s", "quarantine", exc_info=True)
            return admin.JSONResponse({"ok": False, "error": "Could not load quarantine history."}, status_code=503)

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
            items = []
            for operation_id, raw_files, created_at in rows:
                files = raw_files if isinstance(raw_files, list) else json.loads(raw_files or "[]")
                projected = []
                for item in files:
                    path = item.get("path") if isinstance(item, dict) else None
                    parsed = PurePosixPath(path) if isinstance(path, str) else None
                    if (parsed is None or parsed.is_absolute() or ".." in parsed.parts or "\\" in path
                            or parsed.suffix.lower() not in {".strm", ".nfo", ".jpg"}):
                        continue
                    projected.append({"path": path, "size": int(item.get("size") or 0)})
                items.append({"operation_id": str(operation_id), "files": projected,
                              "file_count": len(projected), "created_at": created_at.isoformat()
                              if hasattr(created_at, "isoformat") else str(created_at)})
            return {"ok": True, "items": items, "total": total, "offset": offset, "limit": limit,
                    "truncated": offset + len(items) < total}
        except Exception as _exc:
            logger.debug("Ignored exception in %s", "file_quarantine", exc_info=True)
            return admin.JSONResponse({"ok": False, "error": "Could not load file quarantine history."}, status_code=503)
        finally:
            if conn is not None:
                conn.close()

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
            joins = []
            group_fields = ["p.performer_name"]
            bio_flag = "FALSE"
            photo_flag = "FALSE"
            aliases_expr = "'[]'::jsonb"
            alias_search = []
            if "performer_bios" in tables:
                joins.append("LEFT JOIN performer_bios b ON b.name = p.performer_name")
                bio_flag = "(b.bio IS NOT NULL AND btrim(b.bio) <> '')"
                aliases_expr = "COALESCE(b.aliases, '[]'::jsonb)"
                group_fields.extend(["b.name", "b.bio", "b.aliases"])
                alias_search.append(
                    "EXISTS (SELECT 1 FROM performer_bios pb, "
                    "jsonb_array_elements_text(COALESCE(pb.aliases, '[]'::jsonb)) AS alias_values(alias_value) "
                    "WHERE pb.name = p.performer_name AND alias_values.alias_value ILIKE %s)"
                )
            mapped_aliases_available = "performer_aliases" in tables
            if mapped_aliases_available:
                alias_search.append(
                    "EXISTS (SELECT 1 FROM performer_aliases pa "
                    "WHERE lower(pa.canonical) = lower(p.performer_name) AND pa.alias ILIKE %s)"
                )
            if "performer_photos" in tables:
                joins.append("LEFT JOIN performer_photos ph ON ph.name = p.performer_name")
                photo_flag = "(ph.thumb IS NOT NULL AND btrim(ph.thumb) <> '')"
                group_fields.extend(["ph.name", "ph.thumb"])
            where = "WHERE btrim(p.performer_name) <> ''"
            params = []
            if q:
                search_clauses = ["p.performer_name ILIKE %s", *alias_search]
                where += " AND (" + " OR ".join(search_clauses) + ")"
                params.extend([f"%{q}%"] * len(search_clauses))
            join_sql = " ".join(joins)
            total = conn.execute(
                f"SELECT count(DISTINCT p.performer_name) FROM video_performers p {where}", params
            ).fetchone()[0]
            rows = conn.execute(
                "SELECT p.performer_name, count(DISTINCT p.video_id), "
                f"{bio_flag}, {photo_flag}, {aliases_expr} "
                f"FROM video_performers p {join_sql} {where} "
                f"GROUP BY {', '.join(group_fields)} "
                "ORDER BY lower(p.performer_name), p.performer_name LIMIT %s OFFSET %s",
                [*params, limit, offset],
            ).fetchall()
            # Aliases of the mapped table are collected for the returned page alone. In
            # the grouped query PostgreSQL evaluates them per skipped row, which made
            # offset 2000 cost 2.8s where offset 0 cost 0.2s. Matching stays in SQL so
            # the page's names are compared with the same lower() the old subquery used.
            mapped_by_name = {}
            if mapped_aliases_available and rows:
                for page_name, mapped in conn.execute(
                    "SELECT n.name, jsonb_agg(pa.alias ORDER BY lower(pa.alias), pa.alias) "
                    "FROM unnest(%s::text[]) AS n(name) "
                    "JOIN performer_aliases pa ON lower(pa.canonical) = lower(n.name) "
                    "GROUP BY n.name",
                    ([str(row[0]) for row in rows],),
                ).fetchall():
                    mapped_by_name[page_name] = mapped
            items = []
            for name, count, has_bio, has_photo, aliases in rows:
                if isinstance(aliases, str):
                    try:
                        import json
                        aliases = json.loads(aliases)
                    except ValueError:
                        aliases = []
                mapped_aliases = mapped_by_name.get(name) or []
                if isinstance(mapped_aliases, str):
                    try:
                        import json
                        mapped_aliases = json.loads(mapped_aliases)
                    except ValueError:
                        mapped_aliases = []
                combined_aliases = []
                for alias in [*(aliases if isinstance(aliases, list) else []),
                              *(mapped_aliases if isinstance(mapped_aliases, list) else [])]:
                    if isinstance(alias, str) and alias.strip() and alias.strip().casefold() != str(name).casefold():
                        if all(alias.strip().casefold() != existing.casefold() for existing in combined_aliases):
                            combined_aliases.append(alias.strip())
                items.append({"name": str(name), "video_count": int(count),
                              "has_bio": bool(has_bio), "has_photo": bool(has_photo),
                              "aliases": combined_aliases})
            return {"ok": True, "items": items, "total": int(total), "offset": offset,
                    "limit": limit, "available": True}
        except Exception as _exc:
            logger.debug("Ignored exception in %s", "people", exc_info=True)
            return admin.JSONResponse({"ok": False, "error": "Could not load the people catalog."}, status_code=503)
        finally:
            if conn is not None:
                conn.close()

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
        except Exception as _exc:
            logger.debug("Ignored exception in %s", "person_detail", exc_info=True)
            return admin.JSONResponse({"ok": False, "error": "Could not load performer details."}, status_code=503)
        finally:
            if conn is not None:
                conn.close()

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
        except Exception as _exc:
            logger.debug("Ignored exception in %s", "person_profile", exc_info=True)
            return admin.JSONResponse({"ok": False, "error": "Could not load performer profile."}, status_code=503)
        finally:
            if conn is not None:
                conn.close()

    @router.put("/api/v2/organize/people/{name}/profile")
    async def update_person_profile(request: Request, name: str):
        denied = admin._write_denied(request)
        if denied is not None:
            return denied
        if not name or len(name) > 200:
            return admin.JSONResponse({"ok": False, "error": "Invalid performer name."}, status_code=400)
        content_length = request.headers.get("content-length")
        try:
            if content_length is not None and int(content_length) > 32 * 1024:
                return admin.JSONResponse({"ok": False, "error": "Request is too large."}, status_code=413)
        except ValueError:
            return admin.JSONResponse({"ok": False, "error": "Invalid request size."}, status_code=400)
        raw = bytearray()
        try:
            async for chunk in request.stream():
                if len(raw) + len(chunk) > 32 * 1024:
                    return admin.JSONResponse({"ok": False, "error": "Request is too large."}, status_code=413)
                raw.extend(chunk)
            body = json.loads(raw)
        except Exception:
            body = None
        allowed = {"expected_revision", "bio", "photo", "aliases", "reset_fields"}
        if (not isinstance(body, dict) or set(body) - allowed
                or not {"expected_revision", "bio", "photo", "aliases"}.issubset(body)):
            return admin.JSONResponse({"ok": False, "error": "Invalid performer profile data."}, status_code=400)
        reset_fields = body.get("reset_fields", [])
        if not isinstance(reset_fields, list) or any(not isinstance(field, str) for field in reset_fields):
            return admin.JSONResponse({"ok": False, "error": "Invalid profile reset fields."}, status_code=400)
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
        except Exception as _exc:
            logger.debug("Ignored exception in %s", "update_person_profile", exc_info=True)
            return admin.JSONResponse({"ok": False, "error": "Could not update performer profile."}, status_code=503)
        finally:
            if conn is not None:
                conn.close()

    return router
