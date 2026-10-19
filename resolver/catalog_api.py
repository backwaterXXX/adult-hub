"""Catalog and provider API routes for the resolver."""
import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from providers import get_provider
from providers.base import normalize_performers

logger = logging.getLogger("resolver")
THUMB_API_TIMEOUT_SECONDS = 10.0
router = APIRouter()

@router.get("/api/categories")
async def get_categories(provider: str = "telegram"):
    try:
        prov = get_provider(provider)
        categories = await prov.get_categories()
        return [
            {
                "id": c.id,
                "title": c.title,
                "slug": c.slug,
                "url": c.url
            }
            for c in categories
        ]
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Provider '{provider}' not found")
    except Exception as e:
        logger.error(f"Error fetching categories for {provider}: {e}")
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/api/browse")
async def browse(section: str = Query("trending"), page: int = 1, provider: str = "telegram"):
    try:
        prov = get_provider(provider)
        videos = await prov.get_browse_videos(section=section, page=page)
        return [v.__dict__ for v in videos]
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Provider '{provider}' not found")
    except Exception as e:
        logger.error(f"Error browsing {section} ({provider}): {e}")
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/api/category/{category_id}")
async def get_category_videos(
    category_id: str,
    page: int = 1,
    sort: str = "latest",
    provider: str = "telegram",
    min_duration: Optional[int] = None,
    offset_id: Optional[int] = None,
    limit: Optional[int] = None,
):
    try:
        prov = get_provider(provider)
        if provider == "telegram":
            auth_check = getattr(prov, "is_channel_authorized", None)
            if callable(auth_check) and not auth_check(category_id):
                raise HTTPException(status_code=403, detail="Channel not authorized")
        kwargs = {"page": page, "sort": sort}
        if provider == "telegram":
            if min_duration is not None:
                kwargs["min_duration"] = min_duration
            if offset_id is not None:
                kwargs["offset_id"] = offset_id
            if limit is not None:
                kwargs["limit"] = limit
        videos = await prov.get_category_videos(category_id=category_id, **kwargs)
        return [v.__dict__ for v in videos]
    except HTTPException:
        raise
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Provider '{provider}' not found")
    except Exception as e:
        logger.error(f"Error fetching category {category_id} ({provider}): {e}")
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/api/telegram/history/{channel_id}")
async def get_telegram_history(channel_id: str, offset_id: Optional[int] = None,
                               limit: int = 100):
    try:
        prov = get_provider("telegram")
        auth_check = getattr(prov, "is_channel_authorized", None)
        if callable(auth_check) and not auth_check(channel_id):
            raise HTTPException(status_code=403, detail="Channel not authorized")
        return await prov.get_history_batch(
            channel_id, offset_id=offset_id, limit=max(1, min(limit, 500)))
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Telegram history fetch failed for %s: %s", channel_id, e)
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/api/telegram/message-check/{channel_id}/{message_id}")
async def check_telegram_message(channel_id: str, message_id: int, unique_id: str):
    """Metadata-only exact post check for the bounded sync quarantine worker."""
    try:
        prov = get_provider("telegram")
        auth_check = getattr(prov, "is_channel_authorized", None)
        if callable(auth_check) and not auth_check(channel_id):
            raise HTTPException(status_code=403, detail="Channel not authorized")
        status = await prov.check_message_metadata(channel_id, message_id, unique_id)
        return {"status": status}
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("Telegram exact message check failed for %s/%s: %s", channel_id, message_id, e)
        raise HTTPException(status_code=502, detail="Telegram message check was inconclusive")


