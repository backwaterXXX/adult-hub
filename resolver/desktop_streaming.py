"""Playback descriptor and stream-candidate helpers for the desktop API."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from resolver.desktop_api import PlaybackDescriptorDTO

from fastapi import HTTPException

from common import db
from common.client_auth.service import BoundedValidationError

logger = logging.getLogger("adult_hub.desktop_streaming")


def _parse_play_id(video_id: str) -> tuple[str, str]:
    """Parse provider composite or plain video ID, defaulting to Telegram."""
    if not video_id:
        return "telegram", ""
    text = video_id.strip()
    if ":" in text:
        from resolver.providers import list_providers

        prov, raw_id = text.split(":", 1)
        if prov in list_providers():
            return prov, raw_id
        raise BoundedValidationError(f"Unknown provider prefix '{prov}' in id {video_id!r}")
    return "telegram", text


def _pick_canonical_stream_id(conn: db.Connection, requested_id: str, tables: Any) -> str:
    """Resolve a requested video ID to its canonical ID through source aliases and duplicate decisions."""
    canonical_id = requested_id
    if "video_sources" in tables:
        alias_row = conn.execute(
            "SELECT video_id FROM video_sources WHERE source_id=%s AND video_id!=%s AND is_active=1",
            (requested_id, requested_id),
        ).fetchone()
        if alias_row and alias_row[0]:
            canonical_id = alias_row[0]
    if canonical_id == requested_id and "video_duplicate_decisions" in tables:
        dec_row = conn.execute(
            "SELECT keeper_id FROM video_duplicate_decisions WHERE loser_id=%s AND state='active'",
            (requested_id,),
        ).fetchone()
        if dec_row and dec_row[0]:
            canonical_id = dec_row[0]
    return canonical_id


def _collect_stream_candidate_ids(conn: db.Connection, requested_id: str, canonical_id: str, tables: Any) -> list[str]:
    """Build the ordered candidate list: the canonical ID, its active sources, then the requested ID."""
    candidates = [canonical_id]
    if "video_sources" in tables:
        sources = conn.execute(
            "SELECT source_id FROM video_sources WHERE video_id=%s AND is_active=1 ORDER BY priority ASC, id ASC",
            (canonical_id,),
        ).fetchall()
        for (s,) in sources:
            if s and s not in candidates:
                candidates.append(s)
    if requested_id not in candidates:
        candidates.append(requested_id)
    return candidates


def _get_stream_candidate_ids(requested_id: str, conn: db.Connection | None = None) -> list[str]:
    """Look up canonical video ID and all active failover sources from the database."""
    close_conn = False
    if conn is None:
        from resolver.desktop_api import _canonical_production_db_url

        url = _canonical_production_db_url()
        if not url:
            return [requested_id]
        conn = db.connect(url)
        close_conn = True
    try:
        tables = db.table_names(conn)
        canonical_id = _pick_canonical_stream_id(conn, requested_id, tables)
        return _collect_stream_candidate_ids(conn, requested_id, canonical_id, tables)
    except Exception as exc:
        logger.debug("Source candidate lookup error for %s: %s", requested_id, exc)
        return [requested_id]
    finally:
        if close_conn:
            conn.close()


def _is_video_visible_and_playable(conn: db.Connection, canonical_id: str) -> bool:
    """Verify that video exists, is playable, not dead, and not quarantined."""
    tables = db.table_names(conn)
    if "videos" not in tables:
        return True
    row = conn.execute(
        "SELECT playable FROM videos WHERE id = %s",
        (canonical_id,),
    ).fetchone()
    if row is None:
        return False
    if row[0] is not None and row[0] == 0:
        return False
    if "dead_streams" in tables:
        dead = conn.execute(
            "SELECT 1 FROM dead_streams WHERE video_id = %s",
            (canonical_id,),
        ).fetchone()
        if dead:
            return False
    if "video_duplicate_decisions" in tables:
        quarantined = conn.execute(
            "SELECT 1 FROM video_duplicate_decisions WHERE loser_id = %s AND keeper_id = %s AND state = 'active'",
            (canonical_id, canonical_id),
        ).fetchone()
        if quarantined:
            return False
    return True


async def _fetch_telegram_variants(cand_id: str, raw_id: str):
    """Query Telegram provider for pre-existing variants. Returns (variants_data, error)."""
    channel_ref = (raw_id or "").split(":", 1)[0]
    try:
        from resolver.providers import get_provider

        tg_prov = get_provider("telegram")
    except KeyError:
        return None, HTTPException(status_code=502, detail="Telegram provider not available")

    auth_check = getattr(tg_prov, "is_channel_authorized", None)
    if callable(auth_check) and not auth_check(channel_ref):
        return None, HTTPException(status_code=403, detail="Telegram channel is not authorized")

    variants_fn = getattr(tg_prov, "get_video_variants", None)
    if not callable(variants_fn):
        return None, None

    try:
        res = await variants_fn(raw_id)
        if isinstance(res, dict):
            res["video_id"] = cand_id
        return res, None
    except HTTPException as exc:
        return None, exc
    except ValueError:
        return None, HTTPException(status_code=404, detail="Video variants are unavailable")
    except Exception as exc:
        logger.warning("Failed to fetch variants for %s: %s", cand_id, exc)
        return None, exc


def _build_playback_descriptor(
    canonical_id: str,
    variants_data: dict[str, Any] | None,
    media_metadata: dict[str, Any] | None = None,
) -> PlaybackDescriptorDTO:
    """Build a safe client-facing playback descriptor strictly omitting Telegram URLs and credentials."""
    from resolver.desktop_api import PlaybackDescriptorDTO, PlaybackRenditionDTO

    meta = media_metadata or {}
    raw_variants = (variants_data.get("variants", []) if variants_data else []) or []

    rendition_dtos: list[PlaybackRenditionDTO] = []
    seen_ids: set[str] = set()

    for var in raw_variants:
        var_id = str(var.get("id") or "").strip()
        if not var_id or var_id in seen_ids:
            continue
        seen_ids.add(var_id)

        is_orig = bool(var.get("is_original", False))
        h = var.get("height")
        w = var.get("width")
        mime = var.get("mime_type") or "video/mp4"
        size = var.get("file_size")

        if is_orig:
            label = f"Original Direct Play ({h}p)" if h else "Original Direct Play"
            stream_path = f"/api/v1/playback/stream/{canonical_id}"
        else:
            label = f"Telegram Alternate Stream ({h}p)" if h else "Telegram Alternate Stream"
            stream_path = f"/api/v1/playback/stream/{canonical_id}?rendition_id={var_id}"

        rendition_dtos.append(
            PlaybackRenditionDTO(
                id=var_id,
                label=label,
                mime_type=mime,
                file_size_bytes=size,
                width=w,
                height=h,
                is_default=is_orig,
                stream_path=stream_path,
            )
        )

    if not rendition_dtos:
        rendition_dtos.append(
            PlaybackRenditionDTO(
                id="original",
                label="Original Direct Play",
                mime_type=meta.get("mime_type") or "video/mp4",
                file_size_bytes=meta.get("file_size"),
                width=meta.get("width"),
                height=meta.get("height"),
                is_default=True,
                stream_path=f"/api/v1/playback/stream/{canonical_id}",
            )
        )

    # Sort renditions: default (original) first, then alternatives by height descending
    rendition_dtos.sort(key=lambda r: (not r.is_default, -(r.height or 0)))

    default_rendition = next((r for r in rendition_dtos if r.is_default), rendition_dtos[0])
    main_stream_path = default_rendition.stream_path

    return PlaybackDescriptorDTO(
        video_id=canonical_id,
        stream_path=main_stream_path,
        renditions=rendition_dtos,
        mime_type=default_rendition.mime_type or "video/mp4",
        duration_seconds=meta.get("duration"),
        file_size_bytes=default_rendition.file_size_bytes or meta.get("file_size"),
        width=default_rendition.width or meta.get("width"),
        height=default_rendition.height or meta.get("height"),
    )


