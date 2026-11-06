# Adult Hub

**Adult Hub** turns Telegram channels into a searchable video library for gay adult content. Browse titles, performers, and artwork in [Jellyfin](https://jellyfin.org/) or use the companion Android and desktop players.

Videos stay on Telegram and stream on demand. Adult Hub stores the catalog, stream links, metadata, and artwork locally. The web admin panel lets you add sources, monitor indexing, organize performers, review duplicates, and manage background tasks.

> **Responsible use.** Adult Hub is intended for adults and for content you are legally allowed to access and organize. It indexes links from sources you configure. You are responsible for complying with their terms of service and applicable laws.

## Companion Players

- **[Adult Hub Player](https://github.com/backwaterXXX/adult-hub-player)** — native Android app for phones and Android TV.
- **[Adult Hub Desktop](https://github.com/backwaterXXX/adult-hub-desktop)** — desktop player for Windows and Linux.

## How It Works

1. Adult Hub indexes your configured Telegram channels and generates stream links, metadata, and artwork.
2. Jellyfin displays the library with titles and performer information.
3. Choose a video to stream it from Telegram through the resolver to your TV, phone, or browser.

```
PHASE 1: Library Indexing (Background)
──────────────────────────────────────────────────────────────────────────────────
  ┌───────────────────┐    1. Periodic crawl     ┌──────────────┐
  │ Telegram Channels │ ───────────────────────► │ Sync Service │
  └───────────────────┘                          └──────┬───────┘
                                                        │ 2. Writes: .strm (links)
                                                        │    + .nfo + thumbnails
                                                        ▼
                                                 ┌────────────────────────┐
                                                 │ Jellyfin Media Library │
                                                 └────────────────────────┘

PHASE 2: On-Demand Playback (Real-time)
──────────────────────────────────────────────────────────────────────────────────
  ┌────────────────────────┐   3. Presses Play   ┌─────────────────┐
  │ Jellyfin Media Library │ ──────────────────► │ Playback Client │ (TV/phone/web)
  └────────────────────────┘   (loads .strm URL) └────┬───────▲────┘
                                                      │       │
                                 4. HTTP GET request  │       │ 6. HTTP 206 Stream
                                    (with Range)      │       │    (Partial Content)
                                                      ▼       │
  ┌────────────────────────┐ 5. On-demand stream ┌────────────┴─────┐
  │    Telegram Servers    │ ◄─────────────────► │ Resolver Service │
  └────────────────────────┘      (MTProto)      └──────────────────┘
```

## Quick Start

The fastest way to install Adult Hub is using prebuilt container images with Docker Compose. This path requires no Git repository clone, compiler, or local image builds.

> **Prebuilt Image Availability**: Official container images (`ghcr.io/backwaterxxx/adult-hub-resolver` and `ghcr.io/backwaterxxx/adult-hub-sync`) are published automatically to the GitHub Container Registry as part of tagged releases.

### Prerequisites

- **Docker** with the **Compose v2** plugin (`docker compose`) and a running Docker daemon.
- **Telegram API credentials**: obtain your API ID and API Hash from [my.telegram.org](https://my.telegram.org), under *API development tools*.
- Access to your Telegram account to receive the login code, plus your 2FA password if enabled.

### Installation

1. Download and extract the standalone release bundle:

```sh
mkdir adult-hub && cd adult-hub
curl -sSL https://github.com/backwaterXXX/adult-hub/releases/latest/download/adult-hub-standalone.tar.gz | tar -xz
```

2. Copy configuration templates and add credentials:

```sh
cp .env.example .env
cp config.example.yaml config.yaml
```

Edit `.env` to configure your passwords, tokens, and Telegram API credentials (`TELEGRAM_API_ID` and `TELEGRAM_API_HASH`).

3. Pull published images, authenticate Telegram interactively, and start the stack:

```sh
docker compose pull
docker compose run --rm telegram-auth
docker compose up -d
```

`telegram-auth` prompts for your phone number, code, and optional 2FA password, and writes an authorized session token to `./data/telegram.session`. Startup automatically applies database migrations and brings up PostgreSQL, Jellyfin, Resolver, Sync, and Backup services.

To access the stack from other devices on your local network (LAN), set `RESOLVER_BIND_IP` and `JELLYFIN_BIND_IP` to your host's LAN IP, and set `STANDALONE_STREAM_PUBLIC_URL_TELEGRAM=http://<YOUR_LAN_IP>:8090` in `.env`.

### Alternative: Automated Source Installer

If you are developing locally, working from a Git checkout, or installing before prebuilt images are published for your commit, use the interactive source installer:

```sh
git clone https://github.com/backwaterXXX/adult-hub.git
cd adult-hub
make setup
```

The installer verifies prerequisites, prompts for binding modes, generates credentials, and builds images from source (`make setup --source-build`). See [INSTALL.md](INSTALL.md) for full options.

### First Steps

1. Open the **Admin URL** ([http://127.0.0.1:8090/admin/](http://127.0.0.1:8090/admin/) or your configured LAN address). Enter the `ADMIN_WRITE_TOKEN` from `.env` in the panel to enable management actions.
2. In **Sources**, add your first Telegram channel, for example `@channel_name`.
3. Open **Jellyfin** ([http://127.0.0.1:8096/](http://127.0.0.1:8096/) or your configured LAN address) and complete its setup wizard. Add a **Movies** library pointing to `/media/adult`, with a display name such as **Adult**.
4. In the Jellyfin library settings, keep the **NFO reader** enabled and disable the **NFO metadata saver**. Adult Hub generates the sidecars; Jellyfin mounts the media directory read-only.

Playback devices must be able to reach the resolver address.

To update prebuilt standalone services and apply migrations:

```sh
docker compose pull
docker compose up -d
```

See [INSTALL.md](INSTALL.md) for installation options, existing PostgreSQL infrastructure, updates, backups, reverse proxies, and troubleshooting.

## Admin Panel

The Admin interface is available at `/admin/` and `/admin/v2/`.

- **Home:** overview of the library and current activity.
- **Library:** search videos, inspect availability, edit metadata with previews, review duplicates and metadata suggestions, and restore quarantined items.
- **Sources:** manage Telegram channels, check access, inspect sync history, and request synchronization.
- **Organize and Performers:** organize collections and review performer matches and duplicate suggestions. Hiding items retains their metadata, history, and undo; quarantine does not delete Telegram media.
- **Tasks:** follow queued work and inspect results after reloads or worker restarts.
- **System:** inspect service status, manage database backups, and request maintenance.
- **Schedules:** review, enable, disable, and edit automation.

Management actions use the configured admin write token and browser Origin checks. Keep the panel on a trusted network or configure access through a reverse proxy as described in [INSTALL.md](INSTALL.md). Classic Admin and former Admin V1 API routes are retired.

## Architecture

- **Resolver (`resolver/`):** serves the Admin UI, management API, artwork, and on-demand Telegram streams.
- **Sync (`sync/`):** indexes channels, maintains PostgreSQL records, generates STRM/NFO files and artwork, and processes background work.
- **Shared code (`common/`):** configuration validation, database access, versioned migrations, durable task queue, and collection management.
- **PostgreSQL:** stores the catalog and task queue.
- **Jellyfin:** presents the generated media library; companion players provide additional playback clients.
- **Backup service:** schedules verified PostgreSQL snapshots.

Telegram is the only media source provider. The standalone installer creates the initial source configuration; use the Admin panel to add channels. Provider IDs and media filenames remain stable across metadata updates.

## Development & Testing

From the repository root:

```sh
make install-dev
make lint
make test
```

`make test` requires `TEST_DATABASE_URL` pointing to a separate disposable PostgreSQL database, supplied through the shell or repository `.env`. It runs the full Python test profile, browser contracts, Admin JavaScript syntax checks, and the proxy-origin contract. Never use the production database for tests. Backup restore tests additionally require a separate `BACKUP_RESTORE_DATABASE_URL` with permission to create and drop their disposable database.

Runtime dependencies are pinned in `resolver/requirements.lock` and `sync/requirements.lock`; `requirements-dev.lock` adds the test and lint tools. After changing dependency inputs, run `make lock` with `uv` installed and include the updated lockfiles.

### Isolated Resolver Development

```sh
make dev
make dev-logs
make dev-down
```

`make dev` starts a source-reloading resolver at `http://127.0.0.1:18090` with a disposable PostgreSQL database and runs migrations before startup. It uses `config.example.yaml` and does not use production configuration, media, Telegram sessions, or runtime data. Database, `/data`, and `/cache` state are ephemeral.

Run individual checks with `make python-test`, `make ui-contracts`, `make js-check`, or `make proxy-origin`. Use `make help` for all targets.

### Releases

`VERSION` defines the application version reported by the resolver and sync service. `make deploy` uses that value for image tags and the OCI version label. Keep release tags (`vX.Y.Z`), `VERSION`, source commits, and resolver/sync images aligned for deployment and rollback. Deploy compatible resolver and sync builds together.
