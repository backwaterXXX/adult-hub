# Adult Hub Advanced Configuration Reference

This guide details advanced operational parameters, performance tuning options, deployment architectures, precedence rules, and configuration migration for Adult Hub.

For standard installations, only the common variables in `.env.example` are required. All parameters listed below have battle-tested production defaults built into code.

---

## 1. Configuration Precedence & Architecture

Adult Hub reads configuration from multiple layers, prioritizing operator runtime actions over static files:

```
┌─────────────────────────────────────────────────────────────┐
│ 1. Admin UI / JSON Overlay (admin-source-overrides.json)     │ <- Highest priority for sources
├─────────────────────────────────────────────────────────────┤
│ 2. Process Environment Variables (.env / docker-compose)     │ <- Highest priority for infra & secrets
├─────────────────────────────────────────────────────────────┤
│ 3. Database System Tables (system_schedules)                │ <- Priority for task schedules
├─────────────────────────────────────────────────────────────┤
│ 4. config.yaml (Checked-in / Mounted YAML, optional)         │ <- Base static configuration
├─────────────────────────────────────────────────────────────┤
│ 5. Built-in Code Defaults (common/config.py)                 │ <- Sane fallback values
└─────────────────────────────────────────────────────────────┘
```

### Precedence Guarantees:
1. **Dynamic Sources Win**: When a Telegram channel is added, edited, or removed via the Admin panel, the change is written to `/data/admin-source-overrides.json`. This override always takes precedence over any channel in `config.yaml`.
2. **Startup Safety**: Container startup **never overwrites** Admin UI values.
3. **Schedule Persistence**: System schedules (`database-backup`, `library-sync`, `artwork-generation`, `scene-fingerprints`, `phash-generation`) are seeded into the `system_schedules` database table on initial boot using `ON CONFLICT DO NOTHING`. Once initialized, schedules edited in the Admin UI (`/admin/schedules`) persist across container restarts and are not reset by environment variables.
4. **Database Connection Precedence**: `DATABASE_URL` is the canonical connection string for PostgreSQL 17. If unset or empty, it falls back to the legacy `STANDALONE_DATABASE_URL`, and in standalone mode defaults to `postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@database:5432/${POSTGRES_DB}`. Inside standalone Docker networks, the database container hostname is `database` (aliased as `postgres` for backward compatibility). The same rule applies to the other legacy `STANDALONE_*` names in section 4.3: the canonical name wins whenever it is set, and the legacy name is used only when it is unset or empty.
5. **Optional `config.yaml`**: `config.yaml` is completely optional. If absent, Adult Hub boots with default sync schedules and sources loaded directly from the Admin overlay. An unset or default container path (`/config/config.yaml`) is tolerated on fresh installs; an explicitly configured missing custom path will fail fast with an actionable error.
6. **Environment Scoping**: Secrets (`DATABASE_URL`, `ADMIN_WRITE_TOKEN`, `TELEGRAM_API_HASH`, etc.) are scoped to the specific container that requires them.

---

## 2. Advanced Configuration Knobs

Configure these settings in `.env` or Compose only when tuning specific performance characteristics. A `.env` value reaches a container only if `docker-compose.yml` passes that variable through; otherwise the container uses the code default.

### 2.1 Concurrency & Cache Tuning

| Variable | Default | Description |
| :--- | :--- | :--- |
| `RESOLVER_CONCURRENCY_LIMIT` | `10` | Maximum simultaneous upstream video stream resolutions. |
| `CACHE_TTL_SECONDS` | `3600` | Resolver LRU cache TTL for stream URLs (60 minutes). |
| `MAX_CACHE_ENTRIES` | `2000` | Maximum entries cached in resolver memory before eviction. |

### 2.2 Telegram Stream & Transfer Tuning

