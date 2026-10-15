"""Performer backfill and audit operations for the sync database."""

import json
from common import db
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Tuple


@dataclass(frozen=True)
class PerformerEnrichmentServices:
    fetch_json_with_retry: Callable
    parse_title_performers: Callable
    normalize_performers: Callable
    performer_backfill_providers: Callable
    performer_audit_providers: Callable
    load_performer_thumbs: Callable
    format_dateadded: Callable
    stable_media_basename: Callable
    create_nfo_file: Callable
    resolver_api_url: str
    backfill_per_run: int
    backfill_sleep_seconds: float
    audit_threshold: int
    audit_per_run: int
    logger: Any
    time: Any


def _regenerate_performer_nfo(
    conn, media_dir, vid_id, provider, title, slug, duration, thumbnail,
    categories, tags, performers, services,
):
    try:
        genres = set(json.loads(categories)) if categories else set()
    except Exception:
        genres = set()
    try:
        tag_set = set(json.loads(tags)) if tags else set()
    except Exception:
        tag_set = set()

    provider_video_id = vid_id.split(":", 1)[-1]
    video = {
        "id": vid_id,
        "provider": provider,
        "provider_video_id": provider_video_id,
        "title": title or provider_video_id,
        "slug": slug or "",
        "duration": duration or 0,
        "thumbnail": thumbnail or "",
        "performers": performers,
        "performer_thumbs": services.load_performer_thumbs(conn, performers),
        "studio": "",
        "dateadded": "",
    }
    try:
        extra = conn.execute(
            "SELECT studio, first_seen FROM videos WHERE id = %s", (vid_id,)
        ).fetchone()
    except Exception:
        extra = None
    if extra:
        if extra[0]:
            video["studio"] = extra[0]
        if extra[1]:
            video["dateadded"] = services.format_dateadded(extra[1])

    nfo_path = media_dir / f"{services.stable_media_basename(vid_id)}.nfo"
    services.create_nfo_file(video, genres, tag_set, nfo_path, conn=conn)


def backfill_performers(conn: db.Connection, media_dir: Path,
                        providers: Optional[Iterable[str]] = None, *, services) -> Tuple[int, int]:
    """Fill missing performers via single-video page lookups (rate-limited).

    ``providers`` selects the queried providers (default: extension-
    registered backfill providers; empty without the extension = idle).
    Newest videos first, at most services.backfill_per_run per sync with a
    1s gap between requests. Only the source page supplies cast for these
    providers; a failed or empty lookup remains eligible after the retry delay.
    Found names are stored in DB and the sibling
    .nfo is regenerated. Returns (checked, updated).
    """
    if services.backfill_per_run <= 0:
        return 0, 0
    target = tuple(providers) if providers is not None else services.performer_backfill_providers()
    if not target:
        return 0, 0
    placeholders = ",".join("%s" for _ in target)
    # Negative cache: rows attempted in the last 30 days are skipped so
    # permanently-empty lookups (dead videos, no cast upstream) don't
    # starve the queue — fresh unknowns always drain first.
    retry_cutoff = int(services.time.time()) - 30 * 86400
    try:
        rows = conn.execute(
            f"""SELECT id, provider, title, slug, duration, thumbnail,
                       performers, categories, tags
                FROM videos
                WHERE provider IN ({placeholders})
                  AND (performers IS NULL OR performers = '[]'::jsonb)
                  AND (performers_checked_at IS NULL OR performers_checked_at < %s)
                ORDER BY last_seen DESC LIMIT %s""",
            (*target, retry_cutoff, services.backfill_per_run),
        ).fetchall()
    except Exception:
        try:  # legacy DB bez kolumny performers_checked_at (np. w testach)
            rows = conn.execute(
                f"""SELECT id, provider, title, slug, duration, thumbnail,
                           performers, categories, tags
                    FROM videos
                    WHERE provider IN ({placeholders})
                      AND (performers IS NULL OR performers = '[]'::jsonb)
                    ORDER BY last_seen DESC LIMIT %s""",
                (*target, services.backfill_per_run),
            ).fetchall()
        except Exception as e:
            services.logger.warning(f"Performer backfill: query failed: {e}")
            return 0, 0
    checked = 0
    updated = 0
    for vid_id, provider, title, slug, duration, thumb, _perf, cats, tags in rows:
        checked += 1
        try:
            with conn:
                conn.execute("UPDATE videos SET performers_checked_at = %s WHERE id = %s",
                             (int(services.time.time()), vid_id))
        except Exception:
            pass
        pid = vid_id.split(":", 1)[-1]
        try:
            from urllib.parse import quote
            url = (f"{services.resolver_api_url.rstrip('/')}/admin/api/video-performers"
                   f"?provider={provider}&pid={quote(pid, safe='')}&slug={quote(slug or '', safe='')}")
            data = services.fetch_json_with_retry(url, timeout=20.0, desc=f"performers/{vid_id}")
            names = services.normalize_performers((data or {}).get("performers", []))
        except Exception as e:
            services.logger.warning(f"Performer backfill failed for {vid_id}: {e}")
            # Failed requests may be transient. With the 30-day query cutoff,
            # backdating this marker by 29 days makes it eligible in one day.
            try:
                with conn:
                    conn.execute("UPDATE videos SET performers_checked_at = %s WHERE id = %s",
                                 (int(services.time.time()) - 29 * 86400, vid_id))
            except db.OperationalError:
                pass  # legacy DB without the marker
            services.time.sleep(services.backfill_sleep_seconds)
            continue
        if names:
            try:
                conn.execute("UPDATE videos SET performers = %s WHERE id = %s",
                             (json.dumps(names), vid_id))
                conn.commit()
                _regenerate_performer_nfo(
                    conn, media_dir, vid_id, provider, title, slug, duration,
                    thumb, cats, tags, names, services,
                )
                updated += 1
            except Exception as e:
                services.logger.warning(f"Performer backfill store failed for {vid_id}: {e}")
        services.time.sleep(services.backfill_sleep_seconds)
    if updated:
        services.logger.info(f"Performer backfill: {updated}/{checked} videos enriched")
    return checked, updated


