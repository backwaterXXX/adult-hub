import os
import re
import argparse
import hashlib
import time
import json
from common import db
from common.performers import is_performer_segment
import fcntl
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional, Set, Tuple, Iterable

from sync.category_mapping import map_category, map_categories
from sync.sync_shutdown import install_signal_handlers, stop_requested
from sync.db_schema import init_db as _init_db_impl
from sync.jellyfin_db import sync_jellyfin_titles as _sync_jellyfin_titles
from sync.jellyfin_db import sync_jellyfin_telegram_metadata as _sync_jellyfin_telegram_metadata
from sync.jellyfin_db import audit_telegram_import_state as _audit_telegram_import_state
from sync.jellyfin_db import trigger_library_refresh as _trigger_jellyfin_refresh_impl
from sync.jellyfin_db import confirm_metadata_import_jobs as _confirm_metadata_import_jobs_impl
from sync.http_client import (
    fetch_json_with_retry as _fetch_json_with_retry,
    get_http_client as _get_http_client,
    http_get as _http_get,
    is_retryable_error as _is_retryable_error_impl,
    parse_retry_after as _parse_retry_after,
    retry_after_from_error as _retry_after_from_error_impl,
)
from sync.sync_metrics import (
    observe_http_error as _observe_http_error_impl,
    observe_sync_stage as _observe_sync_stage_impl,
)
from sync.nfo import (
    create_nfo_file as _create_nfo_file_impl,
    format_dateadded as _format_dateadded_impl,
    load_performer_links as _load_performer_links_impl,
    load_performer_thumbs as _load_performer_thumbs_impl,
    video_for_nfo as _video_for_nfo_impl,
)
from sync.performer_enrichment import (
    PerformerEnrichmentServices,
    backfill_performers_from_titles as _backfill_performers_from_titles_impl,
)
from sync.telegram_pipeline import (
    TelegramSyncServices,
    safe_error_detail as _safe_error_detail,
    sync_telegram_channel_pipeline as _sync_telegram_channel_pipeline_impl,
    telegram_passes_tags as _telegram_passes_tags,
    telegram_tag_set as _telegram_tag_set,
    telegram_video_tags as _telegram_video_tags,
)
from sync.telegram_unavailable import reconcile_telegram_channel as _reconcile_telegram_channel_impl
from sync.media_files import (
    MediaFileServices,
    resolve_media_path as _resolve_media_path_impl,
    rewrite_all_stream_files as _rewrite_all_stream_files_impl,
    stable_media_basename as _stable_media_basename_impl,
    create_stream_file as _create_stream_file_impl,
    write_stream_file as _write_stream_file_impl,
)
from sync.library_cleanup import (
    LibraryCleanupServices,
    cleanup_orphaned_records as _cleanup_orphaned_records_impl,
    cleanup_retention as _cleanup_retention_impl,
    purge_videos as _purge_videos_impl,
)
from sync.data_migrations import (
    DataMigrationServices,
    CATEGORY_MAPPING_VERSION,
    DISPLAY_TITLE_VERSION,
    REDUNDANT_PERFORMERS_VERSION,
    _STABLE_STRM_RE,
    migrate_synced_files as _migrate_synced_files_impl,
    migrate_categories as _migrate_categories_impl,
    migrate_display_titles as _migrate_display_titles_impl,
    migrate_redundant_performers as _migrate_redundant_performers_impl,
)
from sync.dead_streams import (
    DeadStreamServices,
    RESOLVER_DEAD_MARKERS,
    deadlisted_ids,
    denylist_ids,
    hide_dead_streams as _hide_dead_streams_impl,
    probe_playlist_url as _probe_playlist_url_impl,
    probe_stream_via_resolver as _probe_stream_via_resolver_impl,
    probe_stream_via_resolver_outcome as _probe_stream_via_resolver_outcome_impl,
)
from sync.thumbnails import (
    THUMB_FAIL_TTL_SECONDS,
    ThumbnailServices,
    _thumb_fail_marker,
    _valid_thumbnail_bytes,
    download_image as _download_image_impl,
    thumbnail_urls as _thumbnail_urls_impl,
)
from sync.provider_registry import (
    STREAM_MODE_DEFAULT,
    _STREAM_MODE_PER_PROVIDER,
    _PROVIDER_INFO,
    stream_mode_for_provider as _stream_mode_for_provider_impl,
    get_provider_info as _get_provider_info_impl,
    known_provider_names as _known_provider_names_impl,
    normalize_cleanup_provider as _normalize_cleanup_provider_impl,
    _telegram_channel_of_video as _telegram_channel_of_video_impl,
    _normalized_success_set as _normalized_success_set_impl,
    is_cleanup_allowed as _is_cleanup_allowed_impl,
)
from sync.category_collector import (
    CategoryCollectorServices,
    _CATEGORY_SKIP,
    register_category_skip as _register_category_skip_impl,
    fetch_provider_categories as _fetch_provider_categories_impl,
    fetch_provider_category_videos as _fetch_provider_category_videos_impl,
    collect_provider_categories as _collect_provider_categories_impl,
)
from sync.video_persistence import (
    VideoPersistenceServices,
    _merge_video_fields as _merge_video_fields_impl,
    persist_prepared_video as _persist_prepared_video_impl,
    sync_unique_video as _sync_unique_video_impl,
    flush_collected as _flush_collected_impl,
    republish_pending_videos as _republish_pending_videos_impl,
)
from sync.duplicate_files import (
    _write_catalog_backup, _duplicate_file_matches, _duplicate_move_file,
)
from sync.duplicate_jobs import (
    DuplicateJobServices,
    process_video_duplicate_jobs_locked as _process_video_duplicate_jobs_impl,
)
from sync.media_maintenance import (
    cleanup_old_transcodes as _cleanup_old_transcodes,
    cleanup_legacy_folders as _cleanup_legacy_folders,
)
from common.duration import parse_duration_seconds
from common.performers import (
    ACTION_VERBS,
    MEDIA_EXT_SUFFIX_RE,
    NATIONALITY_PREFIXES,
    PARTICLES,
    TITLE_STOPWORDS,
    is_valid_performer_name,
    normalize_performers,
    prune_redundant_performers,
)
from common.performer_titles import parse_title_performers_with_known_map as _parse_title_performers_with_known_map
from common.logging_setup import configure_logging
from sync.display_titles import (
    clean_display_title,
    get_sync_slug_token_hook,
    register_sync_slug_token_hook,
)
from sync.external_metadata_scan import run_external_metadata_backfill as _run_external_metadata_backfill_impl