| Variable | Default | Description |
| :--- | :--- | :--- |
| `TELEGRAM_PARALLEL_STREAM_ENABLED` | `true` | Enable multi-worker parallel chunk streaming for playback. |
| `TELEGRAM_DOWNLOAD_WORKERS` | `4` | Parallel chunk download workers per stream when parallel mode is active. |
| `TELEGRAM_DOWNLOAD_GLOBAL_WORKERS` | `12` | Global ceiling on concurrent Telegram download workers. |
| `TELEGRAM_DOWNLOAD_BLOCK_BYTES` | `524288` | Byte size of chunk downloads from Telegram MTProto (512 KB). |
| `TELEGRAM_READ_AHEAD_BYTES` | `67108864` | Read-ahead buffer size for progressive streaming (64 MB). |
| `TELEGRAM_STREAM_CACHE_BYTES` | `268435456`| In-memory ring buffer size for active streams (256 MB). |
| `TELEGRAM_STREAM_CACHE_TTL_SECONDS`| `300` | Cache lifetime for idle playback stream chunks (5 minutes). |
| `TELEGRAM_DOWNLOAD_BLOCK_TIMEOUT_SECONDS` | `45` | Per-block stall timeout; failed blocks retry immediately. |
| `TELEGRAM_SESSION_PATH` | `/data/telegram.session` | MTProto session database path (falls back to legacy `/cache/telegram.session` if present). |
| `TELEGRAM_SIGNATURE_CHANNEL` | `""` | Telegram handle used in video captions to detect cast lists. |

### 2.3 Fingerprinting & Video pHash Budgets

| Variable | Default | Description |
| :--- | :--- | :--- |
| `TELEGRAM_FINGERPRINTS_ENABLED` | `true` | Enable automated OSHASH and pHash fingerprinting for metadata matching. |
| `TELEGRAM_FINGERPRINT_BACKFILL_PER_RUN` | `200` | Maximum videos matched via existing fingerprints (OSHASH/pHash) per sync run. Videos with neither fingerprint are skipped until one exists; the `phash-generation` schedule creates pHashes. |
| `TELEGRAM_OSHASH_BACKFILL_PER_RUN` | `500` | Maximum unhashed videos probed with OSHASH per sync run. |
| `TELEGRAM_PHASH_BATCH_SIZE` | `20` | Number of unhashed videos to compute visual pHash for per pHash generation batch run (e.g. 10, 20, 100). |
| `TELEGRAM_PHASH_ENABLED` | `true` | Match by cached visual pHash when OSHASH yields no duration match. |
| `TELEGRAM_PHASH_BYTE_BUDGET` | `268435456` | Byte limit for remote sample extraction when computing pHash (256 MB). |
| `TELEGRAM_PHASH_TIMEOUT_SECONDS` | `1200` | Timeout per pHash calculation task (20 minutes). |
| `TELEGRAM_FINGERPRINT_DAILY_BYTE_BUDGET` | `30000000000` | Daily bandwidth quota allocated to background fingerprinting (30 GB). |
| `TELEGRAM_FINGERPRINT_MAX_BYTES_PER_SECOND`| `2097152` | Maximum download bandwidth for fingerprinting jobs (2 MB/s). |
| `TELEGRAM_FINGERPRINT_USAGE_PATH` | `/data/cache/telegram-fingerprint-daily-usage.json` | Shared path to persistent daily bandwidth counter between resolver and sync. |

### 2.4 Duplicate Detection & Upkeep

| Variable | Default | Description |
| :--- | :--- | :--- |
| `DUPLICATE_AUTO_MERGE_ENABLED` | `false` | Automatically merge detected video duplicates during scheduled runs. |
| `DUPLICATE_AUTO_MERGE_TELEGRAM_FILE_ID` | `true` | Automatically merge videos sharing the identical Telegram file ID. |
| `VIDEO_VISUAL_WORKER_ENABLED` | `false` | Enable background visual duplicate screening in resolver. |
| `HIDE_DEAD_STREAMS` | `true` | Filter out videos whose stream links are confirmed deleted from Telegram. |
| `TELEGRAM_AVAILABILITY_CHECKS_PER_RUN` | `20` | Bounded message availability probes per channel per sync run. |

### 2.5 Supervisor Watchdog & Alarms

