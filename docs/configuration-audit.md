# Adult Hub Configuration Audit

## 1. Executive Summary

This audit establishes a complete, rigorous inventory of the Adult Hub configuration surface across all services (`resolver`, `sync`, `backup`, `database`, `migration`), CLI tools, setup scripts, Docker Compose manifests, and existing configuration loaders.

### Baseline Metrics (Before Refactoring)

| Metric | Measured Baseline |
| :--- | :--- |
| Active variables in `.env.example` | 72 |
| Total entries in `.env.example` (active + commented) | 92 |
| Lines in `.env.example` | 192 lines |
| Lines in `docker-compose.yml` | 244 lines |
| Environment passes in `docker-compose.yml` | 74 variables |
| Lines in `compose.standalone.yml` | 284 lines |
| Environment passes in `compose.standalone.yml` | 84 variables |
| `config.yaml` requirement | **Strictly required** (missing file crashes `sync` with `SystemExit`) |
| Lines in `config.example.yaml` | 42 lines |

### Key Findings
1. **Redundant Compose Pass-Throughs**: Over 40 advanced internal knobs (e.g., watchdog timing, download block sizes, cache TTLs, backfill quotas) are duplicated across both services in `docker-compose.yml` with repeated default expressions (`${VAR:-default}`), obscuring the essential deployment parameters.
2. **Conflicting Defaults**: Several variables define different defaults in Python source code versus Compose manifests versus `.env.example`.
3. **Hard Dependency on `config.yaml`**: `common/config_loader.py` and `sync/sync_settings.py` raise fatal errors if `config.yaml` does not exist on disk, even though sources can be managed entirely through the Admin UI via `admin-source-overrides.json`.
4. **Disjoint Standalone Naming**: `compose.standalone.yml` introduced a parallel namespace (`STANDALONE_DATABASE_URL`, `STANDALONE_DATA_DIR`, `STANDALONE_MEDIA_DIR`, etc.) that diverges from the standard deployment environment variables, increasing maintenance overhead and confusion.
5. **Legacy Aliases**: Multiple legacy names exist in active code paths (`ADMIN_DUPLICATE_TOKEN`, `STASH_API_KEY`, `THEPORNDB_API_KEY` vs `THEPORNDB_API_TOKEN`, `TELEGRAM_CHANNELS`, `LUKAYAN_*`) that must be preserved for backward compatibility while canonicalizing the primary names.

---

## 2. Complete Configuration Inventory

The following table inventories every configuration parameter across the Adult Hub repository.

### Columns
- **Name**: Configuration key name
- **Consumer(s)**: Services and tools reading this setting (`resolver`, `sync`, `backup`, `setup`, `compose`, `admin`, `tests`)
- **Required**: `Yes` (mandatory for startup), `No` (optional with code default), or `Conditional` (required only if specific feature is enabled)
- **Actual Default**: Values in Python code vs. Compose vs. `.env.example`
- **Sensitive**: `Yes` (secret/credential, must be redacted) or `No`
- **Mutable**: `Runtime` (mutable via Admin UI/DB/hot reload) vs. `Restart` (requires container restart)
- **Persistence**: Storage mechanism (`.env`, `config.yaml`, `admin-source-overrides.json`, DB, `Compose`)
- **Action**: Architectural recommendation (`Keep Core`, `Demote to Advanced`, `Centralize Default`, `Deprecate Alias`, `Make Optional`)
- **Migration**: Migration path or legacy fallback handling