try:
    import httpx
except ImportError:
    httpx = None

from sync.sync_helpers import (
    sanitize_filename,
    format_category_name,
    parse_duration_to_seconds,
    parse_title_performers,
    _merge_video_fields,
    parse_retry_after,
    _retry_after_from_error,
    _is_retryable_error,
    format_dateadded,
    load_performer_thumbs,
    load_performer_links,
)
from sync.sync_settings import (
    RESOLVER_PUBLIC_URL,
    RESOLVER_API_URL,
    STREAM_PUBLIC_URL_TELEGRAM,
    MEDIA_DIR,
    DB_URL,
    DATA_DIR,
    CONFIG,
    JELLYFIN_URL,
    JELLYFIN_API_KEY,
    HIDE_DEAD_STREAMS,
    DEAD_STREAM_RESOLVER_TIMEOUT,
    DEAD_STREAM_DENYLIST_DAYS,
    PBC_BACKFILL_PER_RUN,
    TPDB_BACKFILL_PER_RUN,
    SCENE_BACKFILL_PER_RUN,
    JELLYFIN_PEOPLE_PER_RUN,
    RETENTION_DAYS,
    SYNC_TELEGRAM,
    TELEGRAM_CHANNELS,
    TELEGRAM_MAX_PAGES,
    logger,
)

def stream_mode_for_provider(video_id: str) -> str:
    return _stream_mode_for_provider_impl(video_id, config=CONFIG)

def get_provider_info(provider_key: str) -> Dict[str, str]:
    return _get_provider_info_impl(provider_key)

def known_provider_names() -> Set[str]:
    return _known_provider_names_impl()

def normalize_cleanup_provider(name: str) -> str:
    return _normalize_cleanup_provider_impl(name, config=CONFIG)

def _telegram_channel_of_video(video_id: str) -> Optional[str]:
    return _telegram_channel_of_video_impl(video_id)

def _normalized_success_set(providers_succeeded: Optional[Set[str]]) -> Set[str]:
    return _normalized_success_set_impl(providers_succeeded, config=CONFIG)

def is_cleanup_allowed(video_id: str, provider: Optional[str] = None,
                       providers_succeeded: Optional[Set[str]] = None) -> bool:
    return _is_cleanup_allowed_impl(
        video_id,
        provider=provider,
        providers_succeeded=providers_succeeded,
        config=CONFIG,
    )


def _check_resolver_auth(resolver_url: str) -> Optional[bool]:
    base_url = str(resolver_url).rstrip("/")
    try:
        import urllib.request
        req = urllib.request.Request(
            f"{base_url}/admin/internal/telegram/auth/any",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30.0) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                if isinstance(data, dict) and isinstance(data.get("authorized"), bool):
                    return data["authorized"]
    except Exception:
        pass
    status_urls = [
        f"{base_url}/admin/internal/telegram/auth/status",
        f"{base_url}/admin/api/v2/telegram/auth/status",
    ]
    for status_url in status_urls:
        try:
            import urllib.request
            req = urllib.request.Request(status_url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30.0) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    if isinstance(data, dict) and ("authorized" in data or data.get("ok")):
                        return bool(data.get("authorized"))
        except Exception:
            continue
    return None


