"""HTTP range proxy for Telegram-backed media streams."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from fastapi import HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from common import telegram_playback_activity

_LOGGER = logging.getLogger(__name__)

# Fire-and-forget size-recording tasks need a reference until they finish.
_pending_size_records: set = set()


@dataclass(frozen=True)
class TelegramProxyServices:
    get_provider: Any
    parse_range_request: Any
    asyncio: Any
    inspect: Any
    record_file_size: Optional[Callable[[str, int], None]] = None


def _resolve_provider(services: TelegramProxyServices, ref: str):
    try:
        prov = services.get_provider("telegram")
    except KeyError:
        raise HTTPException(status_code=502, detail="Telegram provider not available")
    authorize = getattr(prov, "is_channel_authorized", None)
    if callable(authorize):
        channel_ref = ref.rsplit(":", 2)[0]
        if not authorize(channel_ref):
            raise HTTPException(status_code=403, detail="Telegram channel is not authorized")
    if not callable(getattr(prov, "download_range", None)):
        raise HTTPException(status_code=502, detail="Telegram provider has no download_range")
    return prov


async def _probe_total_size(
    services: TelegramProxyServices, prov, ref: str, *, required: bool = False
) -> Optional[int]:
    fs = getattr(prov, "file_size", None)
    if not callable(fs):
        return None
    try:
        res = fs(ref)
        if services.inspect.isawaitable(res):
            res = await res
        return int(res) if res is not None else None
    except HTTPException:
        raise
    except Exception as fs_error:
        _LOGGER.warning("Telegram file_size lookup failed for %s: %s", ref, fs_error)
        if required:
            raise HTTPException(status_code=503, detail=f"Telegram size lookup failed: {fs_error}")
        return None


def _range_headers(offset: int, length: Optional[int], total: Optional[int]) -> dict:
    """Return 206 headers for a satisfiable range, or raise 416."""
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": "application/octet-stream",
    }
    if total is not None:
        end = (offset + length - 1) if length is not None else (total - 1)
        if end >= total:
            end = total - 1
        if end < offset:
            raise HTTPException(status_code=416, detail="Range not satisfiable",
                                headers={"Content-Range": f"bytes */{total}"})
        headers["Content-Range"] = f"bytes {offset}-{end}/{total}"
        headers["Content-Length"] = str(end - offset + 1)
    else:
        if length is not None:
            end = offset + length - 1
            headers["Content-Range"] = f"bytes {offset}-{end}/*"
            headers["Content-Length"] = str(length)
        else:
            headers["Content-Range"] = f"bytes {offset}-/*"
    return headers


def _head_response(total: Optional[int]) -> Response:
    if total is not None:
        return Response(content=b"", media_type="application/octet-stream",
                        headers={"Accept-Ranges": "bytes",
                                 "Content-Length": str(total),
                                 "Content-Type": "application/octet-stream"})
    resp = Response(content=b"", media_type="application/octet-stream",
                    headers={"Accept-Ranges": "bytes"})
    # Starlette synthesizes Content-Length: 0 from the empty body;
    # without a known size that would be a dishonest size advertisement.
    if "content-length" in resp.headers:
        del resp.headers["content-length"]
    return resp


def _is_worker_request(request: Request) -> bool:
    # pHash reads use this same bounded range endpoint. Exclude only the
    # resolver's loopback worker requests; every client playback remains
    # visible to the cross-container rate controller.
    client_host = getattr(getattr(request, "client", None), "host", "")
    return (
        request.headers.get("x-adult-hub-transfer", "").lower() == "phash"
        and client_host in {"127.0.0.1", "::1"}
    )


async def _start_playback(services: TelegramProxyServices, ref: str, total: Optional[int]):
    if services.record_file_size is not None and total is not None:
        async def _record_size(_sink=services.record_file_size, _ref=ref, _total=total):
            try:
                await services.asyncio.to_thread(_sink, _ref, _total)
            except Exception as exc:
                _LOGGER.debug("Playback file-size record failed for %s: %s", _ref, exc)

        task = services.asyncio.create_task(_record_size())
        _pending_size_records.add(task)
        task.add_done_callback(_pending_size_records.discard)
    try:
        return await services.asyncio.to_thread(telegram_playback_activity.begin_playback)
    except Exception:
        # Playback must continue even if the optional activity ledger fails.
        return None


async def _stop_heartbeat(services: TelegramProxyServices, heartbeat_task) -> None:
    if heartbeat_task is None:
        return
    heartbeat_task.cancel()
    try:
        await heartbeat_task
    except services.asyncio.CancelledError:
        pass


async def _finish_stream(services: TelegramProxyServices, iterator, playback_handle) -> None:
    if playback_handle:
        try:
            await services.asyncio.to_thread(
                telegram_playback_activity.end_playback, playback_handle,
            )
        except Exception:
            _LOGGER.debug("Ignored exception in _tg_gen", exc_info=True)
    close_iterator = getattr(iterator, "aclose", None)
    if callable(close_iterator):
        try:
            await close_iterator()
        except Exception:
            _LOGGER.debug("Ignored exception in _tg_gen", exc_info=True)


async def _tg_gen(services: TelegramProxyServices, stream, ref: str, playback_handle):
    iterator = stream.__aiter__()
    heartbeat_task = None

    async def heartbeat():
        while True:
            await services.asyncio.sleep(15)
            await services.asyncio.to_thread(
                telegram_playback_activity.refresh_playback, playback_handle,
            )

    try:
        if playback_handle:
            heartbeat_task = services.asyncio.create_task(heartbeat())
        while True:
            try:
                chunk = await iterator.__anext__()
            except StopAsyncIteration:
                break
            yield chunk
    except services.asyncio.CancelledError:
        raise
    except Exception as e:
        _LOGGER.warning("Telegram stream failed for %s: %s", ref, e)
        raise
    finally:
        await _stop_heartbeat(services, heartbeat_task)
        await _finish_stream(services, iterator, playback_handle)


async def _open_download(services: TelegramProxyServices, prov, ref: str, offset: int, length: Optional[int]):
    try:
        stream = prov.download_range(ref, offset, length)
        if services.inspect.isawaitable(stream):
            stream = await stream
        return stream
    except HTTPException:
        raise
    except Exception as e:
        _LOGGER.warning("Telegram range download failed for %s: %s", ref, e)
        raise HTTPException(status_code=502, detail=f"Telegram download failed: {e}")


async def proxy_telegram_response(
    request: Request, url: str, cache_key: Optional[str] = None,
    *, services: TelegramProxyServices,
):
    """Stream bytes for a ``tg://<ref>`` URL via the telegram provider.

    Contract (implemented by the telegram provider):
    ``async def download_range(self, ref, offset, length)`` -> async iterator
    of bytes, plus optional ``async def file_size(self, ref) -> Optional[int]``.
    No cache and no ``upstream_url_alive`` are used on this path.
    """
    ref = url[len("tg://"):]
    if not ref:
        raise HTTPException(status_code=400, detail="Invalid telegram reference")
    prov = _resolve_provider(services, ref)
    rng_raw = request.headers.get("range")
    # File-size probe is only required when the client requests a range; normal playback streams safely without it.
    total = await _probe_total_size(services, prov, ref, required=bool(rng_raw))
    rng = services.parse_range_request(rng_raw, total)
    offset = rng[0] if rng else 0
    length = rng[1] if rng else None

    is_head = (request.method or "GET").upper() == "HEAD"
    partial_headers = None
    if rng is not None:
        partial_headers = _range_headers(offset, length, total)
        if is_head:
            # Same truthful headers/status as GET without creating or
            # consuming the download iterator.
            return Response(content=b"", status_code=206, headers=partial_headers,
                            media_type="application/octet-stream")
    elif is_head:
        return _head_response(total)
    stream = await _open_download(services, prov, ref, offset, length)

    playback_handle = None
    if not is_head and not _is_worker_request(request):
        playback_handle = await _start_playback(services, ref, total)

    body = _tg_gen(services, stream, ref, playback_handle)
    if partial_headers is not None:
        return StreamingResponse(body, status_code=206, headers=partial_headers, media_type="application/octet-stream")
    tg_headers = {"Accept-Ranges": "bytes", "Content-Type": "application/octet-stream"}
    if total is not None:
        # A full response needs the same known byte length as a range response.
        # Without it, clients such as FFmpeg can treat the .strm input as an
        # open-ended stream and report an ever-growing playback duration.
        tg_headers["Content-Length"] = str(total)
    return StreamingResponse(body, headers=tg_headers, media_type="application/octet-stream")
