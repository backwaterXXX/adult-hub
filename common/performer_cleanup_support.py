"""Helpers shared by the one-off performer cleanup scripts under ``scripts/``.

Script-level settings (``BASE_DIR``, the ``sync`` module, the backup directory) are
passed in as arguments. The scripts keep their own names
for these helpers as thin wrappers, so module globals are read at call time.
"""

from __future__ import annotations

import gzip
import subprocess
import time
from pathlib import Path

from common import db


def docker_pg_dump_backup(backup_dir: Path, prefix: str) -> Path:
    """Dump the database with ``docker exec postgres pg_dump`` into a gzip file."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_file = backup_dir / f"{prefix}-{stamp}.sql.gz"
    res = subprocess.run(
        ["docker", "exec", "postgres", "pg_dump", "-U", "adulthub", "adulthub"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if res.returncode != 0:
        raise RuntimeError(f"docker pg_dump failed: {res.stderr.decode()[:300]}")
    with gzip.open(backup_file, "wb") as f:
        f.write(res.stdout)
    return backup_file


def create_db_backup(db_url: str | None, backup_dir: Path, prefix: str) -> Path:
    """Back up with ``db.pg_backup``; fall back to the docker pg_dump route on RuntimeError."""
    try:
        return db.pg_backup(db_url, backup_dir, prefix=prefix)
    except RuntimeError:
        return docker_pg_dump_backup(backup_dir, prefix)
