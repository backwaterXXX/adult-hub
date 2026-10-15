"""Catalog and metadata migration procedures for the sync service."""

import html
import json
import logging
import os
import re
import shutil
from common import db
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

if __package__:
    from .performer_data_migrations import REDUNDANT_PERFORMERS_VERSION
else:
    from performer_data_migrations import REDUNDANT_PERFORMERS_VERSION


CATEGORY_MAPPING_VERSION = 3
DISPLAY_TITLE_VERSION = 5

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
    load_performer_thumbs: Callable
    format_dateadded: Callable
    create_nfo_file: Callable


def _resolve_default_services(services: Optional[DataMigrationServices]) -> DataMigrationServices:
    if services is not None:
        return services
    try:
        from category_mapping import map_categories as _map_categories
    except ImportError:
        from sync.category_mapping import map_categories as _map_categories
    try:
        from media_files import (
            resolve_media_path as _resolve_media_path,
            stable_media_basename as _stable_media_basename,
            write_stream_file as _write_stream_file,
        )
    except ImportError:
        from sync.media_files import (
            resolve_media_path as _resolve_media_path,
            stable_media_basename as _stable_media_basename,
            write_stream_file as _write_stream_file,
        )
    try:
        from nfo import (
            load_performer_thumbs as _load_performer_thumbs,
            format_dateadded as _format_dateadded,
            create_nfo_file as _create_nfo_file,
        )
    except ImportError:
        from sync.nfo import (
            load_performer_thumbs as _load_performer_thumbs,
            format_dateadded as _format_dateadded,
            create_nfo_file as _create_nfo_file,
        )
    try:
        from provider_registry import get_provider_info as _get_provider_info
    except ImportError:
        from sync.provider_registry import get_provider_info as _get_provider_info
    try:
        from resolver.providers.base import clean_display_title as _clean_display_title
    except ImportError:
        try:
            from providers.base import clean_display_title as _clean_display_title
        except ImportError:
            try:
                from sync.sync import clean_display_title as _clean_display_title
            except ImportError:
                try:
                    from sync import clean_display_title as _clean_display_title
                except ImportError:
                    def _clean_display_title(title, **kwargs):
                        return title
    try:
        from performers import normalize_performers as _normalize_performers
    except ImportError:
        from common.performers import normalize_performers as _normalize_performers

    from common.performers import prune_redundant_performers as _prune_redundant_performers

    def _default_create_nfo(video, genres, tags, dest_path):
        return _create_nfo_file(
            video, genres, tags, dest_path,
            get_provider_info_fn=_get_provider_info,
            clean_display_title_fn=_clean_display_title,
            normalize_performers_fn=_normalize_performers,
            map_categories_fn=_map_categories,
        )

    return DataMigrationServices(
        logger=logging.getLogger("sync.migrations"),
        video_is_disabled=lambda _vid: False,
        resolve_media_path=_resolve_media_path,
        stable_media_basename=_stable_media_basename,
        write_stream_file=_write_stream_file,
        map_categories=_map_categories,
        prune_redundant_performers=_prune_redundant_performers,
        load_performer_thumbs=_load_performer_thumbs,
        format_dateadded=_format_dateadded,
        create_nfo_file=_default_create_nfo,
    )


def migrate_synced_files(conn: db.Connection, media_dir: Path, *,
                         services: Optional[DataMigrationServices] = None) -> None:
    """Move legacy title-based names to stable names, including unseen videos."""
    svc = _resolve_default_services(services)
    # Fast path: if every .strm on disk already uses the stable naming scheme,
    # there is nothing to migrate — skip the per-row DB scan entirely.
    try:
        if media_dir.exists():
            names = [p.name for p in media_dir.glob("*.strm")]
            if names and all(_STABLE_STRM_RE.match(n) for n in names):
                svc.logger.debug("migrate_synced_files: all filenames stable, skipping.")
                return
    except Exception:
        pass
    rows = conn.execute("SELECT file_path, video_id, category, created_at FROM synced_files").fetchall()
    for old_name, video_id, category, created_at in rows:
        if not video_id or svc.video_is_disabled(video_id) or not conn.execute("SELECT 1 FROM videos WHERE id = %s", (video_id,)).fetchone():
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

        for ext in (".nfo", ".jpg"):
            source = old_path.with_suffix(ext)
            target = new_path.with_suffix(ext)
            if source.exists() and not target.exists():
                tmp_path = target.with_suffix(ext + ".tmp")
                try:
                    shutil.copy2(source, tmp_path)
                    os.replace(tmp_path, target)
                finally:
                    tmp_path.unlink(missing_ok=True)
        svc.write_stream_file(new_path, video_id)
        with conn:
            conn.execute("DELETE FROM synced_files WHERE file_path = %s", (old_name,))
            conn.execute("""INSERT INTO synced_files
                (file_path, video_id, category, created_at) VALUES (%s, %s, %s, %s)
                ON CONFLICT (file_path) DO UPDATE SET video_id=EXCLUDED.video_id,
                    category=EXCLUDED.category, created_at=EXCLUDED.created_at""",
                (new_path.name, video_id, category, created_at))
        if not conn.execute("SELECT 1 FROM synced_files WHERE file_path = %s", (old_name,)).fetchone():
            for ext in (".strm", ".nfo", ".jpg"):
                old_path.with_suffix(ext).unlink(missing_ok=True)