| Variable | Default | Description |
| :--- | :--- | :--- |
| `RESOLVER_WATCHDOG_INTERVAL_SECONDS` | `10` | Healthcheck poll interval in seconds. |
| `RESOLVER_WATCHDOG_TIMEOUT_SECONDS` | `4` | Timeout per healthcheck request before marking a failure. |
| `RESOLVER_WATCHDOG_FAILURES` | `3` | Consecutive failed checks before the supervisor restarts resolver (30 seconds). |
| `RESOLVER_WATCHDOG_STARTUP_GRACE_SECONDS`| `45` | Grace period after container boot before watchdog enforcement begins. |
| `ADMIN_V2_MONITOR_BACKUPS` | `false` | Report missing database backups as an alarm in the Admin UI. |
| `ADMIN_V2_QUEUE_WARNING_SECONDS` | `3600` | Queue delay threshold triggering a warning issue (1 hour). |
| `ADMIN_V2_BACKUP_WARNING_SECONDS` | `129600` | Backup age threshold triggering a warning issue (36 hours). |

### 2.6 Verified Opt-in Server & Worker pHash Tuning

Configure these opt-in variables only when enabling server-side or worker-uploaded visual fingerprinting.

| Variable | Default | Description |
| :--- | :--- | :--- |
| `PHASH_UPLOAD_ENABLED` | `false` | Enable client or worker uploaded visual pHash fingerprints. |
| `PHASH_SERVER_BYTE_BUDGET` | `268435456` | Byte limit for remote sample extraction when computing server pHash (256 MB). |
| `PHASH_SERVER_TIMEOUT_SECONDS` | `1200` | Timeout per server pHash extraction task (20 minutes). |
| `PHASH_SERVER_BYTES_PER_SECOND` | `2097152` | Download rate limit during active stream extraction (2 MB/s). |
| `PHASH_SERVER_IDLE_BYTES_PER_SECOND` | `10000000` | Download rate limit during idle extraction (10 MB/s). |
| `PHASH_ACCEPTED_DECODER_PROFILES` | `""` | Comma-separated list of allowed decoder profile identifiers. |
| `PHASH_ALLOWED_USER_IDS` | `""` | Comma-separated client user IDs allowed to submit fingerprints even when they are not admins. |

---

## 3. Supported Deployment Topologies

### Topology A: Standalone All-In-One (Default Quickstart)
Use `scripts/setup.sh` or `compose.standalone.yml` to run the complete stack on a single host.
- PostgreSQL 17 runs as an isolated container with named volume `postgres_data`.
- Resolver and sync connect via Docker internal DNS.
- Automated database backups run via durable queue jobs claimed directly by the sync worker (no separate backup container).

```sh
# Setup standalone environment
scripts/setup.sh --non-interactive
docker compose -p adult-hub-standalone -f compose.standalone.yml up -d
```

### Topology B: External PostgreSQL Database
To connect Adult Hub to an existing PostgreSQL 17 cluster (e.g., self-hosted cluster, RDS, Supabase):
1. Create a dedicated database and user:
   ```sql
   CREATE DATABASE adulthub;
   CREATE USER adulthub WITH PASSWORD 'your_secure_password';
   GRANT ALL PRIVILEGES ON DATABASE adulthub TO adulthub;
   ```
2. Configure `.env`:
   ```env
   DATABASE_URL=postgresql://adulthub:your_secure_password@db.internal:5432/adulthub
   ```
3. Run schema migrations:
   ```sh
   python3 migrate_db.py apply
   ```

### Topology C: Secure Reverse Proxy (Caddy / Nginx)
When exposing Adult Hub to companion players (Android TV, Desktop, Phone) over the internet:

> [!WARNING]
> **Admin Route Protection**: Do not expose management routes (`/admin/*`) publicly without explicit access controls (such as a VPN, client IP whitelist, or reverse-proxy basic auth). Remote playback clients and companion players only require access to playback and thumbnail endpoints (`/play/*`, `/admin/play/*`, `/api/thumb*`, and `/admin/api/thumb*`).

