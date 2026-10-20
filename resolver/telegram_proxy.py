"""HTTP range proxy for Telegram-backed media streams."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from fastapi import HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from common import telegram_playback_activity

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class TelegramProxyServices:
    diagnostics: Any
    get_provider: Any
    parse_range_request: Any
    asyncio: Any
    inspect: Any
    time: Any


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
    trace = services.diagnostics.begin(
        cache_key or ref,
        request.method,
        request.headers.get("range"),
    )
    if not ref:
        if trace:
            trace.set_status(400)
            await services.asyncio.to_thread(trace.finish, "error", "InvalidTelegramReference")
        raise HTTPException(status_code=400, detail="Invalid telegram reference")
    try:
        prov = services.get_provider("telegram")
    except KeyError:
        if trace:
            trace.set_status(502)
            await services.asyncio.to_thread(trace.finish, "error", "TelegramProviderUnavailable")
        raise HTTPException(status_code=502, detail="Telegram provider not available")
    authorize = getattr(prov, "is_channel_authorized", None)
    if callable(authorize):
        channel_ref = ref.rsplit(":", 2)[0]
        if not authorize(channel_ref):
            if trace:
                trace.set_status(403)
                await services.asyncio.to_thread(trace.finish, "error", "TelegramChannelUnauthorized")
            raise HTTPException(status_code=403, detail="Telegram channel is not authorized")
    dl = getattr(prov, "download_range", None)
    if not callable(dl):
        if trace:
            trace.set_status(502)
            await services.asyncio.to_thread(trace.finish, "error", "DownloadRangeUnavailable")
        raise HTTPException(status_code=502, detail="Telegram provider has no download_range")
    rng_raw = request.headers.get("range")
    total: Optional[int] = None
    fs_error: Optional[Exception] = None
    fs = getattr(prov, "file_size", None)
    if callable(fs):
        try:
            res = fs(ref)
            if services.inspect.isawaitable(res):
                res = await res
            if res is not None:
                total = int(res)
        except Exception as exc:
            fs_error = exc
            total = None
        finally:
            if trace:
                trace.mark_file_size()
    if fs_error is not None:
        # A silent downgrade would turn every client seek into a whole-file
        # transfer without telling anyone; fail loudly instead.
        _LOGGER.warning(
            "Telegram file_size lookup failed for %s: %s", ref, fs_error)
        if trace:
            trace.set_status(503)
            await services.asyncio.to_thread(trace.finish, "error", "TelegramFileSizeLookupFailed")
        raise HTTPException(status_code=503, detail="Telegram file size lookup failed") from fs_error
    rng = services.parse_range_request(rng_raw, total)
    offset = rng[0] if rng else 0
    length = rng[1] if rng else None
    # If a client asks for a range but the provider cannot tell us the total
    # size, a partial response cannot be described truthfully. Stream the
    # whole resource from byte zero and return 200 in that case.
    if rng is not None and total is None:
        offset, length = 0, None
    is_head = (request.method or "GET").upper() == "HEAD"
    if rng is not None and total is not None:
        end = (offset + length - 1) if length is not None else (total - 1)
        if end >= total:
            end = total - 1
        if end < offset:
            if trace:
                trace.set_status(416)
                await services.asyncio.to_thread(trace.finish, "error", "RangeNotSatisfiable")
            raise HTTPException(status_code=416, detail="Range not satisfiable",
                                headers={"Content-Range": f"bytes */{total}"})
        tg_headers = {
            "Content-Range": f"bytes {offset}-{end}/{total}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - offset + 1),
            "Content-Type": "application/octet-stream",
        }
        if is_head:
            # Same truthful headers/status as GET without creating or
            # consuming the download iterator.
            if trace:
                trace.set_status(206)
            return Response(content=b"", status_code=206, headers=tg_headers,
                            media_type="application/octet-stream")
    elif is_head:
        if trace:
            trace.set_status(200)
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
    try:
        stream = dl(ref, offset, length)
        if services.inspect.isawaitable(stream):
            stream = await stream
    except HTTPException:
        if trace:
            trace.set_status(502)
            await services.asyncio.to_thread(trace.finish, "error", "HTTPException")
        raise
    except Exception as e:
        if trace:
            trace.set_status(502)
            await services.asyncio.to_thread(trace.finish, "error", type(e).__name__)
        raise HTTPException(status_code=502, detail=f"Telegram download failed: {e}")

    # pHash reads use this same bounded range endpoint. Exclude only the
    # resolver's loopback worker requests; every client playback remains
    # visible to the cross-container rate controller.
    client_host = getattr(getattr(request, "client", None), "host", "")
    worker_request = (
        request.headers.get("x-adult-hub-transfer", "").lower() == "phash"
        and client_host in {"127.0.0.1", "::1"}
    )
    playback_handle = None
    if not is_head and not worker_request:
        try:
            playback_handle = await services.asyncio.to_thread(
                telegram_playback_activity.begin_playback,
            )
        except Exception:
            # Playback must continue even if the optional activity ledger fails.
            playback_handle = None

    async def _tg_gen():
        outcome = "completed"
        error_type = None
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
                if trace:
                    trace.set_phase("telegram_read")
                fetch_started = services.time.monotonic()
                try:
                    chunk = await iterator.__anext__()
                except StopAsyncIteration:
                    break
                if trace:
                    # Measure the Telegram iterator wait separately from time
                    # spent sending the previous chunk to the HTTP client.
                    fetch_wait_ms = int((services.time.monotonic() - fetch_started) * 1000)
                    trace.note_upstream(len(chunk), fetch_wait_ms)
                    trace.set_phase("http_send")
                send_started = services.time.monotonic()
                yield chunk
                if trace:
                    # Time until the ASGI server asks for the next chunk is a
                    # proxy-side send wait, not proof of Chromecast receipt.
                    send_wait_ms = int((services.time.monotonic() - send_started) * 1000)
                    trace.note_downstream(len(chunk), send_wait_ms)
        except services.asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception as e:
            outcome = "error"
            error_type = type(e).__name__
            raise
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except services.asyncio.CancelledError:
                    pass
            if playback_handle:
                try:
                    await services.asyncio.to_thread(
                        telegram_playback_activity.end_playback, playback_handle,
                    )
                except Exception:
                    pass
            if trace:
                await services.asyncio.to_thread(trace.finish, outcome, error_type)
            close_iterator = getattr(iterator, "aclose", None)
            if callable(close_iterator):
                try:
                    await close_iterator()
                except Exception:
                    pass

    if rng is not None and total is not None:
        end = (offset + length - 1) if length is not None else (total - 1)
        if end >= total:
            end = total - 1
        if end < offset:
            if trace:
                trace.set_status(416)
                await services.asyncio.to_thread(trace.finish, "error", "RangeNotSatisfiable")
            raise HTTPException(status_code=416, detail="Range not satisfiable",
                                headers={"Content-Range": f"bytes */{total}"})
        tg_headers = {
            "Content-Range": f"bytes {offset}-{end}/{total}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - offset + 1),
            "Content-Type": "application/octet-stream",
        }
        if trace:
            trace.set_status(206)
        return StreamingResponse(_tg_gen(), status_code=206, headers=tg_headers, media_type="application/octet-stream")
    if trace:
        trace.set_status(200)
    tg_headers = {"Accept-Ranges": "bytes", "Content-Type": "application/octet-stream"}
    if total is not None:
        # A full response needs the same known byte length as a range response.
        # Without it, clients such as FFmpeg can treat the .strm input as an
        # open-ended stream and report an ever-growing playback duration.
        tg_headers["Content-Length"] = str(total)
    return StreamingResponse(_tg_gen(), headers=tg_headers, media_type="application/octet-stream")