def migrate_categories(conn: db.Connection, media_dir: Path, *,
                       services: Optional[DataMigrationServices] = None,
                       category_mapping_version: int = CATEGORY_MAPPING_VERSION) -> None:
    """Normalize existing DB categories, junction table, and NFO files to the unified genres."""
    svc = _resolve_default_services(services)
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'category_mapping_version'").fetchone()
        if row and row[0] == str(category_mapping_version):
            svc.logger.debug("migrate_categories: mapping v%s already applied, skipping.", category_mapping_version)
            return
    except Exception:
        pass
    svc.logger.info("Checking database and NFO files for category migration...")

    # 1. Update videos table
    cur = conn.execute("SELECT id, categories, title FROM videos")
    updated_videos = 0
    with conn:
        for vid_id, cats_val, v_title in cur.fetchall():
            try:
                raw_list = []
                if cats_val:
                    if cats_val.startswith("["):
                        raw_list = json.loads(cats_val)
                    else:
                        raw_list = [c.strip() for c in cats_val.split(",") if c.strip()]
                mapped = svc.map_categories(raw_list, title=v_title)
                new_val = json.dumps(sorted(list(mapped)))
                if new_val != cats_val:
                    conn.execute("UPDATE videos SET categories = %s WHERE id = %s", (new_val, vid_id))
                    updated_videos += 1
            except Exception:
                pass

        if updated_videos > 0:
            svc.logger.info(f"Updated categories for {updated_videos} videos in database.")

        # 2. Rebuild video_categories junction table
        conn.execute("DELETE FROM video_categories")
        cur2 = conn.execute("SELECT id, categories FROM videos WHERE categories <> '[]'::jsonb")
        for vid_id, cats_val in cur2.fetchall():
            try:
                for cat in json.loads(cats_val):
                    if cat and not cat.startswith("Browse"):
                        conn.execute("INSERT INTO video_categories (video_id, category) VALUES (%s, %s) ON CONFLICT DO NOTHING", (vid_id, cat))
            except Exception:
                pass

        # 3. Update synced_files category column
        cur3 = conn.execute("SELECT file_path, video_id FROM synced_files")
        for f_path, vid_id in cur3.fetchall():
            row = conn.execute("SELECT categories FROM videos WHERE id = %s", (vid_id,)).fetchone()
            if row and row[0]:
                try:
                    conn.execute("UPDATE synced_files SET category = %s WHERE file_path = %s", (", ".join(json.loads(row[0])), f_path))
                except Exception:
                    pass

    # 4. Migrate NFO files on disk
    if media_dir.exists():
        updated_nfos = 0
        genre_pattern = re.compile(r'(?:[ \t]*<genre>.*?</genre>\r?\n)+')
        insert_marker_pattern = re.compile(r'([ \t]*<(?:tag|actor|thumb|uniqueid))')

        for nfo_file in media_dir.glob("*.nfo"):
            try:
                text = nfo_file.read_text(encoding="utf-8")
                raw_genres = [html.unescape(g.strip()) for g in re.findall(r'<genre>(.*?)</genre>', text) if g.strip()]
                title_m = re.search(r'<title>(.*?)</title>', text)
                title_val = html.unescape(title_m.group(1).strip()) if title_m else ""
                mapped_genres = svc.map_categories(raw_genres, title=title_val)

                # Check if genres changed
                if sorted(raw_genres) == sorted(mapped_genres):
                    continue

                new_block = "\n".join(f"  <genre>{html.escape(g)}</genre>" for g in sorted(mapped_genres))
                if new_block:
                    new_block += "\n"

                if genre_pattern.search(text):
                    new_text = genre_pattern.sub(new_block, text, count=1)
                else:
                    m = insert_marker_pattern.search(text)
                    if m:
                        idx = m.start()
                        new_text = text[:idx] + new_block + text[idx:]
                    else:
                        new_text = text

                if new_text != text:
                    tmp = nfo_file.with_suffix(".nfo.tmp")
                    tmp.write_text(new_text, encoding="utf-8")
                    os.replace(tmp, nfo_file)
                    updated_nfos += 1
            except Exception as e:
                svc.logger.debug(f"Error migrating NFO {nfo_file.name}: {e}")

        if updated_nfos > 0:
            svc.logger.info(f"Migrated {updated_nfos} NFO files to unified genres.")

    with conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('category_mapping_version', %s) "
            "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
            (str(category_mapping_version),))


