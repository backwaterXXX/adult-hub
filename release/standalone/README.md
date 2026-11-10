# Adult Hub Standalone Deployment Bundle

This standalone release bundle provides a complete, preconfigured Adult Hub installation using official prebuilt container images. It requires no Git repository clone, compiler, Python virtual environment, or local container image builds.

## Prerequisites

- **Docker Engine** (24.0+) with the **Docker Compose v2** plugin (`docker compose`).
- **Telegram API Credentials**: obtain your `API ID` and `API Hash` from [my.telegram.org](https://my.telegram.org) under *API development tools*.
- Access to your Telegram account to receive the authentication code.

## Quick Start (3 Steps)

### 1. Configure Environment and Configuration

```sh
cp .env.example .env
cp config.example.yaml config.yaml
```

Edit `.env`:
- Set `POSTGRES_PASSWORD` to a secure password.
- Set `ADMIN_WRITE_TOKEN` to a secure secret token.
- Set `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` from [my.telegram.org](https://my.telegram.org).
- (Optional) To access Adult Hub from other devices on your home network, set `RESOLVER_BIND_IP` and `JELLYFIN_BIND_IP` to your host's LAN IP (e.g. `192.168.1.100`), and set `STREAM_PUBLIC_URL_TELEGRAM=http://192.168.1.100:8090`.

### 2. Pull Images and Authenticate Telegram

Pull the published images:
```sh
docker compose pull
```

Run one-time interactive Telegram login to generate the MTProto session:
```sh
docker compose run --rm telegram-auth
```
Follow the interactive prompts to enter your phone number, the verification code sent to your Telegram app, and your 2FA password (if enabled). The session is saved to `./data/telegram.session`.

### 3. Start the Stack

```sh
docker compose up -d
```

Compose starts the database, executes schema migrations, and launches Jellyfin, the Resolver API, the Sync worker, and automated database backups.

## Access Endpoints

- **Adult Hub Admin Panel:** [http://127.0.0.1:8090/admin/](http://127.0.0.1:8090/admin/) (or your configured LAN IP and port). Enter your `ADMIN_WRITE_TOKEN` in the panel.
- **Jellyfin Media Server:** [http://127.0.0.1:8096/](http://127.0.0.1:8096/) (or your configured LAN IP and port). Complete Jellyfin's first-run setup wizard and add a library pointing to `/media/adult`.

## Updating

To update all services to the latest published images:
```sh
docker compose pull
docker compose up -d
```

## Useful Commands

- View service status: `docker compose ps`
- Follow logs: `docker compose logs -f`
- Stop all services safely: `docker compose down`
- Verify Telegram session: `docker compose run --rm telegram-auth check`
