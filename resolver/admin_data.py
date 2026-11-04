"""Read-only data access and domain formatting for the admin panel.

Functions take explicit paths and dependencies so HTTP routing stays separate
from data handling and can be tested without a web server. Telegram sources are
identified by their stable channel ID; the studio is only a display label.
"""

from __future__ import annotations

import json
import os
import stat
import time
from datetime import datetime
from pathlib import Path

from common import db
from common.config_loader import ConfigError, load_config
import admin_data_runtime_files as runtime_files


# Reports analyzed by the seven-day summary. `PlaybackDiagnostics.list_reports`
# caps results, so the summary must disclose when more rows exist.


def trim_channel_name(name: str, max_len: int = 25) -> str:
    """Trim channel names that are too long to prevent breaking table layouts."""
    name = (name or "").strip()
    return (name[: max_len - 1].rstrip() + "…") if len(name) > max_len else name


def channel_labels(config_path: Path | str) -> dict[str, str]:
    """Map stable Telegram channel IDs to configured studio labels."""
    mapping: dict[str, str] = {}
    try:
        config = load_config(str(config_path))
    except (ConfigError, OSError, ValueError):
        return mapping
    source = config.get_source("telegram")
    if source is None:
        return mapping
    for channel in source.channels:
        channel_id = str(channel.id or "").strip()
        if not channel_id:
            continue
        label = (channel.studio or channel_id).strip()
        mapping[channel_id] = label
        # Video IDs may store the bare numeric channel form; keep both.
        if channel_id.startswith("-100"):
            mapping.setdefault(channel_id[4:], label)
    return mapping


def configured_sources(config_path: Path | str) -> dict:
    """List every configured source and channel, including disabled ones."""
    try:
        config = load_config(str(config_path))
    except (ConfigError, OSError, ValueError):
        return {"sources": [], "available": False}
    sources = []
    for source in config.sources.values():
        channels = []
        for channel in source.channels:
            item = {
                "id": channel.id,
                "studio": (channel.studio or channel.id),
                "enabled": bool(channel.enabled),
            }
            # These are effective values: config_loader has already applied
            # source defaults to fields omitted from an individual channel.
            if source.provider == "telegram":
                item.update({
                    "stream_mode": channel.stream_mode,
                    "max_pages": channel.max_pages,
                    "min_duration_seconds": channel.min_duration_seconds,
                    "history_batch_size": channel.history_batch_size,
                    "tags_whitelist": list(channel.tags_whitelist),
                    "tags_blacklist": list(channel.tags_blacklist),
                })
            channels.append(item)

        # Expose only the validated, non-secret Telegram defaults, separately
        # from channel values so the UI can label them as source defaults.
        defaults = {}
        if source.provider == "telegram":
            defaults = {
                "stream_mode": source.defaults["stream_mode"],
                "max_pages": source.defaults["max_pages"],
                "min_duration_seconds": source.defaults["min_duration_seconds"],
                "history_batch_size": source.defaults["history_batch_size"],
                "tags_whitelist": list(source.defaults["tags_whitelist"]),
                "tags_blacklist": list(source.defaults["tags_blacklist"]),
            }
        configuration = {}
        if source.provider != "telegram":
            # Preserve useful provider-level settings without returning the
            # configured address, which may contain private host/user data.
            configuration = {
                "address_configured": bool(source.address),
                "stream_mode": source.stream_mode,
                "sections": list(source.sections),
                "max_pages": source.max_pages,
            }
        sources.append({"provider": source.provider, "enabled": bool(source.enabled),
                        "channels": channels, "defaults": defaults,
                        "configuration": configuration})
    return {"sources": sources, "available": True,
            "managed_revision": int(getattr(config, "managed_revision", 0))}


