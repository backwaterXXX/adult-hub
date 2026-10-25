"""Admin v2 review page API for Telegram performer match proposals."""

import math
import json

from fastapi import APIRouter, Query, Request
from admin_v2_errors import V2ErrorRoute

MAX_PROPOSAL_ACTION_BYTES = 8192


def build_router(admin):
    router = APIRouter(route_class=V2ErrorRoute)

    @router.get("/api/v2/performers/fuzzy")
    async def fuzzy(request: Request, threshold: float = Query(0.88)):
        if admin._admin_blocked(request):
            return admin.JSONResponse(
                {"ok": False, "error": "Admin is only available locally."}, status_code=403)
        if not math.isfinite(threshold) or not 0.80 <= threshold <= 0.99:
            return admin.JSONResponse(
                {"ok": False, "error": "Threshold must be between 0.80 and 0.99."},
                status_code=400,
            )
        try:
            from common.fuzzy_performers import find_fuzzy_performer_candidates
            result = await admin.asyncio.to_thread(
                find_fuzzy_performer_candidates,
                admin.HUB_DB_URL,
                similarity_threshold=threshold,
            )
            return result
        except Exception:
            return admin.JSONResponse(
                {"ok": False, "error": "Could not scan performer name variants."},
                status_code=503,
            )

    @router.get("/api/v2/performers/proposals")
    async def proposals(
        request: Request,
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0, le=1_000_000),
    ):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"error": "Admin is only available locally."}, status_code=403)
        try:
            from common.performer_proposals import list_pending_proposals
            return await admin.asyncio.to_thread(
                list_pending_proposals, admin.HUB_DB_URL, limit, offset)
        except Exception:
            return admin.JSONResponse({"error": "Could not load performer proposals."}, status_code=503)

    @router.get("/api/v2/performers/proposal-groups")
    async def proposal_groups(request: Request, limit: int = Query(50, ge=1, le=100),
                              offset: int = Query(0, ge=0, le=1_000_000)):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"error": "Admin is only available locally."}, status_code=403)
        try:
            from common.performer_proposals import list_pending_groups
            return await admin.asyncio.to_thread(list_pending_groups, admin.HUB_DB_URL, limit, offset)
        except Exception:
            return admin.JSONResponse({"error": "Could not summarize performer proposals."}, status_code=503)

    @router.post("/api/v2/performers/proposals/reconcile")
    async def reconcile(request: Request):
        denied = admin._write_denied(request)
        if denied is not None:
            return denied
        try:
            raw = await request.body()
            if len(raw) > 1024:
                return admin.JSONResponse({"error": "Request is too large."}, status_code=413)
            body = json.loads(raw or b"{}")
        except Exception:
            return admin.JSONResponse({"error": "Specify a valid reconciliation cursor."}, status_code=400)
        if (not isinstance(body, dict) or set(body) - {"after_id"}
                or ("after_id" in body and (type(body["after_id"]) is not int or body["after_id"] < 0))):
            return admin.JSONResponse({"error": "Specify a valid reconciliation cursor."}, status_code=400)
        try:
            from common.performer_proposals import reconcile_clear_matches
            return await admin.asyncio.to_thread(
                reconcile_clear_matches, admin.HUB_DB_URL, body.get("after_id", 0),
                wake_worker=admin._wake_worker)
        except Exception:
            return admin.JSONResponse({"error": "Could not reconcile clear performer matches."}, status_code=503)

    @router.post("/api/v2/performers/proposal-groups/{reason}/{key}")
    async def review_proposal_group(request: Request, reason: str, key: str):
        denied = admin._write_denied(request)
        if denied is not None:
            return denied
        try:
            body = json.loads(await request.body())
        except Exception:
            body = None
        if not isinstance(body, dict) or body.get("action") not in {"approve", "reject"}:
            return admin.JSONResponse({"error": "Choose approve or reject."}, status_code=400)
        allowed = {"action"} if body["action"] == "reject" else {"action", "performer_id", "canonical"}
        if set(body) - allowed or (body["action"] == "approve" and
                                   (int("performer_id" in body) + int("canonical" in body) != 1)):
            return admin.JSONResponse({"error": "Specify one matching performer or local name."}, status_code=400)
        if body["action"] == "approve":
            field = "performer_id" if "performer_id" in body else "canonical"
            maximum = 200 if field == "performer_id" else 160
            if not isinstance(body[field], str) or not 1 <= len(body[field]) <= maximum:
                return admin.JSONResponse({"error": "Invalid performer choice."}, status_code=400)
            if field == "performer_id" and not body[field].strip():
                return admin.JSONResponse({"error": "Choose a catalogue performer."}, status_code=400)
        try:
            from common.performer_proposals import ProposalError, review_proposal_group as review_group
            result = await admin.asyncio.to_thread(
                review_group, admin.HUB_DB_URL, key, reason, body, wake_worker=admin._wake_worker)
            return result
        except ProposalError as exc:
            return admin.JSONResponse({"error": exc.message}, status_code=exc.status_code)
        except Exception:
            return admin.JSONResponse({"error": "Could not review performer group."}, status_code=503)

    @router.get("/api/v2/performers/catalog")
    async def catalog(request: Request, q: str = Query("", max_length=200), limit: int = Query(100, ge=1, le=200)):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"error": "Admin is only available locally."}, status_code=403)
        try:
            from common.performer_proposals import search_catalog
            return await admin.asyncio.to_thread(search_catalog, admin.HUB_DB_URL, q, limit)
        except Exception:
            return admin.JSONResponse({"error": "Could not load performer catalogue."}, status_code=503)

    @router.post("/api/v2/performers/proposals/{proposal_id}")
    async def review(request: Request, proposal_id: str):
        denied = admin._write_denied(request)
        if denied is not None:
            return denied
        try:
            numeric_id = int(proposal_id)
        except (TypeError, ValueError):
            return admin.JSONResponse({"error": "Proposal ID must be a positive integer."}, status_code=400)
        if numeric_id <= 0:
            return admin.JSONResponse({"error": "Proposal ID must be a positive integer."}, status_code=400)
        content_length = request.headers.get("content-length")
        try:
            if content_length is not None and int(content_length) > MAX_PROPOSAL_ACTION_BYTES:
                return admin.JSONResponse({"error": "Request is too large."}, status_code=413)
        except ValueError:
            return admin.JSONResponse({"error": "Invalid request size."}, status_code=400)
        raw = bytearray()
        try:
            async for chunk in request.stream():
                if len(raw) + len(chunk) > MAX_PROPOSAL_ACTION_BYTES:
                    return admin.JSONResponse({"error": "Request is too large."}, status_code=413)
                raw.extend(chunk)
            body = json.loads(raw)
        except Exception:
            body = None
        if not isinstance(body, dict) or not isinstance(body.get("action"), str):
            return admin.JSONResponse({"error": "Invalid request data."}, status_code=400)
        if body["action"] == "reject":
            if set(body) != {"action"}:
                return admin.JSONResponse({"error": "Invalid reject request."}, status_code=400)
        elif body["action"] == "approve":
            if set(body) == {"action", "performer_id"}:
                if not isinstance(body["performer_id"], str) or not 1 <= len(body["performer_id"]) <= 200:
                    return admin.JSONResponse({"error": "Invalid performer ID."}, status_code=400)
            elif set(body) == {"action", "canonical"}:
                if not isinstance(body["canonical"], str) or not 1 <= len(body["canonical"]) <= 160:
                    return admin.JSONResponse({"error": "Enter a valid local performer name."}, status_code=400)
            else:
                return admin.JSONResponse({"error": "Specify one catalog performer or one local performer name."}, status_code=400)
        else:
            return admin.JSONResponse({"error": "Action must be approve or reject."}, status_code=400)
        try:
            from common.performer_proposals import ProposalError, review_proposal
            return await admin.asyncio.to_thread(
                review_proposal,
                admin.HUB_DB_URL,
                numeric_id,
                body if isinstance(body, dict) else {},
                wake_worker=admin._wake_worker,
            )
        except ProposalError as exc:
            return admin.JSONResponse({"error": exc.message}, status_code=exc.status_code)
        except Exception:
            return admin.JSONResponse({"error": "Could not review performer proposal."}, status_code=503)

    return router
