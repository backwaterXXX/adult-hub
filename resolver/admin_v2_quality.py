"""Metadata suggestion review API for Admin v2."""

from fastapi import APIRouter, Query, Request
from admin_v2_errors import V2ErrorRoute
import logging

logger = logging.getLogger(__name__)




def build_router(admin):
    router = APIRouter(route_class=V2ErrorRoute)

    @router.get("/api/v2/quality/proposals")
    async def proposals(request: Request, limit: int = Query(30, ge=1, le=100),
                        offset: int = Query(0, ge=0, le=1_000_000)):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"ok": False, "error": "Admin is only available locally."}, status_code=403)
        try:
            from common.metadata_review import list_metadata_review_proposals
            return await admin.asyncio.to_thread(list_metadata_review_proposals, admin.HUB_DB_URL,
                                                 limit=limit, offset=offset)
        except Exception as _exc:
            logger.debug("Ignored exception in %s", "proposals", exc_info=True)
            return admin.JSONResponse({"ok": False, "error": "Metadata proposals are unavailable."}, status_code=503)

    @router.post("/api/v2/quality/proposals/{proposal_id}/refresh")
    async def refresh_proposal(request: Request, proposal_id: int):
        denied = admin._write_denied(request)
        if denied is not None:
            return denied
        try:
            from common.metadata_review import refresh_metadata_review_proposal
            result = await admin.asyncio.to_thread(refresh_metadata_review_proposal,
                                                   admin.HUB_DB_URL, proposal_id)
        except Exception as _exc:
            logger.debug("Ignored exception in %s", "refresh_proposal", exc_info=True)
            return admin.JSONResponse({"ok": False, "error": "Could not refresh this metadata proposal."}, status_code=503)
        if result["outcome"] == "not_found":
            return admin.JSONResponse({"ok": False, "error": "Metadata proposal was not found."}, status_code=404)
        if result["outcome"] in {"invalid", "conflict"}:
            return admin.JSONResponse({"ok": False, "error": "This metadata proposal is no longer pending."}, status_code=409)
        return {"ok": True, "id": result["id"], "revision": result["revision"]}

    @router.post("/api/v2/quality/proposals/{proposal_id}/reject")
    async def reject_proposal(request: Request, proposal_id: int):
        denied = admin._write_denied(request)
        if denied is not None:
            return denied
        try:
            from common.metadata_review import reject_metadata_review_proposal
            result = await admin.asyncio.to_thread(reject_metadata_review_proposal,
                                                   admin.HUB_DB_URL, proposal_id)
        except Exception as _exc:
            logger.debug("Ignored exception in %s", "reject_proposal", exc_info=True)
            return admin.JSONResponse({"ok": False, "error": "Could not reject this metadata proposal."}, status_code=503)
        if result["outcome"] == "not_found":
            return admin.JSONResponse({"ok": False, "error": "Metadata proposal was not found."}, status_code=404)
        if result["outcome"] in {"invalid", "conflict"}:
            return admin.JSONResponse({"ok": False, "error": "This metadata proposal is no longer pending."}, status_code=409)
        return {"ok": True, "id": result["id"], "status": "rejected"}

    return router
