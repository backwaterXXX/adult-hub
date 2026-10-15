# Agent notes

The core is Telegram-only: Telegram support, an HTTP resolver, and a shared PostgreSQL/NFO/STRM library.

- `common/config_loader.py` is the only configuration loader. Both images install `common` into `site-packages`.
- `resolver/providers/__init__.py` registers Telegram only.
- `sync/sync.py` runs the Telegram flow only.
- Retention may affect only registered, enabled sources synchronized successfully in the current run. Preserve historical IDs, the database schema, and media filenames.
- Admin UI and management APIs are V2 only. `resolver/admin.py` serves the V2 page at `/admin/` and `/admin/v2/`; retired V1 UI/API routes and `/admin/classic/` must not be reintroduced. Keep shared resolver/catalog APIs and the Jellyfin client pHash integration under V2 routes. V2 feature routers live in `admin_v2_*.py`; data and access helpers live in `admin_data.py`, `admin_services.py`, and `admin_security.py`.
- Run migration tests against a disposable PostgreSQL test database (`TEST_DATABASE_URL`) and copies of media files. `config.yaml`, `.env`, sessions, the database, and runtime data are local.
- Public tests: `python3 -m unittest discover -s tests`.
- A standard clone must build both images and run Telegram.
