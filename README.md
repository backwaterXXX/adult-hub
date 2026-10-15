# Adult Hub

**Adult Hub** brings videos from Telegram channels into your [Jellyfin](https://jellyfin.org/) library, where you can browse and play them on your devices. It is designed for gay adult content.

Videos stream from Telegram when you play them, so Adult Hub does not download and store the video files in your library. It adds titles, performer details, and artwork to help you organize and find what you want to watch.

---

## How It Works

1. Adult Hub checks your configured Telegram channels and builds a browsable video library.
2. Jellyfin displays the videos with their titles, performer information, and artwork.
3. Choose a video in Jellyfin to stream it directly from Telegram. You can watch on your TV, phone, or web browser.

---

## Key Features

- **Gay adult video library**: Organize gay content from your Telegram channels in Jellyfin.
- **Stream without storing video files**: Videos play from Telegram on demand; your library keeps the catalog and artwork.
- **Watch on your preferred device**: Use Jellyfin on the web, phone, or TV. For Android TV, see the [Adult Hub TV player](https://github.com/backwaterxxx/adult-hub-tv).
- **Browse and organize**: Find videos by performer and use automatically updated Jellyfin collections.
- **Manage your library**: Review possible duplicates and keep unwanted items out of view without removing them from Telegram.

---

## Quick Start (Standard Clone: Telegram)

A standard deployment requires the public core repository, Docker Compose, and a PostgreSQL 17 database that the containers can reach over the Compose `selfhost` network.

### 0. Prerequisites

* **Docker** with the **Docker Compose** plugin.
* **PostgreSQL 17**, reachable from the containers as host `postgres`. Create a database and user for the hub and set the matching `DATABASE_URL` in `.env`. The database server is not part of this repository.
* The external network `selfhost` declared in `docker-compose.yml`. Compose will not create it, so create it once (or reuse the network created by your database stack) before starting:

```sh
docker network create selfhost
```

For tests and host tools, point `TEST_DATABASE_URL` at a separate database (never the production one).

### 1. Clone & Configure

```sh
git clone <CORE_REPOSITORY_URL> adult-hub
cd adult-hub
cp config.example.yaml config.yaml
cp .env.example .env
```

Edit `.env` and `config.yaml`:
* **`.env`**: Set your `HOST_IP`, `JELLYFIN_URL`, `JELLYFIN_API_KEY`, `DATABASE_URL`, your Telegram MTProto API credentials (`TELEGRAM_API_ID`, `TELEGRAM_API_HASH`), and your public playback URL (`RESOLVER_PUBLIC_URL`).
* **`config.yaml`**: Add your authorized Telegram channels under the `sources` section (the default example channel is fictional and disabled).

Advanced environment variables and fallback behavior are documented in the example configuration and Compose files.

### 2. Build & Launch

```sh
docker compose build resolver sync
docker compose up -d resolver sync
```

Resolver and sync source code is packaged into their Docker images; Compose mounts runtime data and configuration but does not overlay `/app`. Rebuild and recreate both services after code changes. This keeps each image self-contained so restoring a retained versioned image actually restores its application code.

### Isolated resolver development loop

For resolver UI/API work, `make dev` starts `docker-compose.dev.yml` in the background: Uvicorn watches the mounted resolver source and reloads on Python changes. The stack publishes only `127.0.0.1:18090`, creates a disposable PostgreSQL database in tmpfs, and runs migrations against that database before starting the resolver. It uses `config.example.yaml` and mounts source files read-only; it does not use `.env`, production networks, media, Jellyfin, Telegram sessions, or runtime data. Database and `/data`/`/cache` state are ephemeral and disappear when their containers are recreated. Use `make dev-logs` to follow resolver output and `make dev-down` to stop the stack.

The application version is stored in `VERSION` and is shown by the resolver health endpoint, the admin panel header, and the sync startup log. For a versioned image, set `APP_VERSION` in `.env` to the same value as `VERSION` before building and starting the services. Compose uses it for image tags and the OCI image version label. Use Git tags in the form `vX.Y.Z` for releases, keeping the tag, `VERSION`, and `.env` value aligned. Retain each release's matching source commit and resolver/sync image tags together for rollback. The `adult-hub-common` package has its own package version in `common/pyproject.toml`.

Check resolver liveness and readiness separately. Liveness only confirms the
process can answer; readiness also verifies the PostgreSQL connection. The
Prometheus endpoint is available to trusted, non-public-domain scrapers at
`/metrics`.

```sh
curl http://127.0.0.1:8090/admin/health/live
curl http://127.0.0.1:8090/admin/health/ready
```

---

## Networking & Reverse Proxy

Clients (smartphones, TV apps, browsers) must be able to reach the resolver's streaming endpoint directly:

* **`RESOLVER_PUBLIC_URL`**: Set this in `.env` to the public HTTPS URL (e.g. `https://hub.example.com`). STRM files will point to `{RESOLVER_PUBLIC_URL}/admin/play/{id}/stream`.
* **`STREAM_PUBLIC_URL_TELEGRAM`**: Optional. Set only if Telegram streams should use a dedicated hostname. Defaults to `RESOLVER_PUBLIC_URL`.
* **Reverse Proxy Routing**:
  * Forward `/admin*` to the `resolver` service (`http://127.0.0.1:8090`).
  * Forward all other traffic to your `jellyfin` service.
  * *Important*: Using a local LAN IP for `RESOLVER_PUBLIC_URL` will prevent playback on mobile devices outside your home network.

---

## Admin v2 (`/admin/`)

The English Admin v2 interface is the only panel, at `/admin/` and `/admin/v2/`. The retired `/admin/classic/` URL and all former Admin V1 API routes return 404. V2 groups the daily workflow into Home, Library, Sources, Organize, Performers, Jobs, and System. It includes Telegram performer proposal review, metadata overrides, availability checks, durable preview/execute plans, paginated audit findings with selected repair of registered STRM files and NFO sidecars, and Jellyfin refresh requests for import findings. It also provides redacted playback-diagnostics JSON downloads (1, 7, or 30 days; at most 100 reports), backup verification, and scheduled maintenance. Optional Jellyfin client pHash intake is disabled by default on `/admin/api/v2/video-phash/*`. A Jellyfin refresh request is not shown as a completed import; run another audit to verify it.

New schema is applied by the shared migration runner. Deploy compatible resolver and sync-worker builds together before using actions added by v2. Backup restore tests require a separate `BACKUP_RESTORE_DATABASE_URL` with permission to create and drop its disposable database; do not point it at the production database.

---

### Admin access and safeguards

When `ADMIN_WRITE_TOKEN` is configured, enter it in Admin v2 for the current browser session to enable write actions. The deprecated `ADMIN_DUPLICATE_TOKEN` remains a fallback only when `ADMIN_WRITE_TOKEN` is empty. Every management mutation requires a matching browser Origin (scheme, host, and port). `ADMIN_PUBLIC=true` permits access through `PUBLIC_DOMAIN` under the same write policy; `ENABLE_ADMIN=false` disables panel access. If no token is configured, writes are available to clients that pass the Origin and access checks.

`PUBLIC_DOMAIN` is matched by exact hostname. Forwarded host headers may block access but cannot grant write access. Set `RESOLVER_TRUSTED_PROXY_IPS` to the exact proxy peer addresses seen by the resolver so Uvicorn preserves the HTTPS browser scheme. For Caddy on the Docker host, include the resolver network gateway address alongside `127.0.0.1,::1`; determine that address from the deployed Docker network. Restrict resolver port 8090 and management routes to your trusted network. Playback `/admin/play/*`, thumbnails, provider APIs, and health routes have separate access rules.

V2 writes use the durable task queue and operation previews where applicable. Use the Jobs view to inspect, retry, cancel, and follow work after reloads or worker restarts. Use Sources for channel configuration/history and per-source sync, Organize for duplicate/person review and quarantine, and System for backups, diagnostics, schedules, and maintenance. All Admin V1 routes are retired; shared resolver/catalog endpoints remain for their non-panel consumers. Do not use legacy Admin API examples or paths.

### Main workflows

- **Sources:** inspect configured Telegram channels and sync history, check access, and queue a global or per-source sync. Disabled channels and incomplete history are shown as such rather than treated as successful.
- **Library and Quality:** search the catalog, review item details and availability, edit metadata with a before/after preview, and inspect NFO/catalog audit findings. Selected repair actions are limited to registered local files and show their result in Jobs.
- **Organize and Performers:** review same-file, strong, and possible duplicate matches; inspect merge effects before confirming; dismiss or hide with history/undo where supported; review fuzzy performer matches before queueing a merge. Quarantine moves registered local sidecars and keeps a restorable decision; it does not delete Telegram media.
- **System:** verify backup archives, queue a backup, review playback/link-check diagnostics, and manage maintenance schedules. A Jellyfin refresh means the request was accepted, not that the import completed; rerun the relevant audit to verify the outcome.

### Repairing Telegram Metadata

Telegram metadata uses `<lockdata>true</lockdata>` and local artwork instead of a remote movie `<thumb>`. Sync rewrites sidecars for the items it processes; use the migration tool to update all existing Telegram sidecars. First inspect its dry run from the repository root, then pause sync before applying. The tool preserves a per-file `.nfo.before-telegram-thumb.bak` backup and writes changes atomically. Substitute your media directory if it differs from `media/adult`:

```sh
python3 tools/migrate_telegram_nfo.py media/adult
docker compose stop sync
python3 tools/migrate_telegram_nfo.py media/adult --apply
docker compose start sync
```

In the Jellyfin library settings, disable the **NFO metadata saver** while keeping the NFO reader enabled. Sync owns these sidecars, and Jellyfin mounts the media directory read-only. Explicitly configure `MetadataSavers: []` for this library; `SaveLocalMetadata: false` alone does not reliably disable the default NFO saver. Existing readers and online providers can remain configured: the local NFO lock protects Telegram items from online matching. Stream filenames and Jellyfin item IDs stay stable.

Normal sync imports changed Telegram NFO through the Jellyfin API serially in batches of up to 200 items. It temporarily unlocks an existing item, requests a local metadata refresh, and checks the result before recording the NFO content hash in `/data/telegram-metadata-state.json`. This also handles subsequent cast/tag changes on locked items. Failed imports remain eligible for retry; a blocked refresh stops new jobs and attempts to restore the item's lock. Processes sharing the state file use a file lock, so a simultaneous maintenance or CLI writer skips its run. Display names and original titles are reconciled through the Jellyfin item API. Runtime sync is disabled because Jellyfin derives movie runtime from media probing and its item update API does not support setting it. The sync service has no writable mount to Jellyfin's private SQLite data.

To inspect remaining work after NFO migration, run the read-only audit inside the sync container. Add `--apply` to import, `--item-id ID` to target a specific Jellyfin item, or `--limit 0` to process all pending imports. The CLI uses the container's configured Jellyfin URL and API key:

```sh
docker compose exec sync python3 /app/jellyfin_db.py \
  --state-path /data/telegram-metadata-state.json
docker compose exec sync python3 /app/jellyfin_db.py \
  --state-path /data/telegram-metadata-state.json --apply --limit 200
```

For an initial library repair, `--adopt-matching` records hashes for already locked items whose titles, descriptions, cast, tags, genres, studios, runtime, and provider IDs match the NFO, avoiding unnecessary refreshes. `--workers 4` allows up to four imports at a time; `--poll-timeout 120` allows each queued import more time to finish. Preview the scope without `--apply`, then import:

```sh
docker compose exec sync python3 /app/jellyfin_db.py \
  --adopt-matching --workers 4 --poll-timeout 120 --limit 0
docker compose exec sync python3 /app/jellyfin_db.py \
  --adopt-matching --workers 4 --poll-timeout 120 --limit 0 --apply
```

The preview endpoint bounds Telegram work to 8 seconds and returns HTTP 504 on timeout, with a separate 10-second endpoint limit. Preview bytes are cached for one hour, capped at 256 entries and 32 MiB; failed previews are cached for 30 seconds. Concurrent requests for one preview share a download, and at most four previews fetch upstream simultaneously. These caches reset when the resolver restarts.

### Playback Diagnostics & Stream Tuning
- Diagnostics can be toggled on-demand for up to 24 hours to troubleshoot playback or buffering issues.
- Reports record Telegram iterator wait times, client disconnect events, and HTTP response throughput.

### Bounded Parallel Telegram Streaming (v1, experimental, default OFF)
- Feature flag `TELEGRAM_PARALLEL_STREAM_ENABLED` (default: `false`). Rollback: set to `false`/unset and restart the resolver; playback returns to the sequential path. No disk cache is added and production must stay disabled until measurements exist.
- Tuning (resolver env, defaults in parentheses): `TELEGRAM_DOWNLOAD_WORKERS` (3) per-stream fetch tasks, `TELEGRAM_DOWNLOAD_GLOBAL_WORKERS` (6) process-wide cap, `TELEGRAM_DOWNLOAD_BLOCK_BYTES` (524288) fetch block size, `TELEGRAM_READ_AHEAD_BYTES` (33554432) read-ahead window behind the HTTP receiver cursor, `TELEGRAM_STREAM_CACHE_BYTES` (134217728) in-memory block cache cap, `TELEGRAM_STREAM_CACHE_TTL_SECONDS` (120) block TTL.
- Semantics: the read-ahead byte cursor is the HTTP receiver position, not the actual player buffer; overlapping readers of the same object share fetched blocks; demand (the next unread byte) wins over stale read-ahead.
- Concurrency caveat: concurrent fetch tasks do NOT guarantee independent MTProto connections; Telegram may still serialize or throttle upstream.
- Enablement gate: run real-movie A/B baselines at 1/2/4 concurrency (stalls, seek latency, upstream errors) before enabling anywhere near production.

---

## Sources

The system is Telegram-only. Configure your channels in `config.yaml` under `sources`; any non-Telegram provider fails fast at startup.

---

## Development & Testing

From the repository root, install the development dependencies, run the linter, and run the complete test suite:

```sh
make install-dev
make lint
make test
```

`resolver/requirements.lock` and `sync/requirements.lock` pin direct and
transitive runtime dependencies. Docker and CI install those lockfiles;
`requirements-dev.lock` combines them with the pinned test/lint tools. After
changing dependency inputs, run `make lock` with `uv` installed and commit the
updated lockfiles.

The shared `make` targets also run browser contracts, Admin v2 JavaScript
syntax checks, and the proxy-origin contract. `make test` requires a separate
PostgreSQL database through `TEST_DATABASE_URL` (from the shell or `.env`) and
fails before discovery if it is missing. It sets the full test profile and the
Telegram-only fixture used by CI:

Ruff is pinned in `requirements-dev.txt` and configured in the root
`pyproject.toml`. Its initial rule set checks common import and correctness
issues. The first baseline run reported 331 diagnostics (203 auto-fixable); the
reported issues were corrected or handled with narrow ignores, and lint now
runs in CI. Run an individual test layer with `make python-test`,
`make ui-contracts`, `make js-check`, or `make proxy-origin`. `make help` lists
the available targets.

Most database-backed tests require a disposable PostgreSQL database. Set `TEST_DATABASE_URL` to a separate database (never your production `DATABASE_URL`); for local runs the runner also reads `TEST_DATABASE_URL` from the repository `.env`. Without it, every test that touches the database is skipped, so the suite can pass while exercising only a fraction of the code.

The test suite needs the resolver and sync dependencies installed. If an import is missing, the tests fail during startup so the setup issue is visible. Set `ADULT_HUB_TEST_PROFILE=partial` only when you intentionally run the tests in an environment that leaves out one of those components; tests for the missing component will be skipped.

### Maintenance scripts

The one-off tools under `scripts/` are grouped by their effect:

- `admin_classic_usage_report.py` reads Uvicorn access logs and reports use of retired Classic routes; it does not change application data.
- `rebuild_telegram_performers.py` reports performer matches by default. `--apply` writes exact matches, `--queue-proposals` queues uncertain matches, and `--rollback RUN_ID` restores a prior applied run.
- `sync_performer_catalog.py` refreshes the local StashDB performer catalogue and requires `STASHDB_API_KEY` plus the configured database.
- `cleanup_nationality_prefixes.py`, `cleanup_performer_data.py`, `cleanup_scene_suffixes.py`, `cleanup_studios_and_tags.py`, and `reparse_telegram_performers.py` update PostgreSQL and/or NFO files directly. They are one-off repair tools and do not expose a dry-run flag; review the script and make a database backup before invoking one.
- `sync_jellyfin_db.py` and `sync_jellyfin_studios_and_tags.py` are retired entrypoints. They exit with instructions to use the regular sync/API flow and Jellyfin's library refresh.
- `tools/export_video_duplicate_review_corpus.py` samples current duplicate suggestions, active hide decisions, and active dismissals for re-review. The CSV leaves `same_scene` blank, records the source of each pair, and does not alter decisions or rules. Keep the output local because it contains titles and performer names:
  ```sh
  docker compose exec resolver python /app/tools/export_video_duplicate_review_corpus.py \
    --output /data/reports/video-duplicate-review.csv --per-stratum 25
  ```
- To evaluate a duplicate review CSV from the optional command-line workflow, fill `same_scene` with Yes/No and use the downloaded reviewed CSV on the host:
  ```sh
  python3 tools/evaluate_review_corpus.py --kind duplicates data/reports/video-duplicate-review-reviewed.csv
  ```
  The output separates current candidate errors from historical hide/dismissal reviews and states its sample scope; it is not a catalog-wide precision/recall estimate.
- `jellyfin-ffmpeg.sh` and `jellyfin-ffprobe.sh` are executable wrappers mounted by Compose for Jellyfin; they are not maintenance commands.

### Database Operations & Recreation
- **Versioned migrations**: Schema changes are managed as ordered, versioned migrations in `common/migrations/` tracked by the `schema_migrations` table.
  ```sh
  # Inspect migration status
  python3 migrate_db.py status

  # Apply pending migrations
  python3 migrate_db.py apply

  # Recreate schema from scratch (wipes tables and reapplies all migrations)
  python3 migrate_db.py recreate --yes
  ```
- **Backup & Restore**:
  ```sh
  # Create and verify a PostgreSQL backup, then apply monthly history retention
  docker compose exec -T sync python backup_scheduler.py --once
  ```
  The scheduler supports checksum verification, archive dry-run, and restore verification; configure host scheduling separately.

### Architecture Highlights
- `common/`: Shared configuration loader and validation logic used across both containers, the PostgreSQL layer (`db.py`, a psycopg 3 wrapper over `DATABASE_URL`), versioned database migrations (`common/migrations/`, `migrate_db.py`, `restore_db.py`), plus `job_queue.py` (durable task queue) and `managed_collections.py`.
- `resolver/providers/`: Provider registry (Telegram-only).
- `sync/sync.py`: Core synchronizer handling PostgreSQL records, NFO/STRM generation, retention enforcement, and custom sync hooks. `sync/runner.py` owns scheduled/on-demand syncs and the durable `sync` jobs.
- Admin UI and management API are V2 only: `resolver/admin.py` serves the UI at `/admin/` and `/admin/v2/`, and mounts the `admin_v2_*.py` feature routers. Classic UI and Admin V1 routes have been retired. Shared resolver/catalog APIs remain on their established non-admin routes; the pHash client integration is versioned under `admin_v2_phash.py`. Data access and formatting live in `admin_data.py`; `admin_services.py` and `admin_security.py` hold shared job and access logic.
- Provider IDs and media filenames remain strictly stable and isolated.
