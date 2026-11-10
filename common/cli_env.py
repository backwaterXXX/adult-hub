"""Environment helpers shared by the root CLI scripts (migrate_db, restore_db)."""

from __future__ import annotations

import os
import socket
from pathlib import Path


def load_dotenv_database_url(base_dir: Path) -> None:
    """Set DATABASE_URL from ``base_dir/.env`` when the environment lacks it."""
    if os.getenv("DATABASE_URL"):
        return
    env_file = base_dir / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == "DATABASE_URL":
            os.environ.setdefault("DATABASE_URL", value.strip())


def normalize_host_url(url: str) -> str:
    """Map a Compose-only ``postgres`` host to localhost when it does not resolve."""
    if "@postgres:" in url or "@postgres/" in url:
        try:
            socket.gethostbyname("postgres")
        except Exception:
            url = url.replace("@postgres:", "@127.0.0.1:").replace("@postgres/", "@127.0.0.1/")
    return url