def _local_artwork_count(conn, media_dir: Path | str) -> int | None:
    """Count catalog videos with a regular, non-empty local artwork sidecar."""
    try:
        root = Path(media_dir).resolve()
        if not root.is_dir():
            return None
        with os.scandir(root) as entries:
            artwork_stems = set()
            for entry in entries:
                if not entry.name.lower().endswith(".jpg"):
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISREG(info.st_mode) and info.st_size > 0:
                    artwork_stems.add(entry.name[:-4])
        rows = conn.execute(
            "SELECT sf.video_id, sf.file_path FROM synced_files sf "
            "JOIN videos v ON v.id=sf.video_id"
        ).fetchall()
    except (OSError, TypeError, ValueError, db.DatabaseError):
        return None

    covered = set()
    for row in rows:
        video_id, file_path = row[0], row[1]
        if (not isinstance(video_id, str) or not video_id or not isinstance(file_path, str)
                or not file_path or "/" in file_path or "\\" in file_path
                or not file_path.lower().endswith(".strm")):
            continue
        if file_path[:-5] in artwork_stems:
            covered.add(video_id)
    return len(covered)


def db_stats(db_url: str | None, config_path: Path | str,
             media_dir: Path | str | None = None) -> dict:
    out: dict = {
        "db_ok": False,
        "db_path": "(postgresql)",
        # The classic panel showed this backend label alongside DB size. Keep
        # it explicit for newer clients without exposing DATABASE_URL.
        "database_backend": "PostgreSQL",
        "db_size_mb": None,
        "videos_total": None,
        "artwork_total": None,
        "oshash_total": None,
        "phash_total": None,
        "files_total": None,
        "by_provider": {},
        "telegram_total": None,
        "telegram_channels": {},
        "last_seen": None,
        "last_seen_human": "—",
        "fresh_24h": None,
        "photos_total": None,
        "bios_total": None,
        "aliases_total": None,
        "dead_streams_total": None,
        "unplayable_total": None,
    }
    try:
        try:
            conn = db.connect(db_url)
        except Exception as exc:
            out["error"] = f"brak połączenia z PostgreSQL: {exc}"[:200]
            return out
        try:
            _collect_db_stats(conn, out, config_path, media_dir)
            out["db_ok"] = True
        finally:
            conn.close()
    except Exception as e:
        out["error"] = str(e)[:200]
    return out