| Name | Consumer(s) | Required | Actual Default | Sensitive | Mutable | Persistence | Action | Migration |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `DATABASE_URL` | resolver, sync, backup, migrate, tools | Yes | `None` (code raises RuntimeError; compose passes `${DATABASE_URL:-}`) | Yes | Restart | `.env` / Env | Keep Core | Retain canonical connection URL |
| `POSTGRES_DB` | standalone database | Conditional | `adulthub` in setup.sh; required in standalone compose | No | Restart | `.env` / Compose | Keep Core (standalone) | Used by standalone postgres container |
| `POSTGRES_USER` | standalone database | Conditional | `adulthub` in setup.sh; required in standalone compose | No | Restart | `.env` / Compose | Keep Core (standalone) | Used by standalone postgres container |
| `POSTGRES_PASSWORD` | standalone database | Conditional | Generated in setup.sh; required in standalone compose | Yes | Restart | `.env` / Compose | Keep Core (standalone) | Used by standalone postgres container |
| `STANDALONE_DATABASE_URL` | standalone compose | No | `postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@database:5432/${POSTGRES_DB}` | Yes | Restart | `.env` / Compose | Demote / Alias | Fallback to `DATABASE_URL` |
| `BACKUP_RESTORE_DATABASE_URL` | resolver, sync | No | Code: `""` / `None`; Compose: `""` | Yes | Restart | `.env` / Env | Demote to Advanced | Preserved for test restore targets |
| `TEST_DATABASE_URL` | test suite, tools | Conditional | Code: `""` (skips DB tests unless required) | Yes | Restart | `.env` / Env | Keep (Dev/Test) | Used strictly for tests, never production |
| `HOST_IP` | compose (Jellyfin published URL) | No | Compose: `${HOST_IP:-127.0.0.1}` | No | Restart | `.env` / Compose | Keep Core | Retain for LAN access |
| `RESOLVER_PUBLIC_URL` | resolver, sync | No | Code: `http://127.0.0.1:8090`; Compose: `${RESOLVER_PUBLIC_URL:-http://127.0.0.1:8090}` | No | Restart | `.env` / Env | Keep Core | Public URL for stream links in .strm |
| `STREAM_PUBLIC_URL_TELEGRAM` | sync | No | Code: `""` (falls back to `RESOLVER_PUBLIC_URL`) | No | Restart | `.env` / Env | Demote to Advanced | Optional Telegram-specific stream origin |
| `RESOLVER_API_URL` | sync | No | Code: `http://localhost:8090`; Compose: `http://resolver:8090` | No | Restart | Compose / Env | Centralize Default | Sane default `http://resolver:8090` in containers |
| `MEDIA_PATH` | compose (host volume) | No | Compose: `./media/adult` | No | Restart | `.env` / Compose | Keep Core | Host path mounted to `/media/adult` |
| `MEDIA_DIR` | resolver, sync, backup | No | Code: `/media/adult` (or `./media/adult`) | No | Restart | Env / Compose | Centralize Default | Container path `/media/adult` |
| `DATA_DIR` | resolver, sync, backup | No | Code: `/data` (or `./data`); Compose: `/data` | No | Restart | Env / Compose | Centralize Default | Container path `/data` |
| `CACHE_DIR` | resolver, sync | No | Code: `/cache`; Compose: `/data/cache` | No | Restart | Env / Compose | Centralize Default | Container path `/cache` |
| `CONFIG_PATH` | resolver, sync, loader | No | Code: `/config/config.yaml`; Compose: `/config/config.yaml` | No | Restart | Env / Compose | Make Optional | If missing, use empty config with defaults |
| `ADMIN_SOURCE_OVERRIDES_PATH` | resolver, sync, loader | No | Code: `/data/admin-source-overrides.json` | No | Restart | Env / Compose | Centralize Default | Retain default `/data/admin-source-overrides.json` |
| `ADMIN_WORKER_CAPABILITIES_PATH` | resolver, sync | No | Code: `/data/cache/worker-capabilities.json` | No | Restart | Env / Compose | Centralize Default | Retain default `/data/cache/worker-capabilities.json` |
| `ENABLE_ADMIN` | resolver | No | Code: `true`; Compose: `true` | No | Restart | `.env` / Env | Demote to Advanced | Default `true` in code |
| `ADMIN_PUBLIC` | resolver | No | Code: `false`; Compose: `false` | No | Restart | `.env` / Env | Keep Core | Critical security gate for public proxy |
| `ADMIN_WRITE_TOKEN` | resolver, setup | No | Code: `""`; Setup: generates token | Yes | Restart | `.env` / Env | Keep Core | Primary token for admin write mutations |
| `ADMIN_DUPLICATE_TOKEN` | resolver | No | Code: `""` | Yes | Restart | `.env` / Env | Deprecate Alias | Fallback to `ADMIN_WRITE_TOKEN` |
| `PUBLIC_DOMAIN` | resolver | No | Code: `media.example.org`; Compose: `media.example.org` | No | Restart | `.env` / Env | Keep Core | Exact public domain for origin check |
| `RESOLVER_TRUSTED_PROXY_IPS` | compose (FORWARDED_ALLOW_IPS) | No | Compose: `127.0.0.1,::1` | No | Restart | `.env` / Compose | Demote to Advanced | Maps to `FORWARDED_ALLOW_IPS` |
| `TELEGRAM_API_ID` | resolver, sync, auth | Conditional | Code: `""` (RuntimeError if empty when needed) | Yes | Restart | `.env` / Env | Keep Core | Required for Telegram provider |
| `TELEGRAM_API_HASH` | resolver, sync, auth | Conditional | Code: `""` (RuntimeError if empty when needed) | Yes | Restart | `.env` / Env | Keep Core | Required for Telegram provider |
| `TELEGRAM_SESSION_PATH` | resolver, sync, auth | No | Code: `/data/telegram.session`; Compose: `/cache/telegram.session` or `/data/telegram.session` | No | Restart | Env / Compose | Centralize Default | Session file location |
| `TELEGRAM_PARALLEL_STREAM_ENABLED` | resolver | No | Code: `false`; Compose: `false` | No | Restart | Env | Demote to Advanced | Default `false` |
| `TELEGRAM_DOWNLOAD_WORKERS` | resolver | No | Code: `3`; Compose: `3` | No | Restart | Env | Demote to Advanced | Default `3` |
| `TELEGRAM_DOWNLOAD_GLOBAL_WORKERS` | resolver | No | Code: `6`; Compose: `6` | No | Restart | Env | Demote to Advanced | Default `6` |
| `TELEGRAM_DOWNLOAD_BLOCK_BYTES` | resolver | No | Code: `524288` (512KB); Compose: `524288` | No | Restart | Env | Demote to Advanced | Default 512KB |
| `TELEGRAM_READ_AHEAD_BYTES` | resolver | No | Code: `33554432` (32MB); Compose: `33554432` | No | Restart | Env | Demote to Advanced | Default 32MB |
| `TELEGRAM_STREAM_CACHE_BYTES` | resolver | No | Code: `134217728` (128MB); Compose: `134217728` | No | Restart | Env | Demote to Advanced | Default 128MB |
| `TELEGRAM_STREAM_CACHE_TTL_SECONDS` | resolver | No | Code: `120`; Compose: `120` | No | Restart | Env | Demote to Advanced | Default 120s |
| `TELEGRAM_SIGNATURE_CHANNEL` | common, resolver, sync | No | Code: `None`; Compose: `""` | No | Restart | Env | Demote to Advanced | Channel signature cast detection |
| `TELEGRAM_CHANNEL_NAME_HINTS` | sync | No | Code: `None`; Compose: `""` | No | Restart | Env | Demote to Advanced | Hints for channel titles |
| `TELEGRAM_AVAILABILITY_CHECKS_PER_RUN` | sync | No | Code: `20`; Compose: `20` | No | Restart | Env | Demote to Advanced | Maximum availability probes |
| `TELEGRAM_FINGERPRINTS_ENABLED` | sync | No | Code: `True`; Compose: `true` | No | Restart | Env | Demote to Advanced | Enables OSHASH / pHash backfill |
| `TELEGRAM_OSHASH_BACKFILL_PER_RUN` | sync | No | Code: `200`; Compose: `200` | No | Restart | Env | Demote to Advanced | Max OSHASH backfills per sync |
| `TELEGRAM_PHASH_ENABLED` | sync | No | Code: `True`; Compose: `true` | No | Restart | Env | Demote to Advanced | Enables pHash calculation |
| `TELEGRAM_PHASH_BYTE_BUDGET` | sync | No | Code: `268435456` (256MB); Compose: `268435456` | No | Restart | Env | Demote to Advanced | pHash chunk budget |
| `TELEGRAM_PHASH_TIMEOUT_SECONDS` | sync | No | Code: `1200` (20m); Compose: `1200` | No | Restart | Env | Demote to Advanced | pHash timeout |
| `TELEGRAM_FINGERPRINT_DAILY_BYTE_BUDGET` | common, sync | No | Code: `30000000000` (30GB); Compose: `30GB`; .env.ex: `10GB` | No | Restart | Env | Centralize Default | Standardize on 30GB code default |
| `TELEGRAM_FINGERPRINT_MAX_BYTES_PER_SECOND` | common, sync | No | Code: `2097152` (2MB/s); Compose: `2097152` | No | Restart | Env | Demote to Advanced | Rate limit for fingerprinting |
| `TELEGRAM_FINGERPRINT_USAGE_PATH` | common, sync | No | Code: `/data/cache/telegram-fingerprint-daily-usage.json` | No | Restart | Env | Centralize Default | Fingerprint usage ledger file |
| `TELEGRAM_CHANNELS` | resolver, sync | No | Code: `""` | No | Restart | Env | Deprecate Alias | Legacy fallback when config.yaml missing |
| `TELEGRAM_MAX_HISTORY` | resolver | No | Code: `1` | No | Restart | Env | Deprecate Alias | Legacy fallback |
| `TELEGRAM_MIN_DURATION_SECONDS` | resolver | No | Code: `420` | No | Restart | Env | Deprecate Alias | Legacy fallback |
| `JELLYFIN_URL` | resolver, sync, tools | No | Code: `""`; Compose: `${JELLYFIN_URL:-}` | No | Restart | `.env` / Env | Keep Core | URL to Jellyfin instance |
| `JELLYFIN_API_KEY` | resolver, sync, tools | No | Code: `""`; Compose: `${JELLYFIN_API_KEY:-}` | Yes | Restart | `.env` / Env | Keep Core | Jellyfin API token |
| `JELLYFIN_FFMPEG_FAST_PROBE` | compose (jellyfin env) | No | Compose: `true` | No | Restart | Env / Compose | Centralize Default | Jellyfin container probe optimization |
| `JELLYFIN_FFMPEG_ANALYZE_DURATION` | compose (jellyfin env) | No | Compose: `5M` | No | Restart | Env / Compose | Centralize Default | Jellyfin container analyze duration |
| `JELLYFIN_FFMPEG_PROBE_SIZE` | compose (jellyfin env) | No | Compose: `10M` | No | Restart | Env / Compose | Centralize Default | Jellyfin container probe size |
| `STASHDB_API_KEY` | sync, resolver | No | Code: `""`; Compose: `${STASHDB_API_KEY:-}` | Yes | Restart | `.env` / Env | Keep Core | StashDB GraphQL API key |
| `STASH_API_KEY` | sync, resolver | No | Code: `""`; Compose: `${STASH_API_KEY:-}` | Yes | Restart | `.env` / Env | Deprecate Alias | Legacy alias for `STASHDB_API_KEY` |
| `STASHDB_API_URL` | sync, resolver | No | Code: `https://stashdb.org/graphql` | No | Restart | Env | Demote to Advanced | Standard StashDB endpoint |
| `THEPORNDB_API_TOKEN` | sync | No | Code: `""`; Compose: `${THEPORNDB_API_TOKEN:-}` | Yes | Restart | `.env` / Env | Keep Core | ThePornDB API token |
| `THEPORNDB_API_KEY` | sync | No | Code: `""`; Compose: `${THEPORNDB_API_KEY:-}` | Yes | Restart | `.env` / Env | Deprecate Alias | Alternative name for `THEPORNDB_API_TOKEN` |
| `LOG_LEVEL` | resolver, sync, backup | No | Code: `INFO`; Compose: `${LOG_LEVEL:-INFO}` | No | Restart | `.env` / Env | Keep Core | Logging verbosity |
| `TZ` | resolver, sync, backup | No | Compose: `${TZ:-Europe/Warsaw}` | No | Restart | `.env` / Compose | Keep Core | Timezone |
| `APP_VERSION` | resolver, sync, backup | No | Code: `0+unknown` (reads VERSION file) | No | Restart | Env / File | Keep Core | Auto-extracted from `VERSION` |
| `RESOLVER_CONCURRENCY_LIMIT` | resolver | No | Code: `10`; Compose: `10` | No | Restart | Env | Demote to Advanced | Concurrency semaphore limit |
| `CACHE_TTL_SECONDS` | resolver | No | Code: `1500`; Compose: `1500` | No | Restart | Env | Demote to Advanced | Resolver URL cache TTL |
| `MAX_CACHE_ENTRIES` | resolver | No | Code: `2000`; Compose: `2000` | No | Restart | Env | Demote to Advanced | Resolver LRU cache entry limit |
| `HIDE_DEAD_STREAMS` | sync | No | Code: `true`; Compose: `true` | No | Restart | Env | Demote to Advanced | Filter unavailable stream links |
| `DUPLICATE_AUTO_MERGE_ENABLED` | sync | No | Code: `false`; Compose: `false` | No | Restart | Env | Demote to Advanced | Auto-merge duplicate videos |
| `DUPLICATE_AUTO_MERGE_TELEGRAM_FILE_ID` | sync | No | Code: `true`; Compose: `true` | No | Restart | Env | Demote to Advanced | Auto-merge identical Telegram files |
| `VIDEO_VISUAL_WORKER_ENABLED` | resolver | No | Code: `false`; Compose: `false` | No | Restart | Env | Demote to Advanced | Background visual duplicate worker |
| `PHASH_UPLOAD_ENABLED` | resolver | No | Code: `false`; Compose: `false` | No | Restart | Env | Demote to Advanced | Accept client-submitted pHashes |
| `PHASH_ACCEPTED_DECODER_PROFILES` | resolver | No | Code: `""`; Compose: `""` | No | Restart | Env | Demote to Advanced | Decoder profiles |
| `PHASH_ALLOWED_USER_IDS` | resolver | No | Code: `""`; Compose: `""` | No | Restart | Env | Demote to Advanced | Non-admin Jellyfin user whitelist |
| `PHASH_SERVER_BYTE_BUDGET` | resolver | No | Code: `268435456` (256MB) | No | Restart | Env | Demote to Advanced | Server-side pHash byte limit |
| `PHASH_SERVER_TIMEOUT_SECONDS` | resolver | No | Code: `1200` (20m) | No | Restart | Env | Demote to Advanced | Server-side pHash timeout |
| `PHASH_SERVER_BYTES_PER_SECOND` | resolver | No | Code: `2097152` (2MB/s) | No | Restart | Env | Demote to Advanced | Server-side pHash stream rate |
| `PHASH_SERVER_IDLE_BYTES_PER_SECOND` | resolver | No | Code: `10000000` (10MB/s) | No | Restart | Env | Demote to Advanced | Server-side idle bandwidth |
| `RESOLVER_WATCHDOG_INTERVAL_SECONDS` | resolver supervisor | No | Code: `10`; Compose: `10` | No | Restart | Env | Demote to Advanced | Supervisor check interval |
| `RESOLVER_WATCHDOG_TIMEOUT_SECONDS` | resolver supervisor | No | Code: `4`; Compose: `4` | No | Restart | Env | Demote to Advanced | Supervisor check timeout |
| `RESOLVER_WATCHDOG_FAILURES` | resolver supervisor | No | Code: `3`; Compose: `12` | No | Restart | Env | Centralize Default | Sane default 12 in code |
| `RESOLVER_WATCHDOG_STARTUP_GRACE_SECONDS`| resolver supervisor | No | Code: `45`; Compose: `45` | No | Restart | Env | Demote to Advanced | Supervisor startup delay |
| `BACKUP_DIR` | backup, resolver, sync | No | Code: `./data/backups` or `/data/backups` | No | Restart | Env / Compose | Centralize Default | Backup storage path |
| `BACKUP_KEEP_DAYS` | backup | No | Code: `7`; Compose: `7` | No | Restart | Env / Compose | Centralize Default | Retention days for backup archives |
| `BACKUP_DAILY_AT` | backup, sync | No | Code: `04:30`; Compose: `04:30` | No | Restart | Env / Compose | Centralize Default | Daily backup schedule time |
| `BACKUP_LOCK_PATH` | backup | No | Code: `/data/cache/backup.lock` | No | Restart | Env / Compose | Centralize Default | Concurrency lock file |
| `ADMIN_V2_MONITOR_BACKUPS` | resolver issues | No | Code: `true` in issue builder; `false` in Compose/admin.py | No | Restart | Env | Centralize Default | Align default to `false` (only explicit) |
| `ADMIN_V2_QUEUE_WARNING_SECONDS` | resolver issues | No | Code: `3600` (1 hour) | No | Restart | Env | Demote to Advanced | Queue delay warning threshold |
| `ADMIN_V2_BACKUP_WARNING_SECONDS` | resolver issues | No | Code: `129600` (36 hours) | No | Restart | Env | Demote to Advanced | Backup age warning threshold |
| `LINKCHECK_LOG_PATH` | resolver, sync | No | Code: `/data/linkcheck.log` | No | Restart | Env / Compose | Centralize Default | Link check report log path |
| `LUKAYAN_*` | sync backfill | No | Legacy aliases for `TELEGRAM_*` | No | Restart | Code | Deprecate Alias | Internal legacy compatibility only |
| `STANDALONE_*` | standalone installer/compose | No | Standalone path/port overrides | No | Restart | `.env` / Compose | Standardize | Map to standard vars with fallback |

