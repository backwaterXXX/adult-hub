"""Daily PostgreSQL snapshot for Adult-Hub.

Uses ``pg_dump`` (custom format, safe while sync holds connections),
verifies the dump by listing its table of contents, and prunes snapshots
older than --keep days.

Scheduling:
    The ``backup`` compose service polls the saved Admin cron setting. Legacy
    host cron calls to ``backup_scheduler.py --once`` remain safe: they run only
    when the saved setting is due. ``--force`` runs an immediate backup.

Direct invocation requires ``DATABASE_URL`` (or --db-url) and the
``pg_dump``/``pg_restore`` client binaries on PATH.

Exit code 0 = snapshot written and verified, 1 = failure.
"""

import argparse
import gzip
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).parent


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Adult-Hub PostgreSQL backup")
    ap.add_argument("--db-url", default=os.getenv("DATABASE_URL", ""))
    ap.add_argument("--dest-dir", default=str(BASE_DIR / "data" / "backups"))
    ap.add_argument("--keep", type=int, default=int(os.getenv("BACKUP_KEEP_DAYS", "7")))
    return ap.parse_args()


def _password(conninfo: str) -> str:
    match = urlparse(conninfo)
    return match.password or ""


def _prune_old_snapshots(dest_dir: Path, dest: Path, keep: int) -> int:
    cutoff = time.time() - max(keep, 1) * 86400
    pruned = 0
    for old in dest_dir.glob("adulthub-*.dump.gz"):
        if old == dest:
            continue
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
                old.with_name(old.name + ".sha256").unlink(missing_ok=True)
                pruned += 1
        except OSError as e:
            print(f"prune warning for {old.name}: {e}", file=sys.stderr)
    return pruned


def main() -> int:
    args = parse_args()
    conninfo = (args.db_url or "").strip()
    if not conninfo:
        print("DATABASE_URL is not set (use --db-url)", file=sys.stderr)
        return 1
    pg_dump = shutil.which("pg_dump")
    pg_restore = shutil.which("pg_restore")
    if not pg_dump or not pg_restore:
        print("pg_dump/pg_restore not found on PATH", file=sys.stderr)
        return 1
    dest_dir = Path(args.dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    stamp = time.strftime("%Y%m%d-%H%M")
    dest = dest_dir / f"adulthub-{stamp}.dump.gz"
    raw_tmp = dest_dir / f".{dest.name}.{uuid.uuid4().hex}.dump"
    gzip_tmp = dest_dir / f".{dest.name}.{uuid.uuid4().hex}.tmp"
    sidecar_tmp = dest.with_name(dest.name + f".{uuid.uuid4().hex}.sha256.tmp")
    try:
        env = dict(os.environ, PGPASSWORD=_password(conninfo))
        proc = subprocess.run(
            [pg_dump, "--dbname", conninfo, "--format", "custom",
             "--no-owner", "--no-privileges", "--file", str(raw_tmp)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env, check=False,
        )
        if proc.returncode != 0:
            print(f"pg_dump failed: {proc.stderr.decode()[:500]}", file=sys.stderr)
            return 1
        with raw_tmp.open("rb") as src, gzip.open(gzip_tmp, "wb") as out:
            shutil.copyfileobj(src, out, length=1024 * 1024)
        verify = subprocess.run(
            [pg_restore, "--list", str(raw_tmp)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if verify.returncode != 0:
            print(f"dump verification failed: {verify.stderr.decode()[:300]}", file=sys.stderr)
            return 1
        tables = len(re.findall(rb"\sTABLE DATA ", verify.stdout))
        os.replace(gzip_tmp, dest)
        digest = hashlib.sha256()
        with dest.open("rb") as src:
            for chunk in iter(lambda: src.read(1024 * 1024), b""):
                digest.update(chunk)
        sidecar = dest.with_name(dest.name + ".sha256")
        sidecar_tmp.write_text(f"{digest.hexdigest()}  {dest.name}\n", encoding="ascii")
        os.replace(sidecar_tmp, sidecar)
    except Exception as e:
        print(f"backup failed: {e}", file=sys.stderr)
        dest.unlink(missing_ok=True)
        return 1
    finally:
        raw_tmp.unlink(missing_ok=True)
        gzip_tmp.unlink(missing_ok=True)
        sidecar_tmp.unlink(missing_ok=True)

    pruned = _prune_old_snapshots(dest_dir, dest, args.keep)
    size_kb = dest.stat().st_size // 1024
    print(f"{time.strftime('%Y-%m-%d %H:%M')}: backup {dest.name} "
          f"({size_kb} kB, {tables} tables, pg_restore --list ok, pruned {pruned})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
