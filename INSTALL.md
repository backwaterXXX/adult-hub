# Adult Hub Installation and Operations Guide

This guide covers deployment options, operational workflows, storage architecture, backup procedures, Jellyfin integration, and troubleshooting for Adult Hub.

For a fast three-command setup on a new system, see the [Quick Start in README.md](README.md#quick-start).

---

## Table of Contents

1. [Deployment Architectures](#1-deployment-architectures)
2. [Standalone Prebuilt Image Bundle (Downloadable Archive)](#2-standalone-prebuilt-image-bundle-downloadable-archive)
   - [Overview and Bundle Contents](#overview-and-bundle-contents)
   - [Prerequisites](#prerequisites)
   - [Three-Step Installation](#three-step-installation)
   - [Interactive Telegram Authentication Service](#interactive-telegram-authentication-service)
   - [Network Binding and Client-Facing Playback](#network-binding-and-client-facing-playback)
   - [Updating Prebuilt Deployments](#updating-prebuilt-deployments)
3. [Standalone Repository Installer (`make setup`)](#3-standalone-repository-installer-make-setup)
   - [Overview and Directory Layout](#overview-and-directory-layout)
   - [Running `make setup`](#running-make-setup)
   - [Setup Options, Prebuilt Consumption, and Source Build](#setup-options-prebuilt-consumption-and-source-build)
   - [Resuming or Recovering Interrupted Setup](#resuming-or-recovering-interrupted-setup)
4. [Existing Infrastructure / Production Deployment (`make deploy`)](#4-existing-infrastructure-production-deployment-make-deploy)
   - [Overview and Differences from Standalone](#overview-and-differences-from-standalone)
   - [Prerequisites and External Services](#prerequisites-and-external-services)
   - [Initial Configuration](#initial-configuration)
   - [Applying Migrations](#applying-migrations)
   - [Deployment with `make deploy`](#deployment-with-make-deploy)
5. [Maintenance and Updates](#5-maintenance-and-updates)
   - [Updating Prebuilt Standalone Deployments](#updating-prebuilt-standalone-deployments)
   - [Updating Repository Standalone Deployments (`make update`)](#updating-repository-standalone-deployments-make-update)
   - [Updating Production Deployments](#updating-production-deployments)
6. [Storage, Volumes, and Data Persistence](#6-storage-volumes-and-data-persistence)
7. [Backup and Database Restoration](#7-backup-and-database-restoration)
   - [Automated Scheduled Backups](#automated-scheduled-backups)
   - [Manual Database Snapshots](#manual-database-snapshots)
   - [Restoring from a Backup](#restoring-from-a-backup)
8. [Jellyfin Integration](#8-jellyfin-integration)
   - [Basic Setup (No API Key Required)](#basic-setup-no-api-key-required)
   - [Recommended Jellyfin Library Configuration](#recommended-jellyfin-library-configuration)
   - [Optional API Key Integration](#optional-api-key-integration)
9. [Reverse Proxy and Network Configuration](#9-reverse-proxy-and-network-configuration)
10. [Troubleshooting and Common Issues](#10-troubleshooting-and-common-issues)
11. [Operational Limitations and Safety Bounds](#11-operational-limitations-and-safety-bounds)
12. [Verification Scope and Runtime Limits](#12-verification-scope-and-runtime-limits)

---

## 1. Deployment Architectures

Adult Hub supports three deployment workflows:

| Feature | Prebuilt Release Bundle (Recommended) | Standalone Repository Installer | Existing Infrastructure / Production |
|---|---|---|---|
| **Primary File** | `docker-compose.yml` (from bundle) | `compose.standalone.yml` | `docker-compose.yml` |
| **Compose Project** | `adult-hub-standalone` | `adult-hub-standalone` | `adult-hub` |
| **Setup Workflow** | `pull` -> `run telegram-auth` -> `up -d` | `make setup` (`scripts/setup.sh`) | Manual config + `make deploy` |
| **Update Command** | `docker compose pull && docker compose up -d` | `make update` (`scripts/update.sh`) | `make deploy` |
| **Build Directives** | None (prebuilt published images) | Local build fallback / `--source-build` | Source build via `make deploy` |
| **Marker in `.env`** | `ADULT_HUB_STANDALONE=true` | `ADULT_HUB_STANDALONE=true` | None (omitted) |
| **Database** | Bundled PostgreSQL 17 (`postgres:17-alpine`) | Bundled PostgreSQL 17 (`postgres:17-alpine`) | External PostgreSQL 17 cluster |
| **Database Migrations**| Automatic via ephemeral `migration` container | Automatic via ephemeral `migration` container | Manual via `python3 migrate_db.py apply` |
| **Docker Network** | Internal Compose network | Internal Compose network | External network (`selfhost`) |
| **Jellyfin Service** | Bundled container (`jellyfin/jellyfin:12.4`) | Bundled container (`jellyfin/jellyfin:12.4`) | Host networking (`network_mode: host`) |
| **Data Layout** | Isolated under `./data`, `./media`, `./cache` | Isolated under `./standalone/` | Dedicated host paths (`./data`, etc.) |

---

## 2. Standalone Prebuilt Image Bundle (Downloadable Archive)

The standalone release bundle is the fastest and cleanest installation method for users who do not intend to modify the Adult Hub codebase. It does not require cloning the Git repository, installing build dependencies, or compiling container images locally.

> [!NOTE]
> **Prebuilt Image Availability**: Official container images (`ghcr.io/backwaterxxx/adult-hub-resolver` and `ghcr.io/backwaterxxx/adult-hub-sync`) are published automatically to the GitHub Container Registry (GHCR) upon publication of tagged releases.

### Overview and Bundle Contents

The downloadable archive (`adult-hub-standalone-v0.4.15.tar.gz` or `.zip`) contains everything needed to operate the stack:

```
adult-hub/
├── docker-compose.yml        # Self-contained Compose file (no build directives or repo mounts)
├── .env.example              # Environment configuration template
├── config.example.yaml       # Channel and sync configuration template
└── README.md                 # Quick reference instructions
```

### Prerequisites

- **Docker Engine** (24.0+) with the **Docker Compose v2** plugin (`docker compose`).
- **Telegram MTProto API Credentials**: Register an application at [my.telegram.org](https://my.telegram.org) under *API development tools* to obtain an integer `API ID` and hexadecimal `API Hash`.
- Access to your Telegram account to receive login codes and 2FA authentication.

### Three-Step Installation

1. **Extract Release Bundle & Prepare Configuration**:

   ```sh
   mkdir adult-hub && cd adult-hub
   curl -sSL https://github.com/backwaterXXX/adult-hub/releases/latest/download/adult-hub-standalone.tar.gz | tar -xz
   cp .env.example .env
   ```

   > [!NOTE]
   > `config.yaml` is optional: you can add channels directly via the Admin UI, or copy `cp config.example.yaml config.yaml` if you prefer static configuration. For advanced overrides, see [CONFIGURATION.md](CONFIGURATION.md).

   Edit `.env` to configure:
   - `POSTGRES_PASSWORD`: A strong random password for the internal database.
   - `ADMIN_WRITE_TOKEN`: A secret token for the web admin panel.
   - `TELEGRAM_API_ID` and `TELEGRAM_API_HASH`: Your Telegram API credentials from [my.telegram.org](https://my.telegram.org).

2. **Pull Images & Authenticate Telegram**:

   Pull the prebuilt images:
   ```sh
   docker compose pull
   ```

   Run the interactive Telegram login service:
   ```sh
   docker compose run --rm telegram-auth
   ```

   Follow the interactive prompts:
   - Enter your phone number in international format (e.g. `+1234567890`).
   - Enter the login code delivered to your Telegram app.
   - Enter your 2FA cloud password if enabled.

   The session token is saved securely to `./data/telegram.session`.

3. **Start Core and Application Services**:

   ```sh
   docker compose up -d
   ```

   Docker Compose starts PostgreSQL, executes schema migrations via an ephemeral `migration` container gated by `service_completed_successfully`, and brings up Jellyfin, the Resolver API, and the Sync worker (which also runs scheduled backups and queue processing).

### Interactive Telegram Authentication Service

The `telegram-auth` service is declared under the `tools` Compose profile (`profiles: ["tools"]`). This design guarantees that:
- `docker compose up -d` will **never** inadvertently start or block on the authentication service.
- Running `docker compose run --rm telegram-auth` executes in an isolated one-off container before long-running services start, preventing SQLite session database lock collisions on `telegram.session`.
- Startup of `sync` will fail fast and clearly if `telegram.session` has not been generated yet, advising the operator to run `telegram-auth`.

### Network Binding and Client-Facing Playback

By default, services bind to `127.0.0.1` (loopback) for security. To enable streaming to smart TVs, Android TV players, and companion apps on your home network:

1. In `.env`, set `RESOLVER_BIND_IP` and `JELLYFIN_BIND_IP` to your host's private LAN IP (e.g. `192.168.1.100`):
   ```env
   RESOLVER_BIND_IP=192.168.1.100
   JELLYFIN_BIND_IP=192.168.1.100
   ```
2. Set `STREAM_PUBLIC_URL_TELEGRAM` to your resolver endpoint reachable by client devices. The legacy name `STANDALONE_STREAM_PUBLIC_URL_TELEGRAM` is still accepted as a fallback:
   ```env
   STREAM_PUBLIC_URL_TELEGRAM=http://192.168.1.100:8090
   ```
   Inside the Docker bridge network, Jellyfin resolves stream links through Compose internal DNS (`http://resolver:8090`), while client-facing players use the configured LAN URL.

### Updating Prebuilt Deployments

To update an existing release bundle installation to a new version:

```sh
docker compose pull
docker compose up -d
```

Compose automatically downloads newer image versions, runs schema migrations via the migration container, and restarts application services without modifying configuration, media, or database files.

---

## 3. Standalone Repository Installer (`make setup`)

Standalone deployment via repository checkout bundles all services (PostgreSQL 17, Jellyfin container using image tag `jellyfin/jellyfin:12.4`, Resolver, and Sync worker). It requires no pre-existing database or external Docker network.

### Overview and Directory Layout

Standalone deployment stores all runtime files in an isolated `./standalone/` directory:

```
adult-hub/
├── .env                          # Root environment with ADULT_HUB_STANDALONE=true
├── compose.standalone.yml        # Standalone Compose specification
└── standalone/
    ├── config.yaml               # Application configuration (sync & channel sources)
    ├── data/                     # Mounted to /data (Telegram session, SQLite locks, linkcheck logs)
    │   └── backups/              # Automated database snapshots
    ├── cache/                    # Mounted to /cache (worker capabilities, temp caches)
    └── media/                    # Mounted to /media/adult (.strm, .nfo, thumbnails)
```

In addition, Docker manages three named volumes for database and Jellyfin state:
- `adult-hub-standalone_postgres_data`: PostgreSQL cluster files (`/var/lib/postgresql/data`).
- `adult-hub-standalone_jellyfin_config`: Jellyfin server settings and internal database (`/config`).
- `adult-hub-standalone_jellyfin_cache`: Jellyfin transcode and image caches (`/cache`).

### Running `make setup`

Run the interactive installer from the repository root:

```sh
make setup
```

The installer executes `scripts/setup.sh` and performs the following steps:

1. **Safety Environment Check**:
   - Refuses to run if a production `.env` (without `ADULT_HUB_STANDALONE=true`) exists.
   - Refuses to run if an active production Compose project `adult-hub` is running.
   - Refuses to run if an existing `postgres_data` volume is detected without credentials in `.env`, preventing accidental database password desynchronization.
2. **Prerequisites Check**: Verifies Docker CLI, Compose v2 (`docker compose`), daemon accessibility, and write permissions in the repository.
3. **Network Mode Selection**:
   - **Loopback (`127.0.0.1`)** [Default]: Safe host-only binding. Resolver binds to `127.0.0.1:8090` and Jellyfin binds to `127.0.0.1:8096`. Services are not accessible from external network devices.
   - **Local Network (LAN)**: Detects host network interfaces and binds to a specific private LAN IP (e.g., `192.168.1.50`). Allows smart TVs, Android TV players, and phones on your home network to access streams. Wildcard `0.0.0.0` is prohibited for safety.
4. **Port Conflict Detection**: Probes candidate ports (default `8090` for Resolver, `8096` for Jellyfin). If occupied, automatically suggests and allows choosing available alternative ports.
5. **Directory Creation**: Initializes `./standalone/data/backups`, `./standalone/cache`, and `./standalone/media` with restricted permissions (`chmod 750`).
6. **Credential Generation**: Atomically writes a new `.env` (mode `0600`) with cryptographically secure random values for `POSTGRES_PASSWORD` and `ADMIN_WRITE_TOKEN`.
7. **Configuration Generation**: Writes an initial `standalone/config.yaml` with a valid default sync configuration and an empty `channels: []` list.
8. **Core Service Startup and Migration**:
   - Starts `database` and `jellyfin` containers.
   - Awaits database health check.
   - Runs database schema migrations via a one-off `migration` container.
9. **Interactive Telegram Authentication**:
   - Prompts for your Telegram `API ID` and `API Hash` (from [my.telegram.org](https://my.telegram.org)).
   - Checks if resolver or sync containers are running to prevent SQLite session locking.
   - Runs `python3 -m resolver.providers.telegram_auth auth` in an interactive container. Enter your phone number (international format, e.g. `+1234567890`), login code received in Telegram, and 2FA password (if enabled).
10. **Application Stack Startup**: Starts `resolver` and `sync` services once authorization succeeds. If authentication is postponed, core database and Jellyfin services remain running while resolver and sync remain withheld until authorization is completed.

### Setup Options, Prebuilt Consumption, and Source Build

By default, `scripts/setup.sh` is configured to consume prebuilt published container images (`ghcr.io/backwaterxxx/adult-hub-resolver` and `ghcr.io/backwaterxxx/adult-hub-sync`). If the prebuilt images are not yet available on the registry (for example, on development branches prior to publication), the installer falls back to building images from repository source.

To explicitly force building from local source:

```sh
scripts/setup.sh --source-build
# or using make:
make setup SOURCE_BUILD=1
```

You can also bypass prompts or inspect status using flags with `scripts/setup.sh`:

```sh
# Show configuration summary and service health without modifying anything
scripts/setup.sh --status

# Non-interactive loopback installation (useful for headless setups or scripts)
scripts/setup.sh -y --bind-mode loopback

# Non-interactive LAN installation binding to a specific host IP
scripts/setup.sh -y --bind-mode lan --bind-ip 192.168.1.100

# Custom port assignments
scripts/setup.sh --resolver-port 8091 --jellyfin-port 8097

# Prepare config and directories without launching Docker containers
scripts/setup.sh --skip-docker-start

# Force building images from local source code
scripts/setup.sh --source-build
```

Available flags:
- `-h, --help`: Display usage summary.
- `-y, --non-interactive`: Accept defaults or environment variables without prompting.
- `--status`: Display current service status, endpoints, and authentication state without altering files.
- `--source-build`: Build container images locally from repository Dockerfiles instead of consuming prebuilt images.
- `--bind-mode MODE`: Network binding mode: `loopback` (default) or `lan`.
- `--bind-ip IP`: Private IP address to bind (required when `--bind-mode lan` is used non-interactively).
- `--resolver-port PORT`: Port for resolver API and admin panel (default: `8090`).
- `--jellyfin-port PORT`: Port for Jellyfin web UI (default: `8096`).
- `--public-url URL`: Client-facing stream URL for LAN playback.
- `--skip-docker-start`: Generate `.env`, configuration, and directories without starting containers.
- `--skip-docker-check`: Skip Docker daemon connectivity check (useful for automated testing or non-standard environments).
- `--docker-bin PATH`: Custom path to Docker binary (default: `docker`).
- `--root-dir PATH`: Target installation root (default: repository root).

### Resuming or Recovering Interrupted Setup

The installer is idempotent and safe to re-run at any time:

- **Interrupted Network/Credential Wizard**: If setup is halted before containers start, running `make setup` again re-reads any preserved values or re-prompts safely.
- **Preserved Secrets**: If `.env` already contains `ADULT_HUB_STANDALONE=true`, existing credentials (`POSTGRES_PASSWORD`, `ADMIN_WRITE_TOKEN`, port bindings) are preserved; existing secrets are never overwritten.
- **Existing Valid Configuration**: If `standalone/config.yaml` already exists and validates against the configuration schema, it is preserved untouched.
- **Interrupted Telegram Authentication**: If you cancel during the phone/code prompt, run `make setup` again. It will detect existing database and configuration, verify that containers are not locking the session, and re-open the interactive login prompt.
- **Session File Safety**: If a `telegram.session` file already exists but authorization fails, `scripts/setup.sh` will **never** overwrite or delete the session file. It reports an error and requests that the operator either restore the valid session or manually delete the stale file before re-authenticating.
- **Volume Desynchronization Guard**: If the `adult-hub-standalone_postgres_data` volume exists but `.env` was deleted, `scripts/setup.sh` safely aborts to prevent creating a new password that the existing database cluster would reject. You must either restore your previous `.env` file or explicitly wipe the orphaned volume (`docker volume rm adult-hub-standalone_postgres_data`) before starting clean.

---

## 4. Existing Infrastructure / Production Deployment (`make deploy`)

The production deployment uses `docker-compose.yml` to integrate Adult Hub into an existing self-hosted server environment alongside an external PostgreSQL cluster, reverse proxy, and hardware-accelerated media stack.

### Overview and Differences from Standalone

- **No Bundled Database**: Production does not start a PostgreSQL container. It connects to an existing PostgreSQL 17 server specified by `DATABASE_URL`.
- **External Docker Network**: Relies on an external Docker network named `selfhost` so containers can communicate with your external database and reverse proxy.
- **Jellyfin Host Networking & Hardware Acceleration**: In `docker-compose.yml`, Jellyfin runs with `network_mode: host`, mounts `/dev/dri` for Intel QuickSync / VA-API hardware transcoding, and runs custom ffmpeg fast-probing wrappers.
- **Release Version Pinning**: `make deploy` reads `VERSION` and builds/tags release images matching the version string.

### Prerequisites and External Services

1. **Docker and Docker Compose v2**: Installed and running on the host.
2. **External Docker Network**: Create the network before starting:
   ```sh
   docker network create selfhost
   ```
3. **PostgreSQL 17 Database**:
   - Create a database and user (e.g. `adulthub`).
   - Note the connection string: `postgresql://user:password@postgres:5432/adulthub` (where `postgres` is the container/host name on the `selfhost` network).
4. **Dedicated Test Database**: If running automated tests or migrations on the host, configure a separate `TEST_DATABASE_URL` (never point tests at the production database).

### Initial Configuration

1. Copy the example configuration files:
   ```sh
   cp .env.example .env
   cp config.example.yaml config.yaml
   ```
   *(Ensure `.env` does **not** contain `ADULT_HUB_STANDALONE=true`.)*

2. Edit `.env`:
   - Set `HOST_IP` to your host's LAN IP.
   - Set `DATABASE_URL` to your PostgreSQL 17 connection string.
   - Set `RESOLVER_PUBLIC_URL` to your external HTTPS URL (e.g., `https://media.example.org`).
   - Set `TELEGRAM_API_ID` and `TELEGRAM_API_HASH`.
   - Optionally configure `JELLYFIN_URL` and `JELLYFIN_API_KEY`.
   - Set `MEDIA_PATH` (defaults to `./media/adult`).

3. Edit `config.yaml`:
   - Add your Telegram channels under `sources.channels`.

4. Authenticate Telegram:
   - For a production deployment, place an authorized session file at `./data/telegram.session` (or run one-time authentication in an isolated container).

### Applying Migrations

Before launching application services, apply schema migrations to the external database:

```sh
# Inspect current migration status
python3 migrate_db.py status

# Apply pending migrations
python3 migrate_db.py apply
```

*(Note: In production, `make deploy` does not run migrations automatically; you must run `migrate_db.py apply` when deploying database schema updates.)*

### Deployment with `make deploy`

To build and start application containers:

```sh
make deploy
```

What `make deploy` does:
1. Reads `VERSION` from the repository root and exports it as `APP_VERSION`.
2. Builds `resolver` and `sync` images tagged as `adult-hub-resolver:${APP_VERSION}` and `adult-hub-sync:${APP_VERSION}`.
3. Restarts `resolver` and `sync` services using `--no-build`.

What `make deploy` does **not** do:
- It does **not** run `git pull`.
- It does **not** touch or restart the `jellyfin` container (Jellyfin is defined in `docker-compose.yml` for convenience, but media server restarts are kept decoupled from application code releases).

---

## 5. Maintenance and Updates

### Updating Prebuilt Standalone Deployments

For release bundle deployments, updating is entirely container-native:

```sh
docker compose pull
docker compose up -d
```

Compose automatically downloads newer image versions, runs schema migrations via the migration container, and restarts application services without modifying configuration, media, or database files.

### Updating Repository Standalone Deployments (`make update`)

For standalone installations, `make update` runs `scripts/update.sh` to safely upgrade images, apply migrations, and restart containers without data loss.

> [!IMPORTANT]
> `make update` updates the local containers from the current repository files. It does **not** run `git pull`. If you want to pull the latest code from GitHub, run `git pull` before running `make update`.

#### Update Workflow Steps:

```sh
git pull
make update
```

The script executes the following automated sequence:
1. **Safety Verification**:
   - Ensures `.env` exists and contains `ADULT_HUB_STANDALONE=true`.
   - Verifies that no production `adult-hub` Compose project is running.
2. **Graceful Sync Shutdown**:
   - If the `sync` service is currently running, sends SIGTERM and waits up to 240 seconds (`stop -t 240 sync`) for the worker to drain the current channel or stage boundary cleanly, preventing corrupted or half-synced batches.
3. **Rebuild Application Images**:
   - Builds fresh `resolver` and `sync` images tagged with the release version in `VERSION`.
4. **Database Verification and Migration**:
   - Ensures the standalone PostgreSQL container is running and healthy.
   - Launches the one-off `migration` container to execute any pending schema migrations.
   - **Safety gate**: If migration fails, the update halts immediately; old application containers are **not** replaced, preventing data inconsistencies.
5. **Telegram Authorization Verification**:
   - Verifies that the Telegram session remains valid.
   - If valid, starts `resolver` and `sync` containers with `--no-build`.
   - If authorization is missing or revoked, starts only `resolver` and withholds `sync` until re-authenticated.
6. **Readiness Probe**:
   - Polls the resolver readiness endpoint (`/admin/health/ready`) until healthy.
   - Displays a completion summary with endpoints.

Available flags for direct invocation (`scripts/update.sh [OPTIONS]`):
- `-h, --help`: Display usage summary.
- `--docker-bin PATH`: Custom path to Docker binary (default: `docker`).
- `--root-dir PATH`: Target installation root (default: repository root).
- `--skip-docker-check`: Skip Docker daemon connectivity check (for testing or non-standard environments).

### Updating Production Deployments

For production deployments:

1. Pull the desired commit or release tag:
   ```sh
   git pull
   ```
2. Apply database migrations:
   ```sh
   python3 migrate_db.py apply
   ```
3. Rebuild and launch application services:
   ```sh
   make deploy
   ```
4. Verify resolver readiness:
   ```sh
   curl http://127.0.0.1:8090/admin/health/ready
   ```

---

## 6. Storage, Volumes, and Data Persistence

Understanding where data is stored is critical for backups, disk sizing, and persistence:

| Data Type | Standalone Path | Production Path | Notes |
|---|---|---|---|
| **Database Data** | Volume `adult-hub-standalone_postgres_data` | External PostgreSQL storage | Contains catalog, channel state, performers, tags, migration history. |
| **Media & STRM** | Host `./standalone/media/` (in container `/media/adult`) | Host `${MEDIA_PATH}` (in container `/media/adult`) | `.strm` stream links, `.nfo` metadata sidecars, `.jpg` posters. Resolver/Sync have write access; Jellyfin has read-only access. |
| **Database Backups** | Host `./standalone/data/backups/` | Host `./data/backups/` | Compressed PostgreSQL snapshots (`.dump.gz` and `.sha256`). |
| **Telegram Session** | Host `./standalone/data/telegram.session` | Host `./data/telegram.session` | MTProto SQLite session token. Keep strictly confidential; never commit to version control. |
| **Jellyfin Config** | Volume `adult-hub-standalone_jellyfin_config` | Host `./jf-config/` | Jellyfin users, settings, and library database. |
| **Jellyfin Cache** | Volume `adult-hub-standalone_jellyfin_cache` | Host `./jf-cache/` | Extracted images, playback caches, transcodes. Ephemeral. |
| **Application Cache** | Host `./standalone/cache/` | Host `./data/cache/` | Worker heartbeat capabilities, temporary sync files. |

---

## 7. Backup and Database Restoration

### Automated Scheduled Backups

Scheduled backups execute through Adult-Hub's durable worker queue (`video_duplicate_jobs`, action `backup_create`) consumed by `adult-sync`. There is no standalone backup container or auxiliary background thread/daemon.

- **Mechanism**: The sync runner periodically checks due system schedules (`database-backup`) while waiting, during full sync passes (at safe checkpoints), and while the worker is active. When due, a `backup_create` job is atomically enqueued into the shared durable queue and claimed by the sync worker.
- **Archive Integrity**: Backups produce a verified PostgreSQL snapshot using `pg_dump` custom format, verified via `pg_restore --list`, compressed with gzip (`adulthub-job-<id>.dump.gz` or `adulthub-YYYYMMDD-HHMM.dump.gz`), and secured with a companion `.sha256` checksum sidecar.
- **Schedule & Slot Progression**: Governed by `BACKUP_DAILY_AT` (default `04:30 Europe/Warsaw`). The schedule slot only advances to the next day upon a successful backup. If a backup fails, the owed slot remains due with an automatic five-minute retry delay.
- **Idempotency & Crash Recovery**: If a worker process is interrupted and restarted, retrying the same job verifies and reuses the existing archive if valid, avoiding redundant `pg_dump` invocations.
- **Follow-up Stages**: When the `history-cleanup` stage is enabled in schedule settings, a durable `history_cleanup` job is automatically queued after a successful backup. A history cleanup failure is tracked in stage status and never causes another database dump.
- **Retention**: Prunes archives older than `BACKUP_KEEP_DAYS` (default: 7 days), removing both `.dump.gz` files and their `.sha256` sidecars together across legacy timestamp and job-id naming conventions. Any individual prune error is logged as a warning without failing the backup.
- **Location**: Snapshots are saved to `/data/backups` (`./standalone/data/backups/` in standalone, `./data/backups/` in production).

### Manual Database Snapshots

You can trigger an immediate database snapshot at any time:

```sh
# Via running sync container (recommended: includes pg_dump and database credentials)
# Standalone:
docker compose -p adult-hub-standalone -f compose.standalone.yml exec sync python3 backup_db.py

# Production:
docker compose exec sync python3 backup_db.py

# Via Admin System Schedules API / UI (queues a durable worker job):
curl -X POST http://127.0.0.1:8090/admin/api/v2/system-schedules/database-backup/run \
  -H "Origin: http://127.0.0.1:8090" \
  -H "Authorization: Bearer $ADMIN_WRITE_TOKEN"

# Via Backups API (independent snapshot):
curl -X POST http://127.0.0.1:8090/admin/api/v2/backups/create \
  -H "Origin: http://127.0.0.1:8090" \
  -H "Authorization: Bearer $ADMIN_WRITE_TOKEN"

# Direct host execution (requires pg_dump/pg_restore client binaries on host PATH and DATABASE_URL)
python3 backup_db.py --db-url "$DATABASE_URL" --dest-dir ./standalone/data/backups --keep 7
```

### Restoring from a Backup

The repository includes `restore_db.py` to restore snapshots safely. The tool verifies checksums, drops and recreates schema objects, and executes versioned migrations to ensure schema convergence.

```sh
# Standalone (backups located in ./standalone/data/backups/):
# Dry-run inspection of the latest backup
python3 restore_db.py --backup-dir ./standalone/data/backups --latest --dry-run

# Restore the latest backup
python3 restore_db.py --backup-dir ./standalone/data/backups --latest

# Restore a specific snapshot file
python3 restore_db.py --file ./standalone/data/backups/adulthub-20261101-0430.dump.gz

# Production (backups located in ./data/backups/):
python3 restore_db.py --latest --dry-run
python3 restore_db.py --latest
```

> [!CAUTION]
> Restoring a database drops and replaces existing catalog tables. Always stop `sync` (`docker compose stop sync`) before restoring to avoid concurrent write conflicts.

### Cutover Runbook: Transition from Standalone Backup Container

When upgrading an existing deployment to the queue-based backup architecture:

> [!NOTE]
> The commands below illustrate the standard `docker compose` stack (`docker-compose.yml`). For standalone repository deployments, pass `-p adult-hub-standalone -f compose.standalone.yml` (e.g., `docker compose -p adult-hub-standalone -f compose.standalone.yml stop -t 240 sync`).

1. **Graceful Scheduler Drain (Pre-Cutover)**:
   While still running the legacy Compose definition, signal the old standalone `backup` service and allow any active `pg_dump` operation to complete cleanly:
   ```sh
   docker compose stop -t 120 backup
   ```
2. **Remove Legacy Backup Container**:
   Remove only the retired `backup` container prior to swapping Compose files. Do **not** use `docker compose down -v` or prune volumes:
   ```sh
   docker compose rm -f backup
   ```
3. **Graceful Sync Drain**:
   Stop the `sync` service with sufficient shutdown grace period so in-flight tasks finish cleanly (matches the 4-minute `stop_grace_period`):
   ```sh
   docker compose stop -t 240 sync
   ```
4. **Update Code, Compose Configuration & Build**:
   Swap or pull the updated code and Compose configuration, then build and validate images:
   ```sh
   docker compose build resolver sync
   ```
5. **Apply Database Migration & Start Services**:
   Apply migrations (adds nullable `last_job_id` to `system_schedules`) and start updated services:
   ```sh
   docker compose up -d resolver sync
   ```
6. **Verify State & Recovery**:
   - Inspect sync logs: `docker compose logs -f sync`
   - Verify orphaned queued/processing schedule states from prior crashes are recovered.
   - Verify schedule status in Admin UI (`/admin/schedules`) or via API (`GET /admin/api/v2/system-schedules`).
   - Trigger a manual backup via Admin UI or API to verify end-to-end execution through the queue worker.
   - Verify daily artwork generation dispatch is active in the sync logs.

#### Rollback Outline
If a rollback is required:
1. Stop updated services: `docker compose stop -t 240 sync resolver`
2. Restore previous Compose configuration or git commit (restoring the `backup` service definition).
3. Bring up previous services: `docker compose up -d resolver sync backup`
4. This sequencing ensures old and new backup schedulers never execute simultaneously.

---

## 8. Jellyfin Integration

Adult Hub integrates with Jellyfin by generating media files directly onto the filesystem:
- **`.strm` files**: Small text files containing the stream URL pointing to the Adult Hub resolver endpoint.
- **`.nfo` files**: Kodi/Jellyfin-compatible XML metadata files containing titles, plots, tags, performers, and premiere dates.
- **`.jpg` files**: Thumbnails and cover artwork.

### Basic Setup (No API Key Required)

No Jellyfin API key is required to stream or browse your library.

1. Open Jellyfin in your browser:
   - Standalone loopback: [http://127.0.0.1:8096/](http://127.0.0.1:8096/)
   - Standalone LAN: `http://<YOUR_LAN_IP>:8096/`
2. Complete the initial Jellyfin setup wizard (create an admin user).
3. Add your media library:
   - Navigate to **Dashboard** -> **Libraries** -> **Add Media Library**.
   - **Content type**: Select **Movies**. *(Do not select Mixed Content or Music Videos).*
   - **Display name**: E.g., `Adult Hub`.
   - **Folders**: Click `+` and enter `/media/adult`.

### Recommended Jellyfin Library Configuration

To prevent Jellyfin from modifying or overwriting Adult Hub sidecars, adjust the library settings:

1. **Metadata Savers**: **Uncheck / disable "NFO" metadata saver**.
   - *Why*: Adult Hub generates and manages all `.nfo` files directly. Jellyfin mounts `/media/adult` as read-only (`:ro`), so Jellyfin's metadata saver cannot and should not write to it.
2. **Metadata Readers**: **Ensure "NFO" metadata reader is checked / enabled**.
   - *Why*: Jellyfin must read the `.nfo` sidecars generated by Adult Hub.
3. **Embedded Metadata**: You may uncheck "Embedded metadata" to prefer NFO tags over container headers.

### Optional API Key Integration

An API key enables advanced bi-directional communication between Adult Hub's sync worker and Jellyfin:
- **Instant Library Refreshes**: Automatically notifies Jellyfin when new videos or metadata sidecars are written, avoiding wait times for periodic library scans.
- **Performer Metadata Sync**: Pushes performer biographies and photos directly into Jellyfin's people database.

To enable:
1. In Jellyfin, go to **Dashboard** -> **Administration** -> **API Keys**.
2. Click `+` to generate a new key (name it `Adult Hub`).
3. Add the key to `.env`:
   - Standalone and production: `JELLYFIN_API_KEY=your_key_here`. The legacy name `STANDALONE_JELLYFIN_API_KEY` is still accepted as a fallback in standalone mode.
4. Restart services or run `make update` / `make deploy`.

---

## 9. Reverse Proxy and Network Configuration

When accessing Adult Hub from remote devices or outside your home network, use a reverse proxy (such as Caddy or Nginx) with HTTPS.

### Routing Requirements

- **Stream Playback & Artwork**: Route `/admin/play/*` and `/admin/api/thumb*` to the **Resolver** service (`http://127.0.0.1:8090`). These endpoints are needed by remote companion players.
- **Management Interface**: Restrict `/admin/*` access to trusted networks (LAN / VPN) or protect it with reverse proxy authentication. Do not publish `/admin/*` publicly without explicit access controls.
- **Jellyfin App**: Route all other requests (including root `/` and Jellyfin web app paths) to the **Jellyfin** service (`http://127.0.0.1:8096`).
- Ensure client-facing URLs in STRM files use HTTPS by setting `RESOLVER_PUBLIC_URL` in `.env` (e.g., `RESOLVER_PUBLIC_URL=https://hub.example.com`).

### Example Caddy Configuration

```caddy
hub.example.com {
    # Forward stream playback resolution and artwork to Resolver
    handle /admin/play/* {
        reverse_proxy 127.0.0.1:8090
    }
    handle /admin/api/thumb* {
        reverse_proxy 127.0.0.1:8090
    }

    # Restrict management interface to trusted local/VPN networks
    @trusted_admin {
        remote_ip 127.0.0.1 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16
        path /admin*
    }
    handle @trusted_admin {
        reverse_proxy 127.0.0.1:8090
    }

    # Block public access to management interface
    handle /admin* {
        respond "Access Denied" 403
    }

    # Forward all other traffic to Jellyfin
    handle {
        reverse_proxy 127.0.0.1:8096
    }
}
```

### Trusted Proxy Configuration

To ensure Uvicorn correctly identifies client IP addresses and protocols behind a reverse proxy, configure `RESOLVER_TRUSTED_PROXY_IPS` in `.env` (e.g., `RESOLVER_TRUSTED_PROXY_IPS=127.0.0.1,::1`).

---

## 10. Troubleshooting and Common Issues

### Port Conflict on 8090 or 8096

- **Symptom**: `scripts/setup.sh` warns that port 8090 or 8096 is already bound.
- **Cause**: Another service (such as an existing Jellyfin or alternative web service) is using the port.
- **Fix**: The standalone installer detects conflicts and suggests alternate ports (e.g. 8091 / 8097). Alternatively, specify overrides explicitly:
  ```sh
  scripts/setup.sh --resolver-port 8091 --jellyfin-port 8097
  ```

### Docker Daemon Permission Denied

- **Symptom**: `Cannot connect to the Docker daemon. Is Docker running?`
- **Cause**: Your user account does not have permission to access the Docker socket.
- **Fix**: Add your user to the `docker` group:
  ```sh
  sudo usermod -aG docker "$USER"
  newgrp docker
  ```

### "Cannot authenticate while resolver or sync service is running"

- **Symptom**: Installer reports an error during Telegram authentication check.
- **Cause**: SQLite session files (`telegram.session`) use exclusive file locking. If resolver or sync containers are running, interactive login is blocked to prevent database corruption.
- **Fix**: Stop application containers before re-authenticating:
  ```sh
  docker compose -p adult-hub-standalone -f compose.standalone.yml stop resolver sync
  make setup
  ```

### Telegram Authorization Fails or Expired

- **Symptom**: Admin UI shows Telegram unauthorized; sync logs report authentication failure.
- **Cause**: Session revoked in Telegram app or invalid API ID / Hash.
- **Fix**:
  1. Verify credentials at [my.telegram.org](https://my.telegram.org).
  2. If the session was revoked, remove the stale file:
     ```sh
     rm ./standalone/data/telegram.session
     ```
  3. Re-run `make setup` to log in again.

### Standalone Database Password Desynchronization

- **Symptom**: `Existing standalone database volume found with missing .env!`
- **Cause**: The named volume `adult-hub-standalone_postgres_data` exists, but `.env` was removed. PostgreSQL initializes credentials only once when the volume is created; generating a new `.env` would cause connection authentication failures.
- **Fix**:
  - If recovering: Restore the original `.env` file containing the matching `POSTGRES_PASSWORD`.
  - If starting fresh: Delete the orphaned volume:
    ```sh
    docker volume rm adult-hub-standalone_postgres_data
    make setup
    ```

### Videos Not Visible in Jellyfin

- **Symptom**: Channels are synced, but Jellyfin library is empty.
- **Checklist**:
  1. Confirm the library path in Jellyfin is exactly `/media/adult`.
  2. Verify files exist on the host in `./standalone/media/` (`ls ./standalone/media/`).
  3. In Jellyfin, go to **Dashboard** -> **Libraries** and click **Scan Library**.

---

## 11. Operational Limitations and Safety Bounds

- **Single Worker Topology**: Run only one sync worker against a single data mount. Worker coordination and job queues use Linux filesystem locks (`flock`) under `/data`. Multi-host distributed worker clusters sharing network shares (NFS/CIFS) are unsupported.
- **On-Demand Streaming**: Adult Hub does not download full video files to disk; it indexes metadata and streams chunks on demand over Telegram MTProto. If a message is deleted from Telegram or upstream servers throttle your IP, playback will stall or fail.
- **Telegram MTProto Limits**: Telegram applies strict flood wait limits. The sync service enforces delays between upstream requests (`category_fetch_delay_seconds: 2.5`) and bounds daily bandwidth usage (`TELEGRAM_FINGERPRINT_DAILY_BYTE_BUDGET`). Do not bypass these throttles.
- **Media Volume Deletion Safeguard**: The sync service refuses an upkeep pass that would delete more than 25% of a source's catalog entries in one run. This prevents an unmounted or replaced media volume from wiping your catalog.

---

## 12. Verification Scope and Runtime Limits

This documentation was written and verified against the repository implementation as follows:

- **Source Code Verification**: All documented behaviors, flags, and mechanics for `make setup`, `scripts/setup.sh`, `make update`, `scripts/update.sh`, `make deploy`, `compose.standalone.yml`, `docker-compose.yml`, and `Makefile` were verified directly from source code.
- **Executed Safe Checks**:
  - `make help`: Successfully verified Makefile targets and descriptions.
  - `make lint`: Verified Ruff correctness across Python code.
  - `git diff --check`: Verified no whitespace or git formatting issues.
- **Unverified Runtime Operations**:
  - A clean-host execution of `make setup`, `make deploy`, and `make update` was **not** run in this environment to protect running containers, active database volumes, and live runtime secrets.
  - No Docker containers were started, stopped, built, or modified during verification.