def _check_local_session_auth() -> bool:
    try:
        session_path = os.getenv("TELEGRAM_SESSION_PATH", "/data/telegram.session")
        if not session_path.endswith(".session"):
            session_path = f"{session_path}.session"
        if not os.path.isfile(session_path) or os.path.getsize(session_path) == 0:
            return False

        api_id_val = os.getenv("TELEGRAM_API_ID")
        api_hash_val = os.getenv("TELEGRAM_API_HASH")
        if not api_id_val or not api_hash_val:
            return False

        try:
            api_id_int = int(str(api_id_val).strip())
        except (ValueError, TypeError):
            return False

        try:
            from resolver.providers.telegram_session import is_session_authorized
        except ImportError:
            # The sync image has no Telegram client library (telethon), so the
            # session check fails closed there.
            return False

        import asyncio
        return bool(asyncio.run(is_session_authorized(session_path, api_id_int, str(api_hash_val).strip())))
    except Exception:
        return False


def is_telegram_authorized() -> bool:
    """Check if Telegram authorization is established via Resolver API or local session check.

    Fails closed: returns True ONLY when authorization is positively confirmed.
    File existence alone is NOT authorization.
    """
    resolver_url = os.getenv("RESOLVER_API_URL")
    if resolver_url:
        authed = _check_resolver_auth(resolver_url)
        if authed is not None:
            return authed
    return _check_local_session_auth()

def resolve_media_path(stored_path: str, media_dir: Path) -> Path:
    return _resolve_media_path_impl(stored_path, media_dir)

def stable_media_basename(video_id: str) -> str:
    return _stable_media_basename_impl(video_id)

def write_stream_file(path: Path, video_id: str, mode: Optional[str] = None) -> bool:
    return _write_stream_file_impl(
        path, video_id, mode,
        services=MediaFileServices(
            stream_mode_for_provider=stream_mode_for_provider,
            resolver_public_url=RESOLVER_PUBLIC_URL,
            stream_public_url_telegram=STREAM_PUBLIC_URL_TELEGRAM,
            is_cleanup_allowed=is_cleanup_allowed,
            logger=logger,
        ),
    )

def create_stream_file(path: Path, video_id: str, mode: Optional[str] = None) -> bool:
    return _create_stream_file_impl(
        path, video_id, mode,
        services=MediaFileServices(
            stream_mode_for_provider=stream_mode_for_provider,
            resolver_public_url=RESOLVER_PUBLIC_URL,
            stream_public_url_telegram=STREAM_PUBLIC_URL_TELEGRAM,
            is_cleanup_allowed=is_cleanup_allowed,
            logger=logger,
        ),
    )

def rewrite_all_stream_files(conn: db.Connection, media_dir: Path,
                             providers_succeeded: Optional[Set[str]] = None) -> Tuple[int, int]:
    """Rewrite every known .strm to the currently configured mode.

    One-shot upkeep: flips stale files (e.g. written before a provider
    switched redirect->proxy) without waiting for re-sync or retention.
    Guarded by is_cleanup_allowed(): absent, disabled or failed providers
    keep their existing .strm bytes untouched (disabling retention alone
    never changes stored URLs). Returns (checked, rewritten).
    """
    return _rewrite_all_stream_files_impl(
        conn, media_dir, providers_succeeded,
        services=MediaFileServices(
            stream_mode_for_provider=stream_mode_for_provider,
            resolver_public_url=RESOLVER_PUBLIC_URL,
            stream_public_url_telegram=STREAM_PUBLIC_URL_TELEGRAM,
            is_cleanup_allowed=is_cleanup_allowed,
            logger=logger,
        ),
        resolve_path_fn=resolve_media_path,
        write_stream_file_fn=write_stream_file,
    )





DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}













_HTTP_CLIENT: Optional[Any] = None

def get_http_client():
    global _HTTP_CLIENT
    _HTTP_CLIENT = _get_http_client(httpx, DEFAULT_HEADERS, _HTTP_CLIENT)
    return _HTTP_CLIENT

def http_get(url: str, headers: Optional[Dict[str, str]] = None, timeout: float = 15.0) -> bytes:
    return _http_get(
        url, headers=headers, timeout=timeout,
        get_client=get_http_client, default_headers=DEFAULT_HEADERS,
    )






def _data_migration_services() -> DataMigrationServices:
    return DataMigrationServices(
        logger=logger,
        video_is_disabled=_video_is_disabled,
        resolve_media_path=resolve_media_path,
        stable_media_basename=stable_media_basename,
        write_stream_file=write_stream_file,
        map_categories=map_categories,
        prune_redundant_performers=prune_redundant_performers,
        load_performer_thumbs=load_performer_thumbs,
        format_dateadded=format_dateadded,
        create_nfo_file=create_nfo_file,
    )


def migrate_synced_files(conn: db.Connection, media_dir: Path):
    return _migrate_synced_files_impl(conn, media_dir, services=_data_migration_services())


def migrate_categories(conn: db.Connection, media_dir: Path):
    return _migrate_categories_impl(
        conn, media_dir, services=_data_migration_services(),
        category_mapping_version=CATEGORY_MAPPING_VERSION,
    )