def backfill_performers_from_titles(conn: db.Connection, media_dir: Path, *, services) -> int:
    """Instant title-based performer enrichment for stored videos lacking cast.

    Runs across stored videos with empty performers (zero network overhead).
    Providers with a page lookup are handled by the rate-limited backfill.
    Extracts performers from titles adhering to studio naming conventions
    ('P1, P2 & P3 – Title', 'Title – P1 & P2', 'P1 fucks P2') and cross-references
    single names against the DB's known performers dictionary.
    Updates DB and regenerates corresponding .nfo files. Returns count updated.
    """
    try:
        known_rows = conn.execute(
            "SELECT performers FROM videos WHERE performers <> '[]'::jsonb"
        ).fetchall()
        known_dict = {}
        for (raw,) in known_rows:
            try:
                items = json.loads(raw) if isinstance(raw, str) else []
                for n in items:
                    name_str = str(n or "").strip()
                    if name_str:
                        k = name_str.lower()
                        if k not in known_dict:
                            known_dict[k] = name_str
            except Exception:
                pass
        try:
            for alias, canonical in conn.execute(
                "SELECT alias, canonical FROM performer_aliases"
            ).fetchall():
                alias_key = str(alias or "").strip().lower()
                canonical_name = str(canonical or "").strip()
                if alias_key and canonical_name:
                    known_dict[alias_key] = canonical_name
        except db.Error:
            pass

        page_providers = tuple(services.performer_backfill_providers())
        rows = conn.execute(
            """SELECT id, provider, title, slug, duration, thumbnail,
                      performers, categories, tags
               FROM videos
               WHERE performers IS NULL OR performers = '[]'::jsonb"""
        ).fetchall()
    except Exception as e:
        services.logger.warning(f"Title performer backfill query failed: {e}")
        return 0

    updated = 0
    for vid_id, provider, title, slug, duration, thumb, _perf, cats, tags in rows:
        if provider in page_providers:
            continue
        names = services.parse_title_performers(title or "", known_performers=known_dict)
        if not names:
            continue
        try:
            conn.execute("UPDATE videos SET performers = %s WHERE id = %s",
                         (json.dumps(names), vid_id))
            conn.commit()
            _regenerate_performer_nfo(
                conn, media_dir, vid_id, provider, title, slug, duration,
                thumb, cats, tags, names, services,
            )
            updated += 1
            for n in names:
                k = n.strip().lower()
                if k not in known_dict:
                    known_dict[k] = n.strip()
        except Exception as e:
            services.logger.warning(f"Title performer backfill store failed for {vid_id}: {e}")

    if updated:
        services.logger.info(f"Title performer backfill: {updated} videos enriched from titles")
    return updated