---

## 3. Duplicated, Conflicting, Legacy, and Unused Settings

### 3.1 Conflicting Defaults Discovered

1. **`RESOLVER_WATCHDOG_FAILURES`**:
   - `resolver/supervisor.py`: Defaults to `3`.
   - `docker-compose.yml` & `compose.standalone.yml`: Defaults to `12`.
   - `.env.example`: Defaults to `12`.
   - *Resolution*: Centralize default to `12` in code so Compose does not need to force it.

2. **`TELEGRAM_FINGERPRINT_DAILY_BYTE_BUDGET`**:
   - `common/telegram_transfer_budget.py`: Defaults to `30_000_000_000` (30 GB).
   - `docker-compose.yml`: Defaults to `30000000000` (30 GB).
   - `.env.example`: Listed as `10000000000` (10 GB).
   - *Resolution*: Standardize on 30 GB across all documentation and references.

3. **`BACKUP_DIR`**:
   - `common/backup.py`: Defaults to `./data/backups`.
   - `backup_scheduler.py`, `resolver/admin.py`, Compose: Defaults to `/data/backups`.
   - *Resolution*: Centralize default to `/data/backups` (with fallback to `./data/backups` if `/data` is unwritable).

4. **`JELLYFIN_PEOPLE_PER_RUN`**:
   - `common/config_loader.py`: Defaults to `50`.
   - `config.example.yaml`: Defaults to `25`.
   - *Resolution*: Centralize on 25 in code defaults matching the recommended conservative pacing.

