# Agent notes

The core is Telegram-only: Telegram support, an HTTP resolver, and a shared PostgreSQL/NFO/STRM library.

- `common/config_loader.py` is the only configuration loader. Both images install `common` into `site-packages`.
- `resolver/providers/__init__.py` registers Telegram only.
- `sync/sync.py` runs the Telegram flow only.
- Retention may affect only registered, enabled sources synchronized successfully in the current run. Automatic upkeep (orphan sweep, retention) additionally refuses a pass that would remove more than a quarter of a source's entries, so a missing or replaced media volume cannot delete the catalog; explicit operator purges are not limited that way. Preserve historical IDs, the database schema, and media filenames.
- A restart must not cut a sync mid-channel: `sync/sync_shutdown.py` turns the child's SIGTERM into a stop request that orchestration honours between Telegram channels, the runner waits `sync.shutdown_grace_seconds` for that drain, and the `sync` service's compose `stop_grace_period` must stay larger. A pass stopped this way records `interrupted` in the sync history and leaves any durable sync job for the next start to requeue.
- Admin UI and management APIs are V2 only. `resolver/admin.py` serves the V2 page at `/admin/` and `/admin/v2/`; retired V1 UI/API routes and `/admin/classic/` must not be reintroduced. Keep shared resolver/catalog APIs and the Jellyfin client pHash integration under V2 routes. V2 feature routers live in `admin_v2_*.py`; data and access helpers live in `admin_data.py`, `admin_services.py`, and `admin_security.py`.
- Run migration tests against a disposable PostgreSQL test database (`TEST_DATABASE_URL`) and copies of media files. `config.yaml`, `.env`, sessions, the database, and runtime data are local.
- A fresh database applies one tracked migration: version `1` runs `common/migrations/schema_baseline.sql`. Later versions (`10`–`55`) form the compatibility chain that only upgrades databases already carrying that history, so `common/migrations/legacy.py` must keep the chain in apply order. A new schema change is a numbered migration after the baseline plus the same end-state added to `schema_baseline.sql`; `tests/test_migrations.py` compares the two and fails if the baseline is not updated.
- `VERSION` is the only place a release number is recorded. `make deploy` exports it as `APP_VERSION` for image tags and the OCI label, and `common/app_version.py` reads `VERSION` itself, so neither `.env` nor `.env.example` pins the version and a release bump touches `VERSION` alone.
- `docs/` is a private, local-only directory intentionally excluded from git and sync. Do not track it or remove the ignore rule.
- Public tests: `python3 -m unittest discover -s tests`.
- A standard clone must build both images and run Telegram.