def migrate_display_titles(conn: db.Connection, media_dir: Path) -> int:
    return _migrate_display_titles_impl(
        conn, media_dir, services=_data_migration_services(),
        display_title_version=DISPLAY_TITLE_VERSION,
    )


def migrate_redundant_performers(conn: db.Connection, media_dir: Path) -> int:
    return _migrate_redundant_performers_impl(
        conn, media_dir, services=_data_migration_services(),
        redundant_performers_version=REDUNDANT_PERFORMERS_VERSION,
    )




def init_db(db_url: str | None = None) -> db.Connection:
    return _init_db_impl(db_url or DB_URL, map_category_fn=map_category)






























_ACTIVE_SYNC_METRICS = None


def _observe_sync_stage(name: str, elapsed: float, failed: bool = False) -> None:
    _observe_sync_stage_impl(_ACTIVE_SYNC_METRICS, name, elapsed, failed)


def _observe_http_error(url: str, error: Exception, retrying: bool) -> None:
    _observe_http_error_impl(_ACTIVE_SYNC_METRICS, url, error, retrying)


def fetch_json_with_retry(url: str, timeout: float = 20.0, max_retries: int = 3, desc: str = "request") -> Any:
    started = time.monotonic()
    failed = False
    try:
        return _fetch_json_with_retry(
            url, timeout=timeout, max_retries=max_retries, desc=desc,
            http_get_fn=http_get, logger=logger, sleep_fn=time.sleep,
            is_retryable_fn=_is_retryable_error,
            on_error=lambda error, attempt, retrying, backoff: _observe_http_error(url, error, retrying),
            retry_after_fn=_retry_after_from_error,
        )
    except Exception:
        failed = True
        raise
    finally:
        _observe_sync_stage("http_request", time.monotonic() - started, failed)

def fetch_provider_categories(provider: str) -> List[Dict[str, str]]:
    return _fetch_provider_categories_impl(
        provider,
        resolver_api_url=RESOLVER_API_URL,
        fetch_json_fn=fetch_json_with_retry,
    )


def fetch_provider_category_videos(provider: str, category_id: str, page: int) -> List[Dict[str, Any]]:
    return _fetch_provider_category_videos_impl(
        provider,
        category_id,
        page,
        resolver_api_url=RESOLVER_API_URL,
        fetch_json_fn=fetch_json_with_retry,
    )


def register_category_skip(provider: str, category_id: str) -> None:
    """Register a (provider, category) pair to skip in category walks."""
    _register_category_skip_impl(provider, category_id, skips=_CATEGORY_SKIP)


def _category_collector_services() -> CategoryCollectorServices:
    return CategoryCollectorServices(
        fetch_categories=fetch_provider_categories,
        fetch_category_videos=fetch_provider_category_videos,
        get_provider_info=get_provider_info,
        format_category_name=format_category_name,
        map_category=map_category,
        merge_video_fields=_merge_video_fields,
        category_skips=_CATEGORY_SKIP,
        logger=logger,
        sleep_fn=time.sleep,
        category_fetch_delay_seconds=(CONFIG.sync.category_fetch_delay_seconds if CONFIG else 2.5),
    )


def collect_provider_categories(collected: Dict[str, Dict[str, Any]], provider: str, max_pages: int) -> bool:
    """Merge category membership from a provider into the deduplicated catalog."""
    return _collect_provider_categories_impl(
        collected,
        provider,
        max_pages,
        services=_category_collector_services(),
    )

# Politeness delay between upstream listing fetches (upstreams throttle
# bursts with 429). Applies to browse sections and the category loop.

def _thumbnail_services() -> ThumbnailServices:
    return ThumbnailServices(
        http_get=http_get,
        logger=logger,
        time=time,
        fail_ttl_seconds=THUMB_FAIL_TTL_SECONDS,
    )


def thumbnail_urls(raw_url: str) -> Tuple[str, str]:
    return _thumbnail_urls_impl(
        raw_url,
        resolver_public_url=RESOLVER_PUBLIC_URL,
        resolver_api_url=RESOLVER_API_URL,
    )


def download_image(url: str, dest_path: Path):
    return _download_image_impl(url, dest_path, services=_thumbnail_services())


def download_image_if_missing(url: str, dest_path: Path) -> bool:
    from sync.thumbnails import download_image_if_missing as _download_if_missing
    return _download_if_missing(url, dest_path, services=_thumbnail_services())












def _video_for_nfo(video: Dict[str, Any], conn: Optional[db.Connection] = None) -> Dict[str, Any]:
    """Overlay persistent title/plot corrections without changing source rows."""
    return _video_for_nfo_impl(video, conn=conn, db_url=DB_URL)