1. Route `/play/*`, `/admin/play/*`, `/api/thumb*`, and `/admin/api/thumb*` to Resolver port `8090`.
2. Restrict root `/admin/` access to trusted networks (e.g. LAN / VPN).
3. If public Admin access is explicitly desired, enable `ADMIN_PUBLIC=true`, set a strong `ADMIN_WRITE_TOKEN`, and configure `PUBLIC_DOMAIN`. By default (`ADMIN_PUBLIC=false`), requests arriving via `PUBLIC_DOMAIN` are denied access to management routes with 403 Forbidden.
4. Example Caddy configuration:
   ```caddy
   media.example.org {
       # Playback stream resolution and artwork (required for companion players)
       handle /play/* {
           reverse_proxy 127.0.0.1:8090
       }
       handle /admin/play/* {
           reverse_proxy 127.0.0.1:8090
       }
       handle /api/thumb* {
           reverse_proxy 127.0.0.1:8090
       }
       handle /admin/api/thumb* {
           reverse_proxy 127.0.0.1:8090
       }

       # Management interface restricted to trusted local network
       @trusted_admin {
           remote_ip 127.0.0.1 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16
           path /admin*
       }
       handle @trusted_admin {
           reverse_proxy 127.0.0.1:8090
       }

       # Block all other /admin access from the public internet
       handle /admin* {
           respond "Access Denied" 403
       }

       # Answer everything else with a 404
       handle {
           respond "Not Found" 404
       }
   }
   ```

---

## 4. Configuration Migration & Verification

Adult Hub includes built-in migration tools to safely inspect and modernize configurations.

### 4.1 Auditing Environment
Run the audit check to verify settings and detect legacy aliases with redacted values:

```sh
python3 -c "
from common.config_migration import audit_configuration
res = audit_configuration()
print('Valid:', res['valid'])
print('Warnings:', res['warnings'])
print('Aliases in use:', res['aliases_in_use'])
"
```

### 4.2 Importing Legacy YAML Channels to Admin UI Overlay
If you have an existing `config.yaml` and wish to manage all channels exclusively through the web Admin panel without relying on static files:

```sh
# Dry run (inspection only, completely side-effect-free)
python3 -c "
from common.config_migration import import_yaml_sources_to_overlay
print(import_yaml_sources_to_overlay(dry_run=True))
"

# Execute migration (atomic lock, idempotent, preserves all Admin UI edits)
python3 -c "
from common.config_migration import import_yaml_sources_to_overlay
print(import_yaml_sources_to_overlay(dry_run=False))
"
```

### 4.3 Legacy Variable Fallback Matrix

Older environment variables are automatically mapped to their canonical equivalents:

| Legacy Variable | Canonical Variable | Status |
| :--- | :--- | :--- |
| `ADMIN_DUPLICATE_TOKEN` | `ADMIN_WRITE_TOKEN` | Deprecated alias, accepted as fallback |
| `STASH_API_KEY` | `STASHDB_API_KEY` | Compatibility alias, accepted as fallback |
| `THEPORNDB_API_KEY` | `THEPORNDB_API_TOKEN` | Compatibility alias, accepted as fallback |
| `STANDALONE_DATABASE_URL`| `DATABASE_URL` | Legacy standalone name, accepted as fallback |
| `STANDALONE_ADMIN_PUBLIC`| `ADMIN_PUBLIC` | Legacy standalone name, accepted as fallback |
| `STANDALONE_STREAM_PUBLIC_URL_TELEGRAM` | `STREAM_PUBLIC_URL_TELEGRAM` | Legacy standalone name, accepted as fallback |
| `STANDALONE_STREAM_RESOLVER_URL` | `RESOLVER_PUBLIC_URL` | Legacy standalone name, accepted as fallback. `RESOLVER_PUBLIC_URL` takes precedence when both are set |
| `RESOLVER_TRUSTED_PROXY_IPS`| `FORWARDED_ALLOW_IPS`| Compatibility alias, accepted as fallback |
| `MEDIA_PATH` | `MEDIA_DIR` | Directory path alias, accepted as fallback |