@router.get("/api/search")
async def search_videos(q: str, page: int = 1, provider: str = "telegram"):
    sanitized_q = q.strip()
    if not sanitized_q:
        return []
    try:
        prov = get_provider(provider)
        videos = await prov.search(query=sanitized_q, page=page)
        return [v.__dict__ for v in videos]
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Provider '{provider}' not found")
    except Exception as e:
        logger.error(f"Error searching for '{sanitized_q}' ({provider}): {e}")
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/api/unsupported-embeds")
async def unsupported_embeds(provider: str = "telegram"):
    """Hosts whose embeds need a real browser (used by sync to hide dead entries)."""
    try:
        prov = get_provider(provider)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Provider '{provider}' not found")
    return list(getattr(prov, "UNSUPPORTED_EMBED_HOSTS", []))


@router.get("/api/video-performers")
async def video_performers(provider: str, pid: str, slug: str = ""):
    """Single-video performer enrichment (one upstream request).

    Used by sync to backfill performers for videos whose listing cards
    carry none. Providers without video_page_url/get_video_performers
    answer 404 (their cards already provide performers or none exist).
    """
    try:
        prov = get_provider(provider)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Provider '{provider}' not found")
    page_url_fn = getattr(prov, "video_page_url", None)
    fetch_fn = getattr(prov, "get_video_performers", None)
    page_url = page_url_fn(pid, slug) if callable(page_url_fn) else None
    if not page_url or not callable(fetch_fn):
        raise HTTPException(status_code=404, detail=f"Performer lookup not supported for '{provider}'")
    try:
        names = normalize_performers(await fetch_fn(page_url))
    except Exception as e:
        logger.error(f"Performer lookup failed for {provider}:{pid}: {e}")
        raise HTTPException(status_code=502, detail=str(e))
    return {"provider": provider, "id": pid, "performers": names}


@router.get("/api/thumb")
async def api_thumb(provider: str = Query(...), ref: str = Query(...)):
    """Message preview thumbnail (opcja C: tylko podglad, nigdy pelny plik).

    Obecnie obslugiwany provider: telegram (ref = {channel}:{msg}:{unique}).
    Sync pobiera to jak zwykly thumbnail do .jpg obok .strm.
    """
    try:
        prov = get_provider(provider)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Provider '{provider}' not found")
    if provider == "telegram":
        channel_ref = (ref or "").split(":", 1)[0]
        auth_check = getattr(prov, "is_channel_authorized", None)
        if callable(auth_check) and not auth_check(channel_ref):
            raise HTTPException(status_code=403, detail="Channel not authorized")
        try:
            import admin as admin_mod
            from admin_catalog_queries import _get_video_artwork_path
            from fastapi.responses import FileResponse
            local_art = await asyncio.to_thread(_get_video_artwork_path, getattr(admin_mod, "HUB_DB_URL", None), f"telegram:{ref}", getattr(admin_mod, "MEDIA_DIR", "/media/adult"))
            if local_art is not None and local_art.is_file():
                return FileResponse(
                    str(local_art),
                    media_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=3600"},
                )
        except Exception:
            pass
    fetch_fn = getattr(prov, "download_thumb", None)
    if not callable(fetch_fn):
        raise HTTPException(status_code=404, detail=f"Thumbs not supported for '{provider}'")
    try:
        # A preview must finish well before Jellyfin's 100-second image timeout.
        data = await asyncio.wait_for(fetch_fn(ref), timeout=THUMB_API_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Preview download timed out")
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Thumb fetch failed for {provider}:{ref[:40]}: {e}")
        raise HTTPException(status_code=502, detail=str(e))
    if not data:
        raise HTTPException(status_code=404, detail="No preview available")
    raw = bytes(data)
    try:
        from common.image_utils import is_black_image
        if is_black_image(raw):
            raise HTTPException(status_code=404, detail="Empty or black preview image")
    except HTTPException:
        raise
    except Exception:
        pass
    if raw.startswith(b"\xff\xd8"):
        media_type = "image/jpeg"
    elif raw.startswith(b"\x89PNG"):
        media_type = "image/png"
    elif raw.startswith(b"RIFF") and b"WEBP" in raw[:16]:
        media_type = "image/webp"
    else:
        media_type = "application/octet-stream"
    return Response(content=raw, media_type=media_type)