def create_nfo_file(video: Dict[str, Any], genres: Set[str], tags: Set[str], dest_path: Path,
                    *, conn: Optional[db.Connection] = None):
    video = _video_for_nfo(video, conn=conn)
    if (video.get("metadata_fields", {}).get("tags", {}).get("overridden")
            and isinstance(video.get("tags"), list)):
        tags = set(video["tags"])
    if conn is not None and not video.get("performer_links"):
        perfs = video.get("performers") or []
        if perfs:
            video = dict(video)
            video["performer_links"] = load_performer_links(conn, perfs)
    return _create_nfo_file_impl(
        video, genres, tags, dest_path,
        get_provider_info_fn=get_provider_info,
        clean_display_title_fn=clean_display_title,
        normalize_performers_fn=(
            lambda names: normalize_performers(names, preserve_literal=True)
            if video.get("performer_resolution") else normalize_performers(names)
        ),
        map_categories_fn=map_categories,
    )


def _persist_prepared_video(conn: db.Connection, prepared: Dict[str, Any]):
    """Persist one media-prepared video inside the caller's DB transaction."""
    _persist_prepared_video_impl(conn, prepared, time_fn=time.time)


def _load_sync_alias_map(conn: Optional[db.Connection]) -> Optional[Dict[str, str]]:
    if conn is None:
        return None
    try:
        rows = conn.execute("SELECT alias, canonical FROM performer_aliases").fetchall()
        return {str(a).strip().lower(): str(c).strip() for a, c in rows if a and c}
    except db.Error as exc:
        logger.warning(
            "Could not load performer aliases; continuing without canonicalization (%s).",
            type(exc).__name__,
        )
        return None


def _video_persistence_services(conn: Optional[db.Connection] = None) -> VideoPersistenceServices:
    alias_map = _load_sync_alias_map(conn)
    norm_fn = (lambda names: normalize_performers(names, alias_map=alias_map)) if alias_map else normalize_performers

    def _create_nfo_with_aliases(video: Dict[str, Any], genres: Set[str], tags: Set[str], dest_path: Path):
        video = _video_for_nfo(video, conn)
        if (video.get("metadata_fields", {}).get("tags", {}).get("overridden")
                and isinstance(video.get("tags"), list)):
            tags = set(video["tags"])
        if conn is not None and not video.get("performer_links"):
            perfs = video.get("performers") or []
            if perfs:
                video = dict(video)
                video["performer_links"] = load_performer_links(conn, perfs)
        return _create_nfo_file_impl(
            video, genres, tags, dest_path,
            get_provider_info_fn=get_provider_info,
            clean_display_title_fn=clean_display_title,
            normalize_performers_fn=(
                lambda names: normalize_performers(names, preserve_literal=True)
                if video.get("performer_resolution") else norm_fn(names)
            ),
            map_categories_fn=map_categories,
        )

    return VideoPersistenceServices(
        stable_media_basename=stable_media_basename,
        map_categories=map_categories,
        normalize_performers=norm_fn,
        thumbnail_urls=thumbnail_urls,
        load_performer_thumbs=load_performer_thumbs,
        load_performer_links=load_performer_links,
        format_dateadded=format_dateadded,
        create_nfo_file=_create_nfo_with_aliases,
        download_image=download_image,
        write_stream_file=write_stream_file,
        sync_unique_video=sync_unique_video,
        persist_prepared_video=_persist_prepared_video,
        logger=logger,
        time_fn=time.time,
    )


def sync_unique_video(video: Dict[str, Any], genres: Set[str], tags: Set[str],
                      target_dir: Path, conn: db.Connection, commit: bool = True,
                      *, _prepare_only: bool = False):
    return _sync_unique_video_impl(
        video=video,
        genres=genres,
        tags=tags,
        target_dir=target_dir,
        conn=conn,
        commit=commit,
        _prepare_only=_prepare_only,
        services=_video_persistence_services(conn),
    )


def flush_collected(collected: Dict[str, Dict[str, Any]], conn: db.Connection,
                    media_dir: Path, source_name: str = "", *, raise_on_error: bool = False) -> int:
    """Flush collected videos to DB and media_dir, merging previously known genres and tags from DB.
    Clears the dictionary after writing to free memory and prevent re-writing."""
    return _flush_collected_impl(
        collected=collected,
        conn=conn,
        media_dir=media_dir,
        source_name=source_name,
        raise_on_error=raise_on_error,
        services=_video_persistence_services(conn),
    )


def republish_pending_videos(conn: db.Connection, media_dir: Path,
                             limit: int = 50) -> Dict[str, int]:
    """Retry library publication for catalog rows whose sidecars never landed."""
    return _republish_pending_videos_impl(
        conn, media_dir, services=_video_persistence_services(conn), limit=limit,
    )




def cleanup_old_transcodes(transcodes_dir: Optional[Path] = None, max_age_hours: int = 2):
    return _cleanup_old_transcodes(transcodes_dir, max_age_hours, logger=logger, clock=time)


def cleanup_legacy_folders(media_dir: Path):
    return _cleanup_legacy_folders(media_dir, logger=logger)


