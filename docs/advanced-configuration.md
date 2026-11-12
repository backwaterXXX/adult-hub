# Adult Hub Advanced Configuration Reference

This guide details advanced operational parameters, tuning options, deployment architectures, precedence rules, and migration paths for Adult Hub.

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
│ 4. config.yaml (Checked-in / Mounted YAML, now optional)    │ <- Base static configuration
├─────────────────────────────────────────────────────────────┤
│ 5. Built-in Code Defaults (common/config.py)                 │ <- Sane fallback values
└─────────────────────────────────────────────────────────────┘
```

### Precedence Guarantees:
1. **Dynamic Sources Win**: When a Telegram channel is added, edited, or removed via the Admin panel, the change is written to `/data/admin-source-overrides.json`. This override always takes precedence over any channel in `config.yaml`.
2. **Startup Safety**: Container startup **never overwrites** Admin UI values.
3. **Optional `config.yaml`**: `config.yaml` is completely optional. If absent, Adult Hub boots with default sync schedules and sources loaded directly from the Admin overlay.
4. **Environment Scoping**: Secrets (`DATABASE_URL`, `ADMIN_WRITE_TOKEN`, `TELEGRAM_API_HASH`, etc.) are scoped to the specific container that requires them.

---

## 2. Advanced Configuration Knobs

Configure these settings in `.env` or Compose only when tuning specific performance characteristics. A `.env` value reaches a container only if `docker-compose.yml` passes that variable through; otherwise the container uses the code default.

### 2.1 Concurrency & Cache Tuning

| Variable | Default | Description |
| :--- | :--- | :--- |
| `RESOLVER_CONCURRENCY_LIMIT` | `10` | Maximum simultaneous upstream video stream resolutions. |
| `CACHE_TTL_SECONDS` | `1500` | Resolver LRU cache TTL for stream URLs (25 minutes). |
| `MAX_CACHE_ENTRIES` | `2000` | Maximum entries cached in resolver memory before eviction. |

### 2.2 Telegram Stream & Transfer Tuning

| Variable | Default | Description |
| :--- | :--- | :--- |
| `TELEGRAM_PARALLEL_STREAM_ENABLED` | `false` | Enable multi-worker parallel chunk streaming for playback. |
| `TELEGRAM_DOWNLOAD_WORKERS` | `3` | Parallel chunk download workers per stream when parallel mode is active. |
| `TELEGRAM_DOWNLOAD_GLOBAL_WORKERS` | `6` | Global ceiling on concurrent Telegram download workers. |
| `TELEGRAM_DOWNLOAD_BLOCK_BYTES` | `524288` | Byte size of chunk downloads from Telegram MTProto (512 KB). |
| `TELEGRAM_READ_AHEAD_BYTES` | `33554432` | Read-ahead buffer size for progressive streaming (32 MB). |
| `TELEGRAM_STREAM_CACHE_BYTES` | `134217728`| In-memory ring buffer size for active streams (128 MB). |
| `TELEGRAM_STREAM_CACHE_TTL_SECONDS`| `120` | Cache lifetime for idle playback stream chunks (seconds). |
| `TELEGRAM_SESSION_PATH` | `/data/telegram.session` | MTProto session database path. |
| `TELEGRAM_SIGNATURE_CHANNEL` | `""` | Telegram handle used in video captions to detect cast lists. |
| `TELEGRAM_CHANNEL_NAME_HINTS` | `""` | Comma-separated channel titles treated as source channels rather than studios. |

### 2.3 Fingerprinting & Video pHash Budgets

| Variable | Default | Description |
| :--- | :--- | :--- |
| `TELEGRAM_FINGERPRINTS_ENABLED` | `true` | Enable automated OSHASH and pHash fingerprinting for metadata matching. |
| `TELEGRAM_OSHASH_BACKFILL_PER_RUN` | `500` | Maximum unhashed videos probed with OSHASH per sync run. |
| `TELEGRAM_PHASH_ENABLED` | `true` | Fallback to visual pHash when OSHASH yields no duration match. |
| `TELEGRAM_PHASH_BYTE_BUDGET` | `268435456` | Byte limit for remote sample extraction when computing pHash (256 MB). |
| `TELEGRAM_PHASH_TIMEOUT_SECONDS` | `1200` | Timeout per pHash calculation task (20 minutes). |
| `TELEGRAM_FINGERPRINT_DAILY_BYTE_BUDGET` | `30000000000` | Daily bandwidth quota allocated to background fingerprinting (30 GB). |
| `TELEGRAM_FINGERPRINT_MAX_BYTES_PER_SECOND`| `2097152` | Maximum download bandwidth for fingerprinting jobs (2 MB/s). |
| `TELEGRAM_FINGERPRINT_USAGE_PATH` | `/data/cache/telegram-fingerprint-daily-usage.json` | Path to persistent daily bandwidth counter. |

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
| `RESOLVER_WATCHDOG_FAILURES` | `3` | Consecutive failed checks before the supervisor restarts resolver (~30 seconds). |
| `RESOLVER_WATCHDOG_STARTUP_GRACE_SECONDS`| `45` | Grace period after container boot before watchdog enforcement begins. |
| `ADMIN_V2_MONITOR_BACKUPS` | `false` | Report missing database backups as an alarm in the Admin UI. |
| `ADMIN_V2_QUEUE_WARNING_SECONDS` | `3600` | Queue delay threshold triggering a warning issue (1 hour). |
| `ADMIN_V2_BACKUP_WARNING_SECONDS` | `129600` | Backup age threshold triggering a warning issue (36 hours). |

---

## 3. Supported Deployment Topologies

### Topology A: Standalone All-In-One (Default Quickstart)
Use `scripts/setup.sh` or `compose.standalone.yml` to run the complete stack on a single host.
- PostgreSQL 17 runs as an isolated container with named volume `postgres_data`.
- Jellyfin runs with bridge networking and persistent volumes `jellyfin_config` and `jellyfin_cache`.
- Resolver and sync connect via Docker internal DNS.

```sh
# Setup standalone environment
scripts/setup.sh --non-interactive
docker compose -f compose.standalone.yml up -d
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
   python3 migrate_db.py
   ```

### Topology C: External Jellyfin Server
If Jellyfin already runs on another server or container:
1. Generate an API Key in Jellyfin: **Dashboard > Advanced > API Keys > Add**.
2. Configure `.env`:
   ```env
   JELLYFIN_URL=http://jellyfin.local:8096
   JELLYFIN_API_KEY=your_generated_api_key
   ```
3. Ensure both Adult Hub and Jellyfin share access to the media directory (e.g., via NFS or local bind mount) mapped at `/media/adult`.

### Topology D: Reverse Proxy (Caddy / Nginx)
When exposing Adult Hub to companion players (Android TV, Desktop, Phone) over the internet:
1. Route `/play/*` and `/admin/*` to resolver port `8090`.
2. Configure `.env`:
   ```env
   RESOLVER_PUBLIC_URL=https://media.example.org
   PUBLIC_DOMAIN=media.example.org
   ADMIN_PUBLIC=true
   ADMIN_WRITE_TOKEN=your_secret_bearer_token
   RESOLVER_TRUSTED_PROXY_IPS=127.0.0.1,::1,172.18.0.1
   ```
3. Example Caddy configuration:
   ```caddy
   media.example.org {
       reverse_proxy 127.0.0.1:8090 {
           header_up Host {host}
           header_up X-Real-IP {remote_host}
           header_up X-Forwarded-For {remote_host}
           header_up X-Forwarded-Proto {scheme}
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
If you have an existing `config.yaml` and wish to manage all channels exclusively through the web Admin panel without relying on the file:

```sh
# Dry run (inspection only)
python3 -c "
from common.config_migration import import_yaml_sources_to_overlay
print(import_yaml_sources_to_overlay(dry_run=True))
"

# Execute migration (idempotent, does not overwrite Admin edits)
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
| `STANDALONE_DATABASE_URL`| `DATABASE_URL` | Standardized, accepted as fallback |
| `LUKAYAN_*` (fingerprints) | `TELEGRAM_*` | Internal legacy aliases |

---

## 5. Troubleshooting Common Issues

### Issue: "DATABASE_URL is not set"
- **Cause**: `.env` file is missing or `DATABASE_URL` is commented out.
- **Fix**: Copy `.env.example` to `.env` and verify `DATABASE_URL=postgresql://user:pass@host:5432/dbname`.

### Issue: "Telegram provider is not configured: set TELEGRAM_API_ID env var"
- **Cause**: Attempted to index or play Telegram media without MTProto credentials.
- **Fix**: Set `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` from https://my.telegram.org.

### Issue: "403 Forbidden: Origin header does not match"
- **Cause**: Admin write operations require the browser `Origin` header to match `PUBLIC_DOMAIN`.
- **Fix**: Ensure your reverse proxy forwards `Host` and `X-Forwarded-Proto` and that `PUBLIC_DOMAIN` in `.env` matches the domain in your browser URL bar.