5. **`ADMIN_V2_MONITOR_BACKUPS`**:
   - `resolver/admin_v2_issue_builder.py:284`: Code fallback is `"true"`.
   - `resolver/admin.py`, `docker-compose.yml`, `.env.example`: Default is `"false"`.
   - *Resolution*: Centralize on `"false"` so unmonitored deployments do not produce false positive alarms.

### 3.2 Legacy Compatibility Aliases to Retain

| Canonical Setting | Accepted Legacy Alias | Location Handled | Action |
| :--- | :--- | :--- | :--- |
| `ADMIN_WRITE_TOKEN` | `ADMIN_DUPLICATE_TOKEN` | `resolver/admin.py`, `common/config.py` | Retain fallback: `ADMIN_WRITE_TOKEN or ADMIN_DUPLICATE_TOKEN or ""` |
| `STASHDB_API_KEY` | `STASH_API_KEY` | `sync/stashdb.py`, `resolver/admin_v2_phash.py` | Retain fallback: `STASHDB_API_KEY or STASH_API_KEY or ""` |
| `THEPORNDB_API_TOKEN` | `THEPORNDB_API_KEY` | `sync/theporndb.py`, `sync/scene_scraper_*.py` | Retain fallback: `THEPORNDB_API_TOKEN or THEPORNDB_API_KEY or ""` |
| `TELEGRAM_` (fingerprints) | `LUKAYAN_` (fingerprints) | `sync/scene_scraper_fingerprint_backfill.py` | Retain fallback for legacy environment migrations |
| `DATABASE_URL` | `STANDALONE_DATABASE_URL` | `common/config.py`, Compose | Accept `STANDALONE_DATABASE_URL` if `DATABASE_URL` is unset |
| Source channels (YAML) | `TELEGRAM_CHANNELS` (CSV) | `resolver/providers/telegram_config.py` | Retain fallback when no YAML or managed overlay channels exist |