def cleanup_orphaned_records(media_dir: Path, conn: db.Connection,
                             providers_succeeded: Optional[Set[str]] = None):
    return _cleanup_orphaned_records_impl(
        media_dir, conn, providers_succeeded,
        services=LibraryCleanupServices(
            is_cleanup_allowed=is_cleanup_allowed,
            video_is_disabled=_video_is_disabled,
            resolve_media_path=resolve_media_path,
            logger=logger,
            time=time,
        ),
    )

def cleanup_retention(conn: db.Connection, media_dir: Path, retention_days: int,
                      providers_succeeded: Optional[Set[str]] = None):
    return _cleanup_retention_impl(
        conn, media_dir, retention_days, providers_succeeded,
        services=LibraryCleanupServices(
            is_cleanup_allowed=is_cleanup_allowed,
            video_is_disabled=_video_is_disabled,
            resolve_media_path=resolve_media_path,
            logger=logger,
            time=time,
        ),
    )

def trigger_jellyfin_refresh(*, min_interval: float = 3.0):
    return _trigger_jellyfin_refresh_impl(
        JELLYFIN_URL, JELLYFIN_API_KEY, min_interval=min_interval,
        http_client_factory=get_http_client, logger=logger, time_fn=time.time,
    )

def sync_jellyfin_titles(hub_db_url=None):
    return _sync_jellyfin_titles(
        hub_db_url=hub_db_url, default_hub_db_url=DB_URL,
        clean_title=clean_display_title, url=JELLYFIN_URL,
        api_key=JELLYFIN_API_KEY, logger=logger, media_dir=MEDIA_DIR,
    )



def sync_jellyfin_telegram_metadata(*, limit=200):
    result = _sync_jellyfin_telegram_metadata(
        JELLYFIN_URL, JELLYFIN_API_KEY, MEDIA_DIR,
        state_path=DATA_DIR / "telegram-metadata-state.json", logger=logger,
        limit=limit,
    )
    confirmed_files = result.pop("_confirmed_file_hashes", {}) if isinstance(result, dict) else {}
    if confirmed_files:
        _confirm_metadata_import_jobs(confirmed_files, DB_URL)
    return result


def _confirm_metadata_import_jobs(confirmed_files, db_url):
    return _confirm_metadata_import_jobs_impl(
        confirmed_files, db_url, logger=logger, time_fn=time.time,
    )



def _dead_stream_services() -> DeadStreamServices:
    return DeadStreamServices(
        stream_mode_for_provider=stream_mode_for_provider,
        resolver_api_url=RESOLVER_API_URL,
        logger=logger,
        default_headers=DEFAULT_HEADERS,
        time=time,
        resolver_timeout=DEAD_STREAM_RESOLVER_TIMEOUT,
        denylist_days=DEAD_STREAM_DENYLIST_DAYS,
    )


def probe_playlist_url(playlist_url: str) -> Optional[bool]:
    ua = DEFAULT_HEADERS.get("User-Agent") if "DEFAULT_HEADERS" in globals() else None
    return _probe_playlist_url_impl(playlist_url, user_agent=ua)


def probe_stream_via_resolver(comp_id: str) -> Optional[bool]:
    return _probe_stream_via_resolver_impl(
        comp_id,
        services=_dead_stream_services(),
        probe_playlist_fn=probe_playlist_url,
    )


def probe_stream_via_resolver_outcome(comp_id: str) -> str:
    return _probe_stream_via_resolver_outcome_impl(comp_id, services=_dead_stream_services())


def hide_dead_streams(collected: Dict[str, Dict[str, Any]], conn: db.Connection) -> None:
    return _hide_dead_streams_impl(collected, conn, services=_dead_stream_services())



def purge_videos(conn: db.Connection, media_dir: Path, comp_ids: List[str], reason: str,
                 providers_succeeded: Optional[Set[str]] = None, force: bool = False) -> int:
    return _purge_videos_impl(
        conn, media_dir, comp_ids, reason, providers_succeeded, force,
        services=LibraryCleanupServices(
            is_cleanup_allowed=is_cleanup_allowed,
            video_is_disabled=_video_is_disabled,
            resolve_media_path=resolve_media_path,
            logger=logger,
            time=time,
        ),
    )








def safe_process_duplicate_jobs_checkpoint(
    conn: db.Connection,
    media_dir: Path,
    collected: Optional[Dict[str, Any]] = None,
) -> int:
    """Safe checkpoint for processing duplicate/merge jobs during full sync.

    Safety invariants:
    1. Must NOT run if in-memory collected records exist (len(collected) > 0),
       preventing subsequent flushes from overwriting merged performer names.
    2. Must verify no open uncommitted transaction.
    3. If jobs are processed, any newly added performer aliases are immediately
       available to subsequent pipeline stages via _video_persistence_services.
    """
    if collected:
        # Invariant: Unflushed in-memory video records exist! Cannot run safely now.
        return 0
    try:
        from common.job_queue import WORKER_ACTIONS
        has_jobs = conn.execute(
            "SELECT 1 FROM video_duplicate_jobs WHERE state='queued' "
            "AND action = ANY(%s) LIMIT 1",
            (list(WORKER_ACTIONS),),
        ).fetchone()
        if not has_jobs:
            return 0
        completed = process_video_duplicate_jobs(conn, media_dir)
        if completed > 0:
            logger.info("Safe checkpoint processed %d queued decision(s).", completed)
            try:
                trig = Path(os.getenv("SYNC_TRIGGER_FILE", "/data/cache/sync.trigger"))
                if trig.exists():
                    payload = json.loads(trig.read_text()) if trig.stat().st_size else {}
                    if isinstance(payload, dict) and payload.get("action") == "jobs":
                        trig.unlink(missing_ok=True)
            except Exception:
                logger.debug("Ignored exception in safe_process_duplicate_jobs_checkpoint", exc_info=True)
        return completed
    except Exception as e:
        logger.warning("Could not process queued duplicate decisions at checkpoint: %s", e)
        return 0