def _collect_db_stats(conn, out: dict, config_path, media_dir) -> None:
    """Fill ``out`` in place. Errors in the core counts propagate to the caller."""
    try:
        out["db_size_mb"] = conn.execute(
            "SELECT round(pg_database_size(current_database()) / 1024.0 / 1024.0, 2)"
        ).fetchone()[0]
    except Exception:
        pass
    out["videos_total"] = conn.execute("SELECT count(*) FROM videos").fetchone()[0]
    if media_dir is not None:
        out["artwork_total"] = _local_artwork_count(conn, media_dir)
    try:
        out["files_total"] = conn.execute("SELECT count(*) FROM synced_files").fetchone()[0]
    except Exception:
        pass
    try:
        _collect_provider_breakdown(conn, out, config_path)
    except Exception:
        pass
    try:
        mx = conn.execute("SELECT MAX(last_seen) FROM videos").fetchone()[0]
        if mx:
            out["last_seen"] = int(mx)
            out["last_seen_human"] = datetime.fromtimestamp(int(mx)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        pass
    try:
        day_ago = int(time.time()) - 86400
        out["fresh_24h"] = conn.execute(
            "SELECT count(*) FROM videos WHERE last_seen > %s", (day_ago,)
        ).fetchone()[0]
    except Exception:
        pass
    _collect_table_totals(conn, out)


def _collect_provider_breakdown(conn, out: dict, config_path) -> None:
    tg_ch_map = channel_labels(config_path)
    # Telegram IDs look like ``telegram:<channel>:...``. Use this
    # stable source identity; videos.studio is production metadata
    # and can be changed by enrichment.
    query = """
        SELECT
            provider,
            CASE
                WHEN provider = 'telegram' THEN
                    NULLIF(split_part(id, ':', 2), '')
                ELSE ''
            END AS ch,
            count(*)
        FROM videos
        GROUP BY provider, ch
        ORDER BY count(*) DESC
    """
    rows = conn.execute(query).fetchall()
    by_provider, tg_channels, tg_total = _provider_totals(rows, tg_ch_map)
    out["by_provider"] = dict(sorted(by_provider.items(), key=lambda item: item[1], reverse=True))
    if tg_total > 0:
        out["telegram_total"] = tg_total
        out["telegram_channels"] = tg_channels


def _provider_totals(rows, tg_ch_map: dict) -> tuple[dict, dict, int]:
    """Group video counts by provider and, for Telegram, by channel label."""
    by_provider: dict = {}
    tg_channels: dict = {}
    tg_total = 0
    for prov, ch, cnt in rows:
        prov_key = prov or "?"
        if prov_key == "telegram":
            channel_id = str(ch or "unknown")
            ch_name = tg_ch_map.get(channel_id, channel_id)
            tg_channels[ch_name] = tg_channels.get(ch_name, 0) + cnt
            tg_total += cnt
            key = f"tg:[{trim_channel_name(ch_name)}]"
            by_provider[key] = by_provider.get(key, 0) + cnt
        else:
            by_provider[prov_key] = by_provider.get(prov_key, 0) + cnt
    return by_provider, tg_channels, tg_total


def _collect_table_totals(conn, out: dict) -> None:
    tables = db.table_names(conn)
    if "video_fingerprints" in tables:
        coverage = conn.execute(
            "SELECT count(*) FILTER (WHERE algorithm = 'OSHASH'), "
            "count(*) FILTER (WHERE algorithm = 'PHASH') FROM video_fingerprints"
        ).fetchone()
        out["oshash_total"], out["phash_total"] = coverage
    if "performer_photos" in tables:
        out["photos_total"] = conn.execute(
            "SELECT count(*) FROM performer_photos "
            "WHERE btrim(COALESCE(thumb, ''), chr(32) || chr(9) || chr(10) || chr(13)) <> ''"
        ).fetchone()[0]
    if "performer_bios" in tables:
        out["bios_total"] = conn.execute(
            "SELECT count(*) FROM performer_bios "
            "WHERE btrim(COALESCE(bio, ''), chr(32) || chr(9) || chr(10) || chr(13)) <> ''"
        ).fetchone()[0]
    if "performer_aliases" in tables:
        out["aliases_total"] = conn.execute(
            "SELECT count(*) FROM performer_aliases "
            "WHERE btrim(COALESCE(alias, ''), chr(32) || chr(9) || chr(10) || chr(13)) <> '' "
            "AND btrim(COALESCE(canonical, ''), chr(32) || chr(9) || chr(10) || chr(13)) <> ''"
        ).fetchone()[0]
    if "dead_streams" in tables:
        out["dead_streams_total"] = conn.execute("SELECT count(*) FROM dead_streams").fetchone()[0]
    if "videos" in tables:
        cols = db.table_columns(conn, "videos")
        if "playable" in cols:
            out["unplayable_total"] = conn.execute("SELECT count(*) FROM videos WHERE playable = 0").fetchone()[0]


def sync_status(status_file: Path | str) -> dict:
    return runtime_files.sync_status(status_file)


def sync_history(history_file: Path | str, config_path: Path | str, limit: int = 30, offset: int = 0) -> dict:
    return runtime_files.sync_history(
        history_file,
        config_path,
        limit, offset,
        channel_labels=channel_labels,
        trim_channel_name=trim_channel_name,
    )


def backup_status(backup_dir: Path | str) -> dict:
    return runtime_files.backup_status(backup_dir)




def unavailable_videos(db_url: str | None, limit: int = 100, offset: int = 0) -> dict:
    """Page over known-unavailable library entries without writing anything.

    An entry is unavailable when ``videos.playable = 0`` or it is recorded in
    ``dead_streams``. Missing tables or columns degrade to an empty page.
    """
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    empty = {"videos": [], "total": 0, "truncated": False, "offset": offset, "available": True}
    try:
        conn = db.connect(db_url)
    except Exception:
        return {**empty, "available": False}
    conn.row_factory = db.Record
    try:
        tables = db.table_names(conn)
        if "videos" not in tables:
            return empty
        cols = set(db.table_columns(conn, "videos"))
        has_playable = "playable" in cols
        has_dead = "dead_streams" in tables
        if not has_playable and not has_dead:
            return empty
        conditions = []
        if has_playable:
            conditions.append("COALESCE(v.playable, 1) = 0")
        if has_dead:
            conditions.append("d.video_id IS NOT NULL")
        where = " OR ".join(conditions)
        select_title = "v.title" if "title" in cols else "''"
        select_provider = "v.provider" if "provider" in cols else "''"
        select_studio = "v.studio" if "studio" in cols else "''"
        select_playable = "v.playable" if has_playable else "NULL"
        join = "LEFT JOIN dead_streams d ON d.video_id = v.id" if has_dead else ""
        select_reason = "d.reason" if has_dead else "NULL"
        select_detected = "d.detected_at" if has_dead else "NULL"
        total = conn.execute(
            f"SELECT count(*) FROM videos v {join} WHERE {where}").fetchone()[0]
        rows = conn.execute(
            f"SELECT v.id, {select_title} AS title, {select_provider} AS provider, "
            f"{select_studio} AS studio, {select_playable} AS playable, "
            f"{select_reason} AS reason, {select_detected} AS detected_at "
            f"FROM videos v {join} WHERE {where} ORDER BY v.id LIMIT %s OFFSET %s",
            (limit, offset)).fetchall()
        return {
            "videos": [{
                "id": row["id"], "title": row["title"] or row["id"],
                "provider": row["provider"] or "", "studio": row["studio"] or "",
                "playable": row["playable"], "reason": row["reason"],
                "detected_at": row["detected_at"],
            } for row in rows],
            "total": total, "truncated": offset + len(rows) < total,
            "offset": offset, "available": True,
        }
    except db.OperationalError:
        return empty
    finally:
        conn.close()


def quarantine_report(db_url: str | None, limit: int = 200, offset: int = 0) -> dict:
    """Read-only audit of active quarantine/hide decisions without file paths."""
    limit = max(1, min(int(limit), 1000))
    offset = max(0, int(offset))
    empty = {"items": [], "total": 0, "truncated": False, "offset": offset, "available": True}
    try:
        conn = db.connect(db_url)
    except Exception:
        return {**empty, "available": False}
    conn.row_factory = db.Record
    try:
        tables = db.table_names(conn)
        if "video_duplicate_decisions" not in tables:
            return empty
        total = conn.execute(
            "SELECT count(*) FROM video_duplicate_decisions WHERE state='active'").fetchone()[0]
        rows = conn.execute(
            "SELECT loser_id, keeper_id, operation_id, created_at, snapshot "
            "FROM video_duplicate_decisions WHERE state='active' "
            "ORDER BY created_at DESC, loser_id LIMIT %s OFFSET %s",
            (limit, offset)).fetchall()
        items = []
        for row in rows:
            snapshot = {}
            try:
                parsed = json.loads(row["snapshot"]) if row["snapshot"] else {}
                if isinstance(parsed, dict):
                    snapshot = parsed
            except (TypeError, ValueError):
                snapshot = {}
            video = snapshot.get("video") if isinstance(snapshot.get("video"), dict) else {}
            files = snapshot.get("files")
            items.append({
                "id": row["loser_id"],
                "title": video.get("title") or row["loser_id"],
                "provider": video.get("provider") or "",
                "operation_id": row["operation_id"],
                "created_at": row["created_at"],
                "mode": "quarantine" if row["keeper_id"] == row["loser_id"] else "hide",
                "files": len(files) if isinstance(files, list) else None,
            })
        return {"items": items, "total": total, "truncated": offset + len(items) < total,
                "offset": offset, "available": True}
    except db.OperationalError:
        return empty
    finally:
        conn.close()
