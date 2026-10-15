"""Quality review and reversible finding triage API for Admin v2."""

import json

from fastapi import APIRouter, Query, Request
from admin_v2_errors import V2ErrorRoute

from common.admin_v2_quality import QUALITY_FIELDS, QUALITY_KINDS


def build_router(admin):
    router = APIRouter(route_class=V2ErrorRoute)

    @router.get("/api/v2/quality/summary")
    async def summary(request: Request):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"ok": False, "error": "Admin is only available locally."}, status_code=403)
        try:
            from common.admin_quality import read_quality_summary
            return await admin.asyncio.to_thread(read_quality_summary, admin.HUB_DB_URL)
        except Exception:
            return admin.JSONResponse({"ok": False, "error": "Quality summary is unavailable."}, status_code=503)

    @router.get("/api/v2/quality/items")
    async def items(request: Request, kind: str = Query(""), status: str = Query("open"),
                    field: str = Query(""),
                    limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=1_000_000)):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"ok": False, "error": "Admin is only available locally."}, status_code=403)
        if kind and kind not in QUALITY_KINDS:
            return admin.JSONResponse({"ok": False, "error": "Invalid quality category."}, status_code=400)
        if status not in {"open", "resolved", "acknowledged", "all"}:
            return admin.JSONResponse({"ok": False, "error": "Invalid quality status."}, status_code=400)
        if field and field not in QUALITY_FIELDS:
            return admin.JSONResponse({"ok": False, "error": "Invalid quality field."}, status_code=400)
        try:
            from common.admin_quality import read_quality_items
            return await admin.asyncio.to_thread(read_quality_items, admin.HUB_DB_URL,
                                                 kind=kind, status=status, field=field, limit=limit, offset=offset)
        except Exception:
            return admin.JSONResponse({"ok": False, "error": "Quality findings are unavailable."}, status_code=503)

    @router.get("/api/v2/quality/proposals")
    async def proposals(request: Request, limit: int = Query(30, ge=1, le=100),
                        offset: int = Query(0, ge=0, le=1_000_000)):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"ok": False, "error": "Admin is only available locally."}, status_code=403)
        try:
            from common.metadata_review import list_metadata_review_proposals
            return await admin.asyncio.to_thread(list_metadata_review_proposals, admin.HUB_DB_URL,
                                                 limit=limit, offset=offset)
        except Exception:
            return admin.JSONResponse({"ok": False, "error": "Metadata proposals are unavailable."}, status_code=503)

    @router.post("/api/v2/quality/proposals/{proposal_id}/reject")
    async def reject_proposal(request: Request, proposal_id: int):
        denied = admin._write_denied(request)
        if denied is not None:
            return denied
        try:
            from common.metadata_review import reject_metadata_review_proposal
            result = await admin.asyncio.to_thread(reject_metadata_review_proposal,
                                                   admin.HUB_DB_URL, proposal_id)
        except Exception:
            return admin.JSONResponse({"ok": False, "error": "Could not reject this metadata proposal."}, status_code=503)
        if result["outcome"] == "not_found":
            return admin.JSONResponse({"ok": False, "error": "Metadata proposal was not found."}, status_code=404)
        if result["outcome"] in {"invalid", "conflict"}:
            return admin.JSONResponse({"ok": False, "error": "This metadata proposal is no longer pending."}, status_code=409)
        return {"ok": True, "id": result["id"], "status": "rejected"}

    @router.post("/api/v2/quality/acknowledge")
    async def acknowledge(request: Request):
        denied = admin._write_denied(request)
        if denied is not None:
            return denied
        try:
            raw = await request.body()
            if len(raw) > 4096:
                return admin.JSONResponse({"ok": False, "error": "Request is too large."}, status_code=413)
            body = json.loads(raw)
        except Exception:
            return admin.JSONResponse({"ok": False, "error": "Specify a valid finding and reason."}, status_code=400)
        if (not isinstance(body, dict) or set(body) != {"finding_id", "video_id", "kind", "note"}
                or type(body.get("finding_id")) is not int or not isinstance(body.get("video_id"), str)
                or not isinstance(body.get("kind"), str) or not isinstance(body.get("note"), str)):
            return admin.JSONResponse({"ok": False, "error": "Specify a valid finding and reason."}, status_code=400)
        try:
            from common.admin_quality import acknowledge_quality_finding
            result = await admin.asyncio.to_thread(acknowledge_quality_finding, admin.HUB_DB_URL,
                                                  body["finding_id"], body["video_id"], body["kind"], body["note"])
        except Exception:
            return admin.JSONResponse({"ok": False, "error": "Could not acknowledge this finding."}, status_code=503)
        if result["outcome"] == "invalid":
            return admin.JSONResponse({"ok": False, "error": result["message"]}, status_code=400)
        if result["outcome"] in {"stale", "conflict"}:
            return admin.JSONResponse({"ok": False, "error": result["message"]}, status_code=409)
        return {"ok": True, "acknowledgement_id": result["acknowledgement_id"]}

    @router.post("/api/v2/quality/reopen")
    async def reopen(request: Request):
        denied = admin._write_denied(request)
        if denied is not None:
            return denied
        try:
            raw = await request.body()
            if len(raw) > 1024:
                return admin.JSONResponse({"ok": False, "error": "Request is too large."}, status_code=413)
            body = json.loads(raw)
        except Exception:
            return admin.JSONResponse({"ok": False, "error": "Specify a valid acknowledgement."}, status_code=400)
        if not isinstance(body, dict) or set(body) != {"acknowledgement_id"} \
                or type(body.get("acknowledgement_id")) is not int:
            return admin.JSONResponse({"ok": False, "error": "Specify a valid acknowledgement."}, status_code=400)
        try:
            from common.admin_quality import reopen_quality_finding
            result = await admin.asyncio.to_thread(reopen_quality_finding, admin.HUB_DB_URL,
                                                  body["acknowledgement_id"])
        except Exception:
            return admin.JSONResponse({"ok": False, "error": "Could not reopen this finding."}, status_code=503)
        if result["outcome"] == "invalid":
            return admin.JSONResponse({"ok": False, "error": result["message"]}, status_code=400)
        if result["outcome"] == "not_found":
            return admin.JSONResponse({"ok": False, "error": "Acknowledgement was not found."}, status_code=404)
        return {"ok": True, "acknowledgement_id": result["acknowledgement_id"]}

    return router