def run_jobs_only() -> int:
    """Process queued duplicate decisions and performer merges without full sync.

    Initializes only the minimal database connection and media directory required
    for video_duplicate_jobs, processes the queue, and exits immediately.
    Guarded by .adult-hub-sync.lock to ensure it does not run concurrently with full sync.
    """
    lock_path = DATA_DIR / ".adult-hub-sync.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.info("Full sync is active; queue will be processed at next safe sync checkpoint.")
            return 0
        try:
            conn = init_db()
            try:
                completed = process_video_duplicate_jobs(conn, MEDIA_DIR, include_long_running=True)
                if completed > 0:
                    logger.info("Jobs worker completed %d queued decision(s).", completed)
                return completed
            finally:
                conn.close()
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _jobs_lock_root(conn: db.Connection, media_dir: Path) -> Path:
    return DATA_DIR.resolve()


def process_video_duplicate_jobs(
    conn: db.Connection,
    media_dir: Path,
    *,
    include_long_running: bool = False,
) -> int:
    """Serialize queue consumers; an OS lock makes restart recovery safe."""
    lock_root = _jobs_lock_root(conn, media_dir)
    lock_root.mkdir(parents=True, exist_ok=True)
    lock_path = lock_root / ".video_duplicate_jobs.lock"
    with lock_path.open("a") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.info("Another sync worker owns the durable job queue")
            return 0
        try:
            return _process_video_duplicate_jobs_locked(
                conn, media_dir, include_long_running=include_long_running
            )
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _run_external_metadata_backfill(conn: db.Connection, media_dir: Path, report_progress) -> dict:
    """Run the same bounded external metadata passes as sync without rescanning sources."""
    return _run_external_metadata_backfill_impl(
        conn,
        media_dir,
        report_progress,
        pbc_limit=PBC_BACKFILL_PER_RUN,
        tpdb_limit=TPDB_BACKFILL_PER_RUN,
        scenes_limit=SCENE_BACKFILL_PER_RUN,
        logger=logger,
    )


def _run_telegram_fingerprint_scan(conn: db.Connection, media_dir: Path, video_ids, report_progress):
    """Run a durable targeted Telegram fingerprint job without scanning channels."""
    from sync.scene_scrapers import backfill_telegram_fingerprints
    return backfill_telegram_fingerprints(
        conn, media_dir, limit=len(video_ids), sleep_sec=0.5, video_ids=video_ids,
        progress_callback=report_progress,
    )


def _duplicate_job_services() -> DuplicateJobServices:
    """Build current dependencies so the sync facade stays patchable."""
    return DuplicateJobServices(
        jobs_lock_root=_jobs_lock_root,
        video_is_disabled=_video_is_disabled,
        write_catalog_backup=_write_catalog_backup,
        duplicate_file_matches=_duplicate_file_matches,
        duplicate_move_file=_duplicate_move_file,
        create_nfo_file=create_nfo_file,
        create_stream_file=create_stream_file,
        format_dateadded=format_dateadded,
        load_performer_thumbs=load_performer_thumbs,
        resolve_media_path=resolve_media_path,
        trigger_jellyfin_refresh=trigger_jellyfin_refresh,
        sync_jellyfin_telegram_metadata=sync_jellyfin_telegram_metadata,
        probe_stream=probe_stream_via_resolver,
        logger=logger,
        probe_stream_detailed=probe_stream_via_resolver_outcome,
        audit_jellyfin_import_state=lambda media_dir, progress=None: _audit_telegram_import_state(
            JELLYFIN_URL, JELLYFIN_API_KEY, media_dir,
            state_path=DATA_DIR / "telegram-metadata-state.json",
            progress=progress,
        ),
        thumbnail_urls=thumbnail_urls,
        download_image_if_missing=download_image_if_missing,
        external_metadata_backfill=_run_external_metadata_backfill,
        telegram_fingerprint_scan=_run_telegram_fingerprint_scan,
    )


def _process_video_duplicate_jobs_locked(
    conn: db.Connection,
    media_dir: Path,
    *,
    include_long_running: bool = False,
) -> int:
    return _process_video_duplicate_jobs_impl(
        conn, media_dir, services=_duplicate_job_services(), include_long_running=include_long_running
    )