def audit_suspicious_performers(conn: db.Connection, media_dir: Path,
                                threshold: int = 0, limit: int = 0,
                                providers: Optional[Iterable[str]] = None, *, services) -> Tuple[int, int]:
    """Re-check stored casts with >=threshold names against video pages.

    Listing cards sometimes accumulate foreign anchors (whole-block parsing);
    the video page (LD+JSON) is truth. Overwrites DB + NFO on successful
    lookup (even with fewer names); lookup failures keep stored data.
    ``providers`` defaults to the extension-registered audit providers
    (empty without the extension = idle). Returns (checked, corrected).
    """
    threshold = threshold or services.audit_threshold
    limit = limit or services.audit_per_run
    if limit <= 0:
        return 0, 0
    target = tuple(providers) if providers is not None else services.performer_audit_providers()
    if not target:
        return 0, 0
    placeholders = ",".join("%s" for _ in target)
    try:
        rows = conn.execute(
            f"""SELECT id, provider, title, slug, duration, thumbnail,
                       performers, categories, tags
                FROM videos WHERE provider IN ({placeholders})""",
            (*target,)).fetchall()
    except Exception as e:
        services.logger.warning(f"Performer audit: query failed: {e}")
        return 0, 0
    suspects = []
    for row in rows:
        vid_id = row[0]
        try:
            names = json.loads(row[6]) if row[6] else []
        except Exception:
            continue
        if isinstance(names, list) and len(names) >= threshold:
            suspects.append(row)
    # Least recently checked first, so a fixed per-run limit eventually
    # reaches every suspect, including those whose page lookup keeps failing.
    try:
        audited = dict(conn.execute(
            "SELECT id, performers_audited_at FROM videos WHERE provider IN ("
            + placeholders + ")", target).fetchall())
    except db.OperationalError:
        audited = {}
    suspects.sort(key=lambda r: (audited.get(r[0]) or 0, r[0]))
    checked = 0
    corrected = 0
    for (vid_id, provider, title, slug, duration, thumb,
         _perf, cats, tags) in suspects[:limit]:
        checked += 1
        try:
            with conn:
                conn.execute("UPDATE videos SET performers_audited_at = %s WHERE id = %s",
                             (services.time.time_ns(), vid_id))
        except db.OperationalError:
            pass  # legacy test DB without the migration
        pid = vid_id.split(":", 1)[-1]
        try:
            from urllib.parse import quote
            url = (f"{services.resolver_api_url.rstrip('/')}/admin/api/video-performers"
                   f"?provider={provider}&pid={quote(pid, safe='')}&slug={quote(slug or '', safe='')}")
            data = services.fetch_json_with_retry(url, timeout=20.0, desc=f"audit/{vid_id}")
            names = services.normalize_performers((data or {}).get("performers", []))
        except Exception as e:
            services.logger.warning(f"Performer audit failed for {vid_id}: {e}")
            services.time.sleep(services.backfill_sleep_seconds)
            continue
        try:
            old = services.normalize_performers(json.loads(_perf)) if _perf else []
        except Exception:
            old = []
        if set(names) == set(old):
            services.time.sleep(services.backfill_sleep_seconds)
            continue
        try:
            with conn:
                conn.execute("UPDATE videos SET performers = %s WHERE id = %s",
                             (json.dumps(names), vid_id))
            _regenerate_performer_nfo(
                conn, media_dir, vid_id, provider, title, slug, duration,
                thumb, cats, tags, names, services,
            )
            corrected += 1
        except Exception as e:
            services.logger.warning(f"Performer audit store failed for {vid_id}: {e}")
        services.time.sleep(services.backfill_sleep_seconds)
    if corrected:
        services.logger.info(f"Performer audit: {corrected}/{checked} casts corrected")
    return checked, corrected
