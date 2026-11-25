"""Catalog and metadata migration procedures for the sync service."""

import json
import logging
import os
import re
import shutil
from common import db
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

logger = logging.getLogger(__name__)

from sync.performer_data_migrations import REDUNDANT_PERFORMERS_VERSION  # noqa: E402


CATEGORY_MAPPING_VERSION = 3

_STABLE_STRM_RE = re.compile(r"^[A-Za-z0-9]+ \[[A-Za-z0-9_-]{1,40}-[0-9a-f]{12}\]\.strm$")


@dataclass(frozen=True)
class DataMigrationServices:
    """Dependencies supplied dynamically by the sync.py facade."""

    logger: Any
    video_is_disabled: Callable
    resolve_media_path: Callable
    stable_media_basename: Callable
    write_stream_file: Callable
    map_categories: Callable
    prune_redundant_performers: Callable


def _import_map_categories():
    from sync.category_mapping import map_categories as _map_categories
    return _map_categories


def _import_media_helpers():
    from sync.media_files import (
        resolve_media_path as _resolve_media_path,
        stable_media_basename as _stable_media_basename,
        write_stream_file as _write_stream_file,
    )
    return _resolve_media_path, _stable_media_basename, _write_stream_file


def _resolve_default_services(services: Optional[DataMigrationServices]) -> DataMigrationServices:
    if services is not None:
        return services
    _map_categories = _import_map_categories()
    _resolve_media_path, _stable_media_basename, _write_stream_file = _import_media_helpers()

    from common.performers import prune_redundant_performers as _prune_redundant_performers

    return DataMigrationServices(
        logger=logging.getLogger("sync.migrations"),
        video_is_disabled=lambda _vid: False,
        resolve_media_path=_resolve_media_path,
        stable_media_basename=_stable_media_basename,
        write_stream_file=_write_stream_file,
        map_categories=_map_categories,
        prune_redundant_performers=_prune_redundant_performers,
    )


def _all_strm_names_stable(media_dir: Path, svc: DataMigrationServices) -> bool:
    """Return True when every .strm on disk already uses the stable naming scheme."""
    try:
        if media_dir.exists():
            names = [p.name for p in media_dir.glob("*.strm")]
            if names and all(_STABLE_STRM_RE.match(n) for n in names):
                svc.logger.debug("migrate_synced_files: all filenames stable, skipping.")
                return True
    except Exception:
        logger.debug("Ignored exception in migrate_synced_files", exc_info=True)
    return False


def _synced_video_is_migratable(conn: db.Connection, video_id, svc: DataMigrationServices) -> bool:
    if not video_id or svc.video_is_disabled(video_id):
        return False
    return bool(conn.execute("SELECT 1 FROM videos WHERE id = %s", (video_id,)).fetchone())


def _copy_sidecar_files(old_path: Path, new_path: Path) -> None:
    for ext in (".jpg",):
        source = old_path.with_suffix(ext)
        target = new_path.with_suffix(ext)
        if source.exists() and not target.exists():
            tmp_path = target.with_suffix(ext + ".tmp")
            try:
                shutil.copy2(source, tmp_path)
                os.replace(tmp_path, target)
            finally:
                tmp_path.unlink(missing_ok=True)


def _replace_synced_file_row(conn: db.Connection, old_name, new_path: Path, video_id, category, created_at) -> None:
    with conn:
        conn.execute("DELETE FROM synced_files WHERE file_path = %s", (old_name,))
        conn.execute("""INSERT INTO synced_files
            (file_path, video_id, category, created_at) VALUES (%s, %s, %s, %s)
            ON CONFLICT (file_path) DO UPDATE SET video_id=EXCLUDED.video_id,
                category=EXCLUDED.category, created_at=EXCLUDED.created_at""",
            (new_path.name, video_id, category, created_at))


def _remove_legacy_stream_files(old_path: Path) -> None:
    for ext in (".strm", ".jpg"):
        old_path.with_suffix(ext).unlink(missing_ok=True)


def migrate_synced_files(conn: db.Connection, media_dir: Path, *,
                         services: Optional[DataMigrationServices] = None) -> None:
    """Move legacy title-based names to stable names, including unseen videos."""
    svc = _resolve_default_services(services)
    # Fast path: if every .strm on disk already uses the stable naming scheme,
    # there is nothing to migrate — skip the per-row DB scan entirely.
    if _all_strm_names_stable(media_dir, svc):
        return
    rows = conn.execute("SELECT file_path, video_id, category, created_at FROM synced_files").fetchall()
    for old_name, video_id, category, created_at in rows:
        if not _synced_video_is_migratable(conn, video_id, svc):
            continue
        try:
            old_path = svc.resolve_media_path(old_name, media_dir)
        except ValueError as e:
            svc.logger.warning("Skipping unsafe media record: %s", e)
            continue
        new_path = media_dir / f"{svc.stable_media_basename(video_id)}.strm"
        if old_path == new_path:
            svc.write_stream_file(new_path, video_id)
            continue

        _copy_sidecar_files(old_path, new_path)
        svc.write_stream_file(new_path, video_id)
        _replace_synced_file_row(conn, old_name, new_path, video_id, category, created_at)
        if not conn.execute("SELECT 1 FROM synced_files WHERE file_path = %s", (old_name,)).fetchone():
            _remove_legacy_stream_files(old_path)