def _performer_enrichment_services():
    return PerformerEnrichmentServices(
        parse_title_performers=parse_title_performers,
        load_performer_thumbs=load_performer_thumbs,
        format_dateadded=format_dateadded,
        stable_media_basename=stable_media_basename,
        create_nfo_file=create_nfo_file,
        logger=logger,
    )


def backfill_performers_from_titles(conn: db.Connection, media_dir: Path) -> int:
    return _backfill_performers_from_titles_impl(
        conn, media_dir, services=_performer_enrichment_services()
    )


def _telegram_sync_services() -> TelegramSyncServices:
    """Build dynamic dependencies so the long-standing sync facade remains patchable."""
    return TelegramSyncServices(
        resolver_public_url=RESOLVER_PUBLIC_URL,
        resolver_api_url=RESOLVER_API_URL,
        map_category=map_category,
        merge_video_fields=_merge_video_fields,
        purge_videos=purge_videos,
        fetch_json_with_retry=fetch_json_with_retry,
        flush_collected=flush_collected,
        logger=logger,
        time=time,
        observe_stage=_observe_sync_stage,
        category_fetch_delay_seconds=(CONFIG.sync.category_fetch_delay_seconds if CONFIG else 2.5),
    )


def _video_is_disabled(video_id: str, provider: Optional[str] = None) -> bool:
    if not CONFIG:
        return False
    provider = provider or video_id.split(":", 1)[0]
    if provider in CONFIG.get_disabled_provider_names():
        return True
    if provider == "telegram":
        parts = video_id.split(":")
        return len(parts) > 2 and parts[1] in CONFIG.get_disabled_telegram_channel_ids()
    return False


def sync_telegram_channel_pipeline(
    channel_id: str, channel_cfg: Any, conn: db.Connection,
    media_dir: Path, collected: Dict[str, Dict[str, Any]], *, sync_mode: str = "scheduled",
):
    """Apply channel filters and consume one resumable raw history batch per run."""
    return _sync_telegram_channel_pipeline_impl(
        channel_id, channel_cfg, conn, media_dir, collected,
        services=_telegram_sync_services(), sync_mode=sync_mode,
    )


def reconcile_telegram_channel(*args, **kwargs):
    return _reconcile_telegram_channel_impl(*args, **kwargs)


def _video_id_snapshot(conn: db.Connection) -> Dict[str, Set[str]]:
    grouped: Dict[str, Set[str]] = {}
    for video_id, provider in conn.execute("SELECT id, provider FROM videos"):
        grouped.setdefault(str(provider or "unknown"), set()).add(str(video_id))
    return grouped


def _write_sync_history(report: dict) -> None:
    path = Path(os.getenv("SYNC_HISTORY_FILE", "/data/cache/sync_history.json"))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            current = json.loads(path.read_text()) if path.exists() else {"runs": []}
            runs = current.get("runs", []) if isinstance(current, dict) else []
        except (OSError, ValueError, TypeError):
            runs = []
        runs.insert(0, report)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"runs": runs[:100]}))
        os.replace(tmp, path)
    except OSError as e:
        logger.warning("Could not save sync history: %s", e)


def run_sync(source_filter: Optional[str] = None, sync_mode: str = "scheduled",
             *, skip_telegram_channels: bool = False):
    """Serialize sync runs so duplicate jobs and catalog writes have one owner."""
    lock_path = DATA_DIR / ".adult-hub-sync.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            return _run_sync_locked(source_filter, sync_mode, skip_telegram_channels)
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _run_sync_locked(source_filter: Optional[str] = None, sync_mode: str = "scheduled",
                     skip_telegram_channels: bool = False):
    """Run one sync pass (compatibility facade)."""
    from sync.sync_orchestration import _run_sync_locked as implementation
    return implementation(source_filter, sync_mode, skip_telegram_channels)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", help="Sync one configured provider or Telegram channel")
    parser.add_argument("--sync-mode", choices=("scheduled", "new", "backfill"), default="scheduled",
                        help="Run the normal combined pass, latest materials only, or one history batch")
    parser.add_argument("--skip-telegram-channels", action="store_true",
                        help="Run the full maintenance/enrichment pass without fetching Telegram channel history")
    parser.add_argument("--jobs-only", action="store_true", help="Process queued duplicate decisions / performer merges and exit immediately")
    args = parser.parse_args()
    if args.jobs_only:
        run_jobs_only()
        raise SystemExit(0)
    # A deploy stops the container, and Docker's SIGTERM used to kill this
    # process mid-channel. Record the request instead so the pass can finish the
    # channel it is on, flush the collected batch and write its history. The
    # jobs-only worker is left with the default disposition: it has no
    # checkpoint to reach, and durable jobs are recovered by their leases.
    install_signal_handlers()
    raise SystemExit(0 if run_sync(
        args.source, args.sync_mode,
        skip_telegram_channels=args.skip_telegram_channels,
    ) else 1)
