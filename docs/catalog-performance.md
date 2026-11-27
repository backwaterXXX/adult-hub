# Catalog read path: indexes, filters and the connection pool

This note describes how `GET /api/v1/catalog/*` answers a request after the
read-path work merged on 2026-11-27, what each index is for, and what to check
when a page is slow again. Measurements come from a copy of the production
catalog (27 993 videos) on PostgreSQL 17, medians of six runs.

## Where a request goes

1. `resolver/desktop_api.py` validates the bearer token, opens a connection
   (pooled in production) and runs the query off the event loop.
2. `resolver/client_catalog.py` builds the `WHERE`/`ORDER BY` and executes a
   `count(*)` plus the page query. Totals are cached for 30 s per filter set
   (`clear_catalog_count_cache()` drops them, tests call it after every reset).
3. Taxonomy counts, the schema probe and the channel map have their own
   short-lived caches keyed by database name.

## Before / after

| Path | Before | After |
|---|---|---|
| Performer detail | 51.4 ms | 20–24 ms |
| Performer page (50) | 39.2 ms | 26–28 ms |
| Favorites page | 40.3 ms | 24–28 ms |
| Search `flooded` | 173.0 ms | 12 ms |
| Search `anal` (common term) | 31.9 ms | 20–23 ms |
| Title sort | 32.9 ms | 6.9 ms |
| Category `Anal` | 6.1 ms | 8–12 ms |
| Category `anal` | 8.9 ms | 8–12 ms |
| Connect + one query | 5.96 ms | 0.20 ms |

The category row is the deliberate trade: matching `lower(category)` costs a few
milliseconds on the largest category (4 948 rows) and makes `G`/`g` equivalent.

## Indexes and migrations

`0010_catalog_search_trigram_favorites_index`:

- `pg_trgm` plus GIN indexes on `videos.title`, `videos.studio`, `videos.plot`
  for `ILIKE '%term%'` search.
- `client_user_favorites (user_id, video_id)` for the set-based favorite filter.
- `video_performers (lower(performer_name), video_id)` and
  `video_categories (lower(category), video_id)` for the case-insensitive
  performer and category filters.

`0011_video_title_sort`:

- `videos.title_sort` stores `lower(COALESCE(override title, title, ''))`.
- `idx_videos_title_sort (title_sort, id)` and
  `idx_videos_title_sort_desc (title_sort DESC, id ASC)` back both directions of
  `sort=title` with the deterministic `id ASC` tie-breaker.
- Triggers keep the column current: `videos_refresh_title_sort` on
  `INSERT OR UPDATE OF title`, and
  `video_metadata_overrides_refresh_title_sort` on override insert, update and
  delete. The override trigger updates only `title_sort`, so it does not fire the
  title trigger again.

## Filter semantics

- **Case-insensitive**: category, tag, studio, performer name and channel label.
- **Exact**: studio (no substring), tag and category values after `lower(trim())`.
- **Categories** read the `video_categories` mirror. `_categories_mirror_complete()`
  probes once per minute whether every categorized video has mirror rows; when
  the mirror is incomplete (fresh fixtures, a rebuild in progress) the filter
  reads `videos.categories` instead, so no video silently disappears.
- **Performers** read `video_performers` when the mirror covers the row; rows
  with a performer override keep the JSON path.
- **Search (`q`)** covers `title`, `plot`, `studio` and an exact video id. Override
  values are resolved once per request into an id array so the main predicate
  stays on the trigram indexes. Raw performer and tag JSON are never searched.
- **Favorites** are three set-based `IN` subqueries (direct, through
  `video_sources`, through active duplicate decisions), which preserves the
  merge semantics of the old correlated `EXISTS` without probing per row.

## Connection pool

`common/db.py::pooled_connect()` keeps up to eight idle sessions per process.
`close()` returns the session, rolls back an open transaction, resets
`autocommit` and the row factory, and discards closed sessions. Explicit test
URLs in `APP_STATE.db_url` keep plain connections, so a disposable database can
be dropped between cases. `close_pools()` closes the idle sessions.

## Slow-task log

`resolver/desktop_api.py::_run_in_thread()` logs
`slow API task <name> took <n> ms` for any task at or above 300 ms. That is the
first place to look when a client reports a slow page.

## Production after 0.6.12 (2026-11-27)

Deployed at 08:26 local; the sync applied migrations 0010 and 0011 at startup and
the resolver logged no errors.

| Path | Production |
|---|---|
| Catalog latest (50) | 10.2 ms |
| Search `anal` | 25.2 ms |
| Search `flooded` | 13.8 ms |
| Title sort | 9.0 ms |
| Performer page (50) | 36.8 ms |
| Performer detail | 30.9 ms |
| Favorites page | 28.5 ms |
| Quality `high` | 8.0 ms |
| Category `Anal` / `anal` | 11.2 / 11.5 ms |
| Taxonomy lists | 0.1–0.4 ms |

`EXPLAIN` on the title sort shows `Index Only Scan using idx_videos_title_sort`
(0.1 ms for the first 50 ids). `GET /api/v1/version` reports `0.6.12` with
`desktop_api`, `tv_pairing` and `user_state` enabled. Both spellings of the
category return the same total (4 924 for `Anal`/`anal`) and the category mirror
has no gaps.

## Known gaps

- **75 videos have no quality tier.** They all come from one channel whose
  Telegram documents carry neither dimensions nor file size, so the generated
  `quality_tier` is `NULL`. Filling them needs a source re-probe in the sync, not
  a database backfill.
- **Category filtering** is the only path that got slower (by ~3 ms on the
  largest category). Using the `videos.categories` GIN containment path would be
  faster but is case-sensitive; resolving a canonical spelling first was not
  worth the extra query for that margin.

## Verifying after a deploy

```sql
-- migration and indexes
SELECT version, name FROM schema_migrations ORDER BY version DESC LIMIT 2;
SELECT indexname FROM pg_indexes
 WHERE tablename IN ('videos', 'video_performers', 'video_categories')
   AND indexname LIKE '%title_sort%' OR indexname LIKE '%trgm%' OR indexname LIKE '%_lower';

-- the sort key is maintained
SELECT count(*) FILTER (WHERE title_sort IS NULL) FROM videos;

-- the category mirror is complete
SELECT count(*) FROM videos v
 WHERE jsonb_array_length(CASE WHEN jsonb_typeof(v.categories) = 'array'
        THEN v.categories ELSE '[]'::jsonb END) > 0
   AND NOT EXISTS (SELECT 1 FROM video_categories vc WHERE vc.video_id = v.id);
```

Both counts should be `0`.