def _category_mapping_already_applied(conn: db.Connection, category_mapping_version: int,
                                      svc: DataMigrationServices) -> bool:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'category_mapping_version'").fetchone()
        if row and row[0] == str(category_mapping_version):
            svc.logger.debug("migrate_categories: mapping v%s already applied, skipping.", category_mapping_version)
            return True
    except Exception:
        logger.debug("Ignored exception in migrate_categories", exc_info=True)
    return False


def _parse_category_column(cats_val) -> list:
    raw_list = []
    if cats_val:
        if cats_val.startswith("["):
            raw_list = json.loads(cats_val)
        else:
            raw_list = [c.strip() for c in cats_val.split(",") if c.strip()]
    return raw_list


def _recompute_video_categories(cats_val, v_title, svc: DataMigrationServices) -> str:
    raw_list = _parse_category_column(cats_val)
    mapped = svc.map_categories(raw_list, title=v_title)
    return json.dumps(sorted(mapped))


def _migrate_video_category_rows(conn: db.Connection, cur, svc: DataMigrationServices) -> None:
    updated_videos = 0
    for vid_id, cats_val, v_title in cur.fetchall():
        try:
            new_val = _recompute_video_categories(cats_val, v_title, svc)
            if new_val != cats_val:
                conn.execute("UPDATE videos SET categories = %s WHERE id = %s", (new_val, vid_id))
                updated_videos += 1
        except Exception:
            logger.debug("Ignored exception in migrate_categories", exc_info=True)

    if updated_videos > 0:
        svc.logger.info("Updated categories for %s videos in database.", updated_videos)


def _rebuild_video_categories_junction(conn: db.Connection) -> None:
    conn.execute("DELETE FROM video_categories")
    cur2 = conn.execute("SELECT id, categories FROM videos WHERE categories <> '[]'::jsonb")
    for vid_id, cats_val in cur2.fetchall():
        try:
            for cat in json.loads(cats_val):
                if cat and not cat.startswith("Browse"):
                    conn.execute("INSERT INTO video_categories (video_id, category) VALUES (%s, %s) ON CONFLICT DO NOTHING", (vid_id, cat))
        except Exception:
            logger.debug("Ignored exception in migrate_categories", exc_info=True)


def _sync_synced_files_category_column(conn: db.Connection) -> None:
    cur3 = conn.execute("SELECT file_path, video_id FROM synced_files")
    for f_path, vid_id in cur3.fetchall():
        row = conn.execute("SELECT categories FROM videos WHERE id = %s", (vid_id,)).fetchone()
        if row and row[0]:
            try:
                conn.execute("UPDATE synced_files SET category = %s WHERE file_path = %s", (", ".join(json.loads(row[0])), f_path))
            except Exception:
                logger.debug("Ignored exception in migrate_categories", exc_info=True)


def _record_category_mapping_version(conn: db.Connection, category_mapping_version: int) -> None:
    with conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('category_mapping_version', %s) "
            "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
            (str(category_mapping_version),))


def migrate_categories(conn: db.Connection, media_dir: Path, *,
                       services: Optional[DataMigrationServices] = None,
                       category_mapping_version: int = CATEGORY_MAPPING_VERSION) -> None:
    """Normalize existing DB categories and the junction table to the unified genres."""
    svc = _resolve_default_services(services)
    if _category_mapping_already_applied(conn, category_mapping_version, svc):
        return
    svc.logger.info("Checking database for category migration...")

    # 1-3. Update videos, rebuild the junction table, and sync synced_files, in one transaction
    cur = conn.execute("SELECT id, categories, title FROM videos")
    with conn:
        _migrate_video_category_rows(conn, cur, svc)
        _rebuild_video_categories_junction(conn)
        _sync_synced_files_category_column(conn)

    _record_category_mapping_version(conn, category_mapping_version)


def break_alias_cycles_and_flatten(conn: db.Connection) -> Tuple[int, int]:
    """Compatibility facade for performer alias graph cleanup."""
    from sync.performer_data_migrations import break_alias_cycles_and_flatten as implementation
    return implementation(conn)


def migrate_redundant_performers(
    conn: db.Connection, media_dir: Path, *,
    services: Optional[DataMigrationServices] = None,
    redundant_performers_version: int = REDUNDANT_PERFORMERS_VERSION,
) -> int:
    """Compatibility facade for performer metadata cleanup."""
    from sync.performer_data_migrations import migrate_redundant_performers as implementation
    return implementation(
        conn, media_dir, services=services,
        redundant_performers_version=redundant_performers_version,
    )


def _load_json_field(raw, as_set: bool):
    """Decode a JSON column. Empty or invalid values give [] (or set() when as_set is True)."""
    empty = set() if as_set else []
    if not raw:
        return empty
    try:
        value = json.loads(raw)
        return set(value) if as_set else value
    except Exception:
        return empty