def break_alias_cycles_and_flatten(conn: db.Connection) -> Tuple[int, int]:
    """Compatibility facade for performer alias graph cleanup."""
    try:
        from .performer_data_migrations import break_alias_cycles_and_flatten as implementation
    except ImportError:
        from performer_data_migrations import break_alias_cycles_and_flatten as implementation
    return implementation(conn)


def migrate_redundant_performers(
    conn: db.Connection, media_dir: Path, *,
    services: Optional[DataMigrationServices] = None,
    redundant_performers_version: int = REDUNDANT_PERFORMERS_VERSION,
) -> int:
    """Compatibility facade for performer metadata and NFO cleanup."""
    try:
        from .performer_data_migrations import migrate_redundant_performers as implementation
    except ImportError:
        from performer_data_migrations import migrate_redundant_performers as implementation
    return implementation(
        conn, media_dir, services=services,
        redundant_performers_version=redundant_performers_version,
    )


def migrate_display_titles(conn: db.Connection, media_dir: Path, *,
                           services: Optional[DataMigrationServices] = None,
                           display_title_version: int = DISPLAY_TITLE_VERSION) -> int:
    """Update existing NFO files to have clean display titles in <title> and raw source in <originaltitle>."""
    svc = _resolve_default_services(services)
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'display_title_version'").fetchone()
        if row and row[0] == str(display_title_version):
            svc.logger.debug("migrate_display_titles: v%s already applied, skipping.", display_title_version)
            return 0
    except Exception:
        pass

    svc.logger.info("Checking NFO files for display title cleanup...")
    rows = conn.execute(
        """SELECT id, provider, title, slug, duration, thumbnail,
                  performers, categories, tags, studio, first_seen
           FROM videos"""
    ).fetchall()
    updated = 0
    for row in rows:
        vid_id, provider, title, slug, duration, thumb, perfs_raw, cats_raw, tags_raw, studio, first_seen = row
        nfo_path = media_dir / f"{svc.stable_media_basename(vid_id)}.nfo"
        if not nfo_path.exists():
            continue
        try:
            perfs = json.loads(perfs_raw) if perfs_raw else []
        except Exception:
            perfs = []
        try:
            genres = set(json.loads(cats_raw)) if cats_raw else set()
        except Exception:
            genres = set()
        try:
            tags = set(json.loads(tags_raw)) if tags_raw else set()
        except Exception:
            tags = set()

        pid = vid_id.split(":", 1)[-1] if ":" in vid_id else vid_id
        video = {
            "id": vid_id,
            "provider": provider,
            "provider_video_id": pid,
            "title": title or pid,
            "slug": slug or "",
            "duration": duration or 0,
            "thumbnail": thumb or "",
            "performers": perfs,
            "performer_thumbs": svc.load_performer_thumbs(conn, perfs),
            "studio": studio or "",
            "dateadded": svc.format_dateadded(first_seen) if first_seen else "",
        }
        old_bytes = nfo_path.read_bytes()
        svc.create_nfo_file(video, genres, tags, nfo_path, conn=conn)
        if nfo_path.read_bytes() != old_bytes:
            updated += 1

    try:
        with conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('display_title_version', %s) "
                "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                (str(display_title_version),),
            )
    except Exception as e:
        svc.logger.warning(f"Could not update display_title_version in meta: {e}")

    if updated > 0:
        svc.logger.info(f"Updated {updated} NFO files with clean display titles.")
    return updated