### 3.3 Unused or Dead Settings

1. **`ADMIN_BASE_URL`**: Only referenced in `tools/admin_v2_release_smoke.py:106` as a CLI override; never used by core application services.
2. **`DB_PATH`**: SQLite legacy variable only found in `tests/pg_helper.py` (which explicitly enforces PostgreSQL). Removed from production scope.
3. **`SYNC_TELEGRAM`**: Evaluated dynamically from `CONFIG.get_source("telegram").enabled`. Setting `SYNC_TELEGRAM` as an env var has no effect on sync.

---

## 4. UI Candidates vs. Infrastructure-Only Settings

### 4.1 Infrastructure-Only (Must remain in `.env` / Compose)
These settings dictate host network topology, filesystem mounts, container images, hardware access, or security boundaries. They **must not** be editable through the Admin UI:
- `DATABASE_URL`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`
- `MEDIA_PATH`, `MEDIA_DIR`, `DATA_DIR`, `CACHE_DIR`
- `HOST_IP`, `RESOLVER_BIND_IP`, `RESOLVER_PORT`, `JELLYFIN_BIND_IP`, `JELLYFIN_PORT`
- `RESOLVER_PUBLIC_URL`, `PUBLIC_DOMAIN`, `ADMIN_PUBLIC`
- `ADMIN_WRITE_TOKEN`
- `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_SESSION_PATH`
- `RESOLVER_IMAGE`, `SYNC_IMAGE`, `POSTGRES_IMAGE`, `JELLYFIN_IMAGE`
- `stop_grace_period`, `mem_limit`, `networks`, `volumes`

### 4.2 UI Candidates (Manageable via Admin UI / Dynamic Overlay)
These settings govern operational policies, integration keys, sync schedules, and maintenance routines. They can be inspected or mutated via the Admin UI without container restarts:
- **Telegram Channels**: Already dynamically managed via `admin-source-overrides.json` (`/admin/api/v2/sources`).
- **Sync Schedules & Intervals**: System schedules are already stored in PostgreSQL (`system_schedules` table) for `library-sync`, `database-backup`, and `artwork-generation`.
- **Deduplication Policies**: Candidate for Admin UI (`DUPLICATE_AUTO_MERGE_ENABLED`, `DUPLICATE_AUTO_MERGE_TELEGRAM_FILE_ID`).
- **Jellyfin Connection & Sync**: Can be configured/tested in Admin UI (`JELLYFIN_URL`, `JELLYFIN_API_KEY`).
- **External Metadata Keys**: `STASHDB_API_KEY`, `THEPORNDB_API_TOKEN`.

---

## 5. Per-Setting Precedence and Lifecycle Implications

### 5.1 Precedence Hierarchy
When a setting can be specified in multiple locations, the effective value follows this explicit hierarchy:

```
┌─────────────────────────────────────────────────────────────┐
│ 1. Admin UI / JSON Overlay (admin-source-overrides.json)     │ <- Highest priority for sources
├─────────────────────────────────────────────────────────────┤
│ 2. Environment Variables (.env / Process os.environ)        │ <- Highest priority for infra & keys
├─────────────────────────────────────────────────────────────┤
│ 3. Database System Tables (system_schedules)                │ <- Priority for task schedules
├─────────────────────────────────────────────────────────────┤
│ 4. config.yaml (Checked-in / Mounted YAML)                  │ <- Base configuration
├─────────────────────────────────────────────────────────────┤
│ 5. Application Code Defaults (common/config.py)             │ <- Built-in fallbacks
└─────────────────────────────────────────────────────────────┘
```

#### Precedence Rules:
1. **Source & Channel Definitions**:
   `admin-source-overrides.json` overlays `config.yaml`. If a channel is disabled or modified in the Admin UI, the override **always** wins over `config.yaml`.
2. **Task Schedules**:
   `system_schedules` table in PostgreSQL controls next run times and enablement. Settings in `config.yaml` (`sync.interval_minutes`, `sync.daily_at`) serve as initial defaults for newly created schedules.
3. **API Keys & Credentials**:
   Environment variables override any config file value. If `STASHDB_API_KEY` is in `.env`, it takes precedence over legacy aliases.
4. **Startup Overwrites**:
   Service startup **must never overwrite** Admin UI values stored in `admin-source-overrides.json` or `system_schedules`.

### 5.2 Lifecycle Implications

1. **Boot-Time Only (Requires Container Restart)**:
   - Database connection (`DATABASE_URL`)
   - Network bindings & ports (`HOST_IP`, `RESOLVER_PORT`, `PUBLIC_DOMAIN`)
   - Volume mounts and directory paths (`MEDIA_PATH`, `DATA_DIR`)
   - MTProto API ID/Hash (`TELEGRAM_API_ID`, `TELEGRAM_API_HASH`)
2. **Dynamically Reloaded on Request**:
   - `common/config_loader.py:load_config()` uses an atomic file signature (`stat.st_mtime_ns`, `stat.st_size`, `stat.st_ino`) to detect changes to `config.yaml` and `admin-source-overrides.json` without process restart.
3. **Graceful Shutdown & Batch Drain**:
   - `sync` runner intercepts `SIGTERM` and completes the in-flight channel before exiting.
   - `config.yaml` defines `sync.shutdown_grace_seconds: 180` (3 minutes).
   - `docker-compose.yml` specifies `stop_grace_period: 4m` (240s).
   - **Crucial**: `stop_grace_period` (4m) must **strictly exceed** `shutdown_grace_seconds` (3m) by at least 60 seconds to prevent Docker from issuing a `SIGKILL` mid-batch. Evidence confirms `stop_grace_period: 4m` is required and must not be reduced.

---

## 6. Recommendations for Configuration Simplification

1. **Create `common/config.py`**:
   - Centralize default constants and environment extraction helpers in `common/config.py` without introducing a second framework.
   - Preserve backward-compatible imports in `sync/sync_settings.py`, `resolver/admin.py`, and `common/config_loader.py`.
2. **Make `config.yaml` Optional**:
   - Modify `common/config_loader.py` so that if `config.yaml` does not exist, it falls back to a clean default `AppConfig` (with standard sync settings and empty sources).
   - If `admin-source-overrides.json` exists, load sources directly from it.
   - Do not require users to create or mount a dummy `config.yaml` on initial install.
3. **Slim Down `.env.example`**:
   - Reduce `.env.example` from 92 entries down to ~15 essential settings:
     `DATABASE_URL`, `RESOLVER_PUBLIC_URL`, `MEDIA_PATH`, `HOST_IP`, `PUBLIC_DOMAIN`, `ADMIN_WRITE_TOKEN`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `JELLYFIN_URL`, `JELLYFIN_API_KEY`, `STASHDB_API_KEY`, `THEPORNDB_API_TOKEN`, `LOG_LEVEL`, `TZ`.
   - Document all advanced overrides in `docs/advanced-configuration.md`.
4. **Streamline `docker-compose.yml`**:
   - Remove redundant `${VAR:-default}` environment mappings where the code already defaults correctly.
   - Cleanly isolate external service integration (PostgreSQL, Jellyfin, Caddy/Reverse Proxy) vs. standalone container definitions.
5. **Harmonize Standalone & Production Environments**:
   - Support standard variable names (`DATABASE_URL`, `MEDIA_PATH`) in standalone mode while preserving fallback support for `STANDALONE_*` variables.
