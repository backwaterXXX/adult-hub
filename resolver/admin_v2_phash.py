"""Authenticated pHash queue intake and compatible client frame API."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re

import httpx
from fastapi import APIRouter, Request, Response
from admin_v2_errors import V2ErrorRoute
from common import db
from common.http_rate_limit import wait_for_host_slot


MAX_BODY_BYTES = 8 * 1024 * 1024
PHASH_ALGORITHM_VERSION = "stash-phash-689a6b400625d6da2afb58a7f908c88bcdb6f8ab-v2"
_VIDEO_ID = re.compile(r"telegram:-?\d+:\d+:\d{1,20}\Z")
_HASH = re.compile(r"[0-9a-f]{16}\Z")
_SERVER_PROFILE = "resolver-ffmpeg-stash-v2"
logger = logging.getLogger(__name__)


def _valid_frame_request(body: object, profiles: set[str]) -> bool:
    """Validate the outer frame contract without trusting JSON value types."""
    required = {"video_id", "duration_us", "sampling_version", "decoder_profile", "frames"}
    allowed = required | {"thumbnail_jpeg_base64"}
    return (
        isinstance(body, dict)
        and required.issubset(body)
        and set(body).issubset(allowed)
        and isinstance(body["video_id"], str)
        and bool(_VIDEO_ID.fullmatch(body["video_id"]))
        and type(body["duration_us"]) is int
        and body["duration_us"] > 0
        and type(body["sampling_version"]) is int
        and body["sampling_version"] == 1
        and isinstance(body["decoder_profile"], str)
        and body["decoder_profile"] in profiles
        and isinstance(body["frames"], list)
        and len(body["frames"]) == 25
    )


def build_router(admin):
    router = APIRouter(route_class=V2ErrorRoute)

    def accepted_profiles() -> set[str]:
        return {part.strip() for part in os.getenv("PHASH_ACCEPTED_DECODER_PROFILES", "").split(",")
                if part.strip()}

    async def authorized_user(token: str) -> dict | None:
        jellyfin_url = os.getenv("JELLYFIN_URL", "").strip().rstrip("/")
        if not token or not jellyfin_url:
            return None
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                authorization = (
                    'MediaBrowser Client="Adult Hub Resolver", '
                    'Device="Resolver", DeviceId="adult-hub-resolver", '
                    'Version="1", '
                    f'Token="{token}"'
                )
                response = await client.get(
                    jellyfin_url + "/Users/Me", headers={"Authorization": authorization},
                )
            if response.status_code != 200:
                return None
            user = response.json()
            user_id = user.get("Id") if isinstance(user, dict) else None
            if not isinstance(user_id, str) or not user_id:
                return None
            allowed = {part.strip().lower() for part in os.getenv("PHASH_ALLOWED_USER_IDS", "").split(",")
                       if part.strip()}
            is_admin = bool(((user.get("Policy") or {}).get("IsAdministrator")))
            return user if is_admin or user_id.lower() in allowed else None
        except Exception as _exc:
            logger.debug("Ignored exception in %s", "authorized_user", exc_info=True)
            return None

    def phash_status(video_id: str, decoder_profile: str) -> tuple[bool, bool]:
        if not _VIDEO_ID.fullmatch(video_id):
            return False, False
        conn = db.connect(admin.HUB_DB_URL)
        try:
            exists = conn.execute(
                "SELECT 1 FROM videos WHERE id=%s AND provider='telegram'", (video_id,),
            ).fetchone() is not None
            current = conn.execute(
                "SELECT fingerprint,generator_version FROM video_phash_provenance WHERE video_id=%s",
                (video_id,),
            ).fetchone()
            thumbnail = conn.execute(
                "SELECT 1 FROM video_phash_thumbnails WHERE video_id=%s", (video_id,),
            ).fetchone()
            already = bool(current and thumbnail
                           and current[1] == PHASH_ALGORITHM_VERSION + ":" + decoder_profile
                           and _HASH.fullmatch(str(current[0] or "")))
            return exists, already
        finally:
            conn.close()

    @router.get("/api/v2/video-phash/status")
    async def get_status(request: Request, video_id: str, decoder_profile: str = ""):
        if not os.getenv("PHASH_UPLOAD_ENABLED", "false").lower() == "true":
            return {"enabled": False}
        token = request.headers.get("X-Emby-Token", "")
        if await authorized_user(token) is None:
            return admin.JSONResponse({"error": "Unauthorized."}, status_code=401)
        profiles = accepted_profiles()
        if not profiles:
            return {"enabled": False}
        if decoder_profile not in profiles:
            return admin.JSONResponse({"error": "Unsupported decoder profile."}, status_code=400)
        try:
            exists, already = await asyncio.to_thread(phash_status, video_id, decoder_profile)
        except Exception as _exc:
            logger.debug("Ignored exception in %s", "get_status", exc_info=True)
            return admin.JSONResponse({"error": "Fingerprint service unavailable."}, status_code=503)
        if not exists:
            return admin.JSONResponse({"error": "Video not found."}, status_code=404)
        return {"enabled": True, "already_computed": already, "accepted_profile": decoder_profile}

    @router.post("/api/v2/video-phash/frames")
    async def submit_frames(request: Request):
        if not os.getenv("PHASH_UPLOAD_ENABLED", "false").lower() == "true":
            return admin.JSONResponse({"error": "Fingerprint uploads are disabled."}, status_code=404)
        user = await authorized_user(request.headers.get("X-Emby-Token", ""))
        if user is None:
            return admin.JSONResponse({"error": "Unauthorized."}, status_code=401)
        length = request.headers.get("content-length")
        try:
            if length is not None and int(length) > MAX_BODY_BYTES:
                return admin.JSONResponse({"error": "Request is too large."}, status_code=413)
        except ValueError:
            return admin.JSONResponse({"error": "Invalid request size."}, status_code=400)
        raw = bytearray()
        try:
            async for chunk in request.stream():
                if len(raw) + len(chunk) > MAX_BODY_BYTES:
                    return admin.JSONResponse({"error": "Request is too large."}, status_code=413)
                raw.extend(chunk)
            body = json.loads(raw)
        except Exception as _exc:
            logger.debug("Ignored exception in %s", "submit_frames", exc_info=True)
            return admin.JSONResponse({"error": "Invalid frame request."}, status_code=400)
        profiles = accepted_profiles()
        if not _valid_frame_request(body, profiles):
            return admin.JSONResponse({"error": "Invalid frame request."}, status_code=400)
        try:
            frames = _decode_frames(body["frames"], body["duration_us"])
        except Exception as _exc:
            logger.debug("Ignored exception in %s", "submit_frames", exc_info=True)
            return admin.JSONResponse({"error": "Invalid or incomplete frames."}, status_code=400)

        thumbnail_bytes = None
        try:
            thumbnail_bytes = _decode_thumbnail(body.get("thumbnail_jpeg_base64"))
            from stash_phash import perception_hash, phash_to_string
            from stash_phash.video import combine_images

            sprite = combine_images(frames)
            fingerprint = phash_to_string(perception_hash(sprite)).lower()
            if not _HASH.fullmatch(fingerprint):
                raise ValueError("Unexpected pHash format")
            result = await asyncio.to_thread(
                _store, admin, body["video_id"], fingerprint, body["decoder_profile"], str(user["Id"]),
                thumbnail_bytes,
            )
            if result == "conflict":
                return admin.JSONResponse({"error": "A different validated fingerprint already exists."}, status_code=409)
        except Exception as _exc:
            logger.debug("Ignored exception in %s", "submit_frames", exc_info=True)
            return admin.JSONResponse({"error": "Could not save video fingerprint."}, status_code=503)

        match_found = None
        try:
            match_found = await _find_stashdb_match(fingerprint)
        except Exception as _exc:
            logger.debug("Ignored exception in %s", "submit_frames", exc_info=True)
        return {"ok": True, "fingerprint": fingerprint, "match_found": match_found}

    @router.post("/api/v2/video-phash/generate")
    async def generate_phash(request: Request):
        """Durably enqueue Stash-compatible pHash and thumbnail generation."""
        if os.getenv("PHASH_UPLOAD_ENABLED", "false").lower() != "true":
            return admin.JSONResponse({"error": "Fingerprint generation is disabled."}, status_code=404)
        user = await authorized_user(request.headers.get("X-Emby-Token", ""))
        if user is None:
            return admin.JSONResponse({"error": "Unauthorized."}, status_code=401)
        try:
            body = await request.json()
        except Exception as _exc:
            logger.debug("Ignored exception in %s", "generate_phash", exc_info=True)
            return admin.JSONResponse({"error": "Invalid generation request."}, status_code=400)
        if (not isinstance(body, dict) or set(body) != {"video_id", "duration_us"}
                or not isinstance(body.get("video_id"), str)
                or not _VIDEO_ID.fullmatch(body["video_id"])
                or type(body.get("duration_us")) is not int
                or not 1 <= body["duration_us"] <= 86_400_000_000):
            return admin.JSONResponse({"error": "Invalid generation request."}, status_code=400)
        if _SERVER_PROFILE not in accepted_profiles():
            return admin.JSONResponse({"error": "Server pHash generation is not enabled."}, status_code=404)
        video_id = body["video_id"]
        try:
            from common import phash_jobs
            exists, _already = await asyncio.to_thread(phash_status, video_id, _SERVER_PROFILE)
            if not exists:
                return admin.JSONResponse({"error": "Video not found."}, status_code=404)
            result = await asyncio.to_thread(
                phash_jobs.enqueue, admin.HUB_DB_URL,
                video_id=video_id, duration_us=body["duration_us"],
                algorithm_version=PHASH_ALGORITHM_VERSION, decoder_profile=_SERVER_PROFILE,
                user_id=str(user["Id"]),
            )
            if result["outcome"] == "not_found":
                return admin.JSONResponse({"error": "Video not found."}, status_code=404)
            job = result["job"]
            response = {
                "ok": True, "job_id": job["id"], "state": job["state"],
                "already_computed": result["outcome"] == "complete",
                "thumbnail_ready": result["outcome"] == "complete",
                "error_code": job.get("error_code"),
            }
            if result["outcome"] in {"queued", "existing"}:
                return admin.JSONResponse(response, status_code=202)
            if result["outcome"] == "terminal":
                return admin.JSONResponse({"error": "This pHash task is terminal; retry it from Admin V2."}, status_code=409)
            return response
        except Exception:
            logger.exception("Could not enqueue server pHash generation")
            return admin.JSONResponse({"error": "Could not queue video fingerprint generation."}, status_code=503)

    @router.get("/api/v2/video-phash/jobs/{job_id}")
    async def get_phash_job(request: Request, job_id: str):
        if not os.getenv("PHASH_UPLOAD_ENABLED", "false").lower() == "true":
            return admin.JSONResponse({"error": "Fingerprint generation is disabled."}, status_code=404)
        user = await authorized_user(request.headers.get("X-Emby-Token", ""))
        if user is None:
            return admin.JSONResponse({"error": "Unauthorized."}, status_code=401)
        try:
            from common.phash_jobs import read_job
            job = await asyncio.to_thread(read_job, admin.HUB_DB_URL, job_id)
        except Exception as _exc:
            logger.debug("Ignored exception in %s", "get_phash_job", exc_info=True)
            return admin.JSONResponse({"error": "Could not load pHash task."}, status_code=503)
        if job is None:
            return admin.JSONResponse({"error": "Task not found."}, status_code=404)
        return {"ok": True, "job": job}

    @router.get("/api/v2/video-phash/thumbnail")
    async def get_phash_thumbnail(request: Request, video_id: str):
        user = await authorized_user(request.headers.get("X-Emby-Token", ""))
        if user is None:
            return admin.JSONResponse({"error": "Unauthorized."}, status_code=401)
        if not _VIDEO_ID.fullmatch(video_id):
            return admin.JSONResponse({"error": "Video not found."}, status_code=404)
        try:
            image = await asyncio.to_thread(_read_thumbnail, admin, video_id)
        except Exception as _exc:
            logger.debug("Ignored exception in %s", "get_phash_thumbnail", exc_info=True)
            return admin.JSONResponse({"error": "Thumbnail service unavailable."}, status_code=503)
        if image is None:
            return admin.JSONResponse({"error": "Thumbnail not found."}, status_code=404)
        return Response(content=image, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

    return router


def _decode_thumbnail(encoded: object) -> bytes | None:
    if encoded is None:
        return None
    if not isinstance(encoded, str) or len(encoded) > 2_000_000:
        raise ValueError("Invalid thumbnail")
    image_bytes = base64.b64decode(encoded, validate=True)
    from io import BytesIO
    from PIL import Image
    with Image.open(BytesIO(image_bytes)) as image:
        if image.format != "JPEG" or not (320 <= image.width <= 1280) or not (1 <= image.height <= 2048):
            raise ValueError("Unsupported thumbnail format")
        image.load()
    return image_bytes


def _read_thumbnail(admin, video_id: str) -> bytes | None:
    conn = db.connect(admin.HUB_DB_URL)
    try:
        row = conn.execute(
            "SELECT image_bytes FROM video_phash_thumbnails WHERE video_id=%s", (video_id,),
        ).fetchone()
        return bytes(row[0]) if row and row[0] is not None else None
    finally:
        conn.close()


def _decode_frames(raw_frames: list, duration_us: int):
    from io import BytesIO
    from PIL import Image

    if len(raw_frames) != 25:
        raise ValueError("Exactly 25 frames are required")
    expected = [int(duration_us * (0.05 + 0.90 * index / 25)) for index in range(25)]
    decoded = []
    frame_height = None
    for index, record in enumerate(raw_frames):
        if (not isinstance(record, dict) or set(record) != {"index", "timestamp_us", "png_base64"}
                or type(record["index"]) is not int or record["index"] != index
                or type(record["timestamp_us"]) is not int
                or abs(record["timestamp_us"] - expected[index]) > 1_000_000
                or not isinstance(record["png_base64"], str)):
            raise ValueError("Incomplete or unordered frame set")
        encoded = record["png_base64"]
        if len(encoded) > 350_000:
            raise ValueError("Frame exceeds the size limit")
        image_bytes = base64.b64decode(encoded, validate=True)
        with Image.open(BytesIO(image_bytes)) as image:
            if image.format != "PNG" or image.width != 160 or not 1 <= image.height <= 512:
                raise ValueError("Unsupported frame format")
            if frame_height is None:
                frame_height = image.height
            if image.height != frame_height:
                raise ValueError("Frame geometry changed during sampling")
            image.load()
            decoded.append(image.convert("RGBA"))
    return decoded


def _store(
    admin, video_id: str, fingerprint: str, profile: str, user_id: str,
    thumbnail_bytes: bytes | None = None, *, connection=None,
) -> str:
    owns_connection = connection is None
    conn = connection or db.connect(admin.HUB_DB_URL)
    try:
        row = conn.execute(
            # Serialize first-time submissions too: locking only an existing
            # provenance row leaves the absent-row case open to concurrent
            # INSERT ... ON CONFLICT updates that silently replace one hash.
            "SELECT provider FROM videos WHERE id=%s FOR UPDATE", (video_id,),
        ).fetchone()
        if not row or row[0] != "telegram":
            raise ValueError("Video not found")
        existing = conn.execute(
            "SELECT fingerprint,generator_version FROM video_phash_provenance WHERE video_id=%s FOR UPDATE",
            (video_id,),
        ).fetchone()
        if existing:
            expected_generator = PHASH_ALGORITHM_VERSION + ":" + profile
            if existing[1] == expected_generator and existing[0] != fingerprint:
                if owns_connection:
                    conn.rollback()
                return "conflict"
            if existing[1] != expected_generator:
                conn.execute(
                    """INSERT INTO video_fingerprints(video_id,algorithm,fingerprint)
                       VALUES (%s,'PHASH',%s)
                       ON CONFLICT(video_id,algorithm) DO UPDATE SET
                       fingerprint=EXCLUDED.fingerprint,computed_at=CURRENT_TIMESTAMP""",
                    (video_id, fingerprint),
                )
                conn.execute(
                    """UPDATE video_phash_provenance SET fingerprint=%s,generator_version=%s,
                       decoder_profile=%s,submitted_by_user_id=%s,submitted_at=CURRENT_TIMESTAMP
                       WHERE video_id=%s""",
                    (fingerprint, expected_generator, profile, user_id, video_id),
                )
        else:
            conn.execute(
                """INSERT INTO video_fingerprints(video_id,algorithm,fingerprint)
                   VALUES (%s,'PHASH',%s)
                   ON CONFLICT(video_id,algorithm) DO UPDATE SET
                   fingerprint=EXCLUDED.fingerprint,computed_at=CURRENT_TIMESTAMP""",
                (video_id, fingerprint),
            )
            conn.execute(
                """INSERT INTO video_phash_provenance
                   (video_id,fingerprint,generator_version,decoder_profile,submitted_by_user_id)
                   VALUES (%s,%s,%s,%s,%s)""",
                (video_id, fingerprint, PHASH_ALGORITHM_VERSION + ":" + profile, profile, user_id),
            )
        if thumbnail_bytes is not None:
            conn.execute(
                """INSERT INTO video_phash_thumbnails(video_id,image_bytes)
                   VALUES (%s,%s)
                   ON CONFLICT(video_id) DO UPDATE SET
                   image_bytes=EXCLUDED.image_bytes,updated_at=CURRENT_TIMESTAMP""",
                (video_id, thumbnail_bytes),
            )
        if owns_connection:
            conn.commit()
        return "saved"
    except Exception:
        if owns_connection:
            conn.rollback()
        raise
    finally:
        if owns_connection:
            conn.close()


async def _find_stashdb_match(fingerprint: str) -> bool | None:
    token = os.getenv("STASHDB_API_KEY", "").strip() or os.getenv("STASH_API_KEY", "").strip()
    if not token:
        return None
    query = """
      query FindScenesByFingerprint($fingerprints: [[FingerprintQueryInput!]!]!) {
        findScenesBySceneFingerprints(fingerprints: $fingerprints) {
          id fingerprints { hash algorithm }
        }
      }
    """
    stashdb_url = os.getenv("STASHDB_API_URL", "https://stashdb.org/graphql")
    # Keep concurrent resolver requests spaced like the sync process. Run the
    # blocking reservation/sleep off the event loop so playback and admin I/O
    # can continue while this optional lookup waits for its host slot.
    await asyncio.to_thread(wait_for_host_slot, stashdb_url)
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            stashdb_url,
            headers={"ApiKey": token, "Accept": "application/json", "Content-Type": "application/json"},
            json={"query": query, "variables": {"fingerprints": [[{"algorithm": "PHASH", "hash": fingerprint}]]}},
        )
    response.raise_for_status()
    payload = response.json()
    groups = ((payload.get("data") or {}).get("findScenesBySceneFingerprints")
              if isinstance(payload, dict) else None)
    candidates = []
    if isinstance(groups, list) and groups and isinstance(groups[0], list):
        for scene in groups[0]:
            if not isinstance(scene, dict) or not scene.get("id"):
                continue
            distances = [
                (int(str(item.get("hash") or ""), 16) ^ int(fingerprint, 16)).bit_count()
                for item in (scene.get("fingerprints") or []) if isinstance(item, dict)
                and str(item.get("algorithm") or "").upper() == "PHASH"
                and re.fullmatch(r"[0-9a-fA-F]{16}", str(item.get("hash") or ""))
            ]
            if distances and min(distances) <= 2:
                candidates.append(scene["id"])
    return len(set(candidates)) == 1