---

## 5. Troubleshooting Common Issues

### Issue: "DATABASE_URL is not configured"
- **Cause**: `.env` file is missing or `DATABASE_URL` is commented out.
- **Fix**: Copy `.env.example` to `.env` and verify `DATABASE_URL=postgresql://user:pass@host:5432/dbname`.

### Issue: "Setting 'ADMIN_WRITE_TOKEN' cannot be whitespace-only"
- **Cause**: An empty string with spaces was supplied for `ADMIN_WRITE_TOKEN`.
- **Fix**: Provide a valid secret token string or leave the variable completely unset/empty to run without token authorization.

### Issue: "403 Forbidden: Origin header does not match"
- **Cause**: Admin write operations require the browser `Origin` header to match `PUBLIC_DOMAIN` or the local host authority.
- **Fix**: Ensure your reverse proxy forwards `Host` and `X-Forwarded-Proto` and that `PUBLIC_DOMAIN` in `.env` matches the domain in your browser URL bar.

## 6. Quarantine & Retention Policy

Blocked channels leave their catalog rows behind on purpose: the metadata, pHash
fingerprints and source provenance are the only evidence of what those posts
contained, and they are what lets the deduplication pass match the same scene
when it appears on a channel that still works.

Nothing removes these rows on a timer.

### 6.1 What the quarantine holds

| Location | Holds | Removed when |
|---|---|---|
| `telegram_unavailable_quarantine` (table) | one row per Telegram post that missed twice: `suspect` after the first miss, `quarantined` after the second, plus the moved file list | the post answers again (the row is deleted and the files move back), or an operator removes the channel |
| `DATA_DIR/telegram-unavailable-quarantine/` | the poster and STRM files of quarantined posts, one folder per video id | the post answers again (files move back to the media tree) |
| `DATA_DIR/duplicate-quarantine/<job id>/` | the files of items hidden or merged by a duplicate decision, kept for undo | undo restores them; there is no automatic expiry |

Quarantined rows keep `playable = 0`, so the client catalog never lists them, but
the row, its `video_sources`, its categories and its fingerprints stay in place.

### 6.2 What is never deleted by a timer

- **Telegram rows.** `sync.library_cleanup.cleanup_retention()` only considers
  providers other than `telegram`, and `cleanup_orphaned_entries()` excludes every
  video with a `quarantined` row through `NOT EXISTS`.
- **Files of quarantined videos.** They were moved out of the media tree, so the
  orphan detector cannot see them as missing files either.
- **Rows of a channel that left the configuration.** `is_cleanup_allowed()`
  requires the provider *and* the channel to be enabled in the current
  configuration and to have synced successfully in this run, so a blocked channel
  can never pass the gate.

`sync.retention_days` prunes only non-Telegram providers that synced fine in the
same run; the Telegram catalogue is out of its reach.

### 6.3 What is deleted, and when

- A duplicate decision (`hide`, `merge`) retires the losing row, but stores the
  full row, its sources, categories and file list in
  `video_duplicate_decisions.snapshot`, which undo restores. Active decisions are
  retained by the history job.
- Removing a channel through the admin job deletes that channel's own videos and
  its quarantine rows after restoring the moved files. Videos that another active
  source still serves are preserved (`_deletable_videos` counts them as
  `preserved_shared`).
- The monthly history retention (`common/history_retention.py`) prunes only
  bookkeeping older than one month: `admin_action_events`, `admin_audit_events`,
  `external_metadata_attempts`, `video_duplicate_jobs` and restored
  `admin_orphan_quarantine` entries. Active decisions, active quarantines and
  running jobs are skipped.

### 6.4 Why the metadata earns its disk space

The deduplication pass sorts candidate keepers by `playable` first, so a live
duplicate always outranks a blocked one and a merge cannot retire working content
in favour of a dead row. When the merge runs the other way, the loser's title,
performers and tags are copied into the keeper, so a blocked post still enriches
the live copy of the same scene. Deleting quarantined rows would therefore throw
away the only record of those scenes and the fingerprints that recognise them.

