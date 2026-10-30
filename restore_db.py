#!/usr/bin/env python3
"""Restore Adult-Hub PostgreSQL database from a backup snapshot.

Companion to backup_db.py. Restores custom-format PostgreSQL dumps
(adulthub-*.dump.gz or .dump), verifies contents, drops and recreates
schema objects, and executes versioned migrations to ensure schema convergence.

Usage:
    python3 restore_db.py [--file /path/to/snapshot.dump.gz] [--db-url URL]
    python3 restore_db.py --latest
    python3 restore_db.py --dry-run [--file ...]
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from common import db


def _load_dotenv() -> None:
    if os.getenv("DATABASE_URL"):
        return
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == "DATABASE_URL":
            os.environ.setdefault("DATABASE_URL", value.strip())


def _normalize_host_url(url: str) -> str:
    if "@postgres:" in url or "@postgres/" in url:
        try:
            import socket
            socket.gethostbyname("postgres")
        except Exception:
            url = url.replace("@postgres:", "@127.0.0.1:").replace("@postgres/", "@127.0.0.1/")
    return url


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    _load_dotenv()
    ap = argparse.ArgumentParser(description="Adult-Hub PostgreSQL database restoration")
    ap.add_argument("--db-url", default=os.getenv("DATABASE_URL", ""),
                    help="Target PostgreSQL connection URL (default: DATABASE_URL env)")
    ap.add_argument("-f", "--file", default="",
                    help="Path to .dump.gz or .dump file (default: latest snapshot in backup-dir)")
    ap.add_argument("--backup-dir", default=str(BASE_DIR / "data" / "backups"),
                    help="Directory containing backup dumps (default: data/backups)")
    ap.add_argument("--latest", action="store_true",
                    help="Pick the most recent backup dump in backup-dir")
    ap.add_argument("--no-clean", dest="clean", action="store_false", default=True,
                    help="Do not drop existing objects before restoring")
    ap.add_argument("--dry-run", action="store_true",
                    help="Verify dump table of contents without writing to database")
    ap.add_argument("-y", "--yes", action="store_true",
                    help="Skip confirmation prompt")
    ap.add_argument("--skip-migrations", action="store_true",
                    help="Skip running apply_migrations() after restore")
    return ap.parse_args(argv)


def find_latest_dump(backup_dir: Path) -> Path | None:
    if not backup_dir.exists():
        return None
    # Checksum sidecars and in-progress temp files share the prefix; only real
    # archives may be selected, whatever their timestamps say.
    candidates = [
        path
        for pattern in ("adulthub-*.dump.gz", "adulthub-*.dump")
        for path in backup_dir.glob(pattern)
        if path.is_file() and not path.is_symlink()
    ]
    dumps = sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)
    return dumps[0] if dumps else None


def verify_dump_checksum(dump: Path) -> bool:
    """Check the .sha256 sidecar written by backup_db.py, when one exists.

    A missing sidecar is allowed with a warning: older backups predate the
    checksum and the pg_restore table-of-contents check still runs. A sidecar
    that disagrees with the archive aborts the restore.
    """
    sidecar = dump.with_name(dump.name + ".sha256")
    if not sidecar.is_file():
        print(f"WARNING: {dump.name} has no SHA-256 sidecar; skipping the integrity check.",
              file=sys.stderr)
        return True
    try:
        checksum_line = sidecar.read_text(encoding="ascii").strip().split()
        expected_hash, expected_name = checksum_line[0], checksum_line[1]
    except (OSError, UnicodeError, IndexError):
        print(f"Dump verification FAILED: {sidecar.name} is not a valid checksum.", file=sys.stderr)
        return False
    if expected_name != dump.name or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        print(f"Dump verification FAILED: {sidecar.name} does not describe {dump.name}.",
              file=sys.stderr)
        return False
    digest = hashlib.sha256()
    try:
        with dump.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        print(f"Dump verification FAILED: could not read {dump.name}: {exc}", file=sys.stderr)
        return False
    if digest.hexdigest() != expected_hash:
        print(f"Dump verification FAILED: checksum mismatch for {dump.name}.", file=sys.stderr)
        return False
    print(f"Checksum verified for {dump.name}.")
    return True


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    conninfo = _normalize_host_url((args.db_url or "").strip())
    if not conninfo and not args.dry_run:
        print("DATABASE_URL is not set (use --db-url)", file=sys.stderr)
        return 1

    pg_restore_bin = shutil.which("pg_restore")
    if not pg_restore_bin:
        print("pg_restore not found on PATH", file=sys.stderr)
        return 1

    backup_dir = Path(args.backup_dir)
    target_file: Path | None = None

    if args.file:
        target_file = Path(args.file)
    else:
        target_file = find_latest_dump(backup_dir)

    if not target_file or not target_file.exists():
        print(f"Dump file not found: {target_file or '(no dumps in ' + str(backup_dir) + ')'}", file=sys.stderr)
        return 1

    print(f"Selected dump file: {target_file} ({target_file.stat().st_size:,} bytes)")

    if not verify_dump_checksum(target_file):
        return 1

    if args.dry_run:
        import subprocess
        import gzip
        import tempfile

        tmp_path = None
        if target_file.name.endswith(".gz"):
            with tempfile.NamedTemporaryFile(suffix=".dump", delete=False) as tmp:
                tmp_path = tmp.name
            with gzip.open(target_file, "rb") as src, open(tmp_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
            verify_file = tmp_path
        else:
            verify_file = str(target_file)

        try:
            res = subprocess.run(
                [pg_restore_bin, "--list", verify_file],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            if res.returncode != 0:
                print(f"Dump verification FAILED: {res.stderr.decode()[:400]}", file=sys.stderr)
                return 1
            print("Dump verification PASSED. Table of contents:")
            lines = res.stdout.decode(errors="replace").splitlines()
            for line in lines[:30]:
                print(f"  {line}")
            if len(lines) > 30:
                print(f"  ... and {len(lines) - 30} more entries")
            return 0
        finally:
            if tmp_path:
                Path(tmp_path).unlink(missing_ok=True)

    if not args.yes:
        confirm = input(
            f"WARNING: This will RESTORE into {conninfo.split('@')[-1] if '@' in conninfo else conninfo} "
            f"from {target_file.name} (clean={args.clean}).\n"
            "Are you sure you want to proceed? [y/N]: "
        )
        if confirm.strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return 1

    print("Restoring database...")
    try:
        db.pg_restore(
            dump_path=target_file,
            db_url=conninfo,
            clean=args.clean,
            run_migrations=not args.skip_migrations,
        )
        print("Database restored successfully.")
        return 0
    except Exception as exc:
        print(f"Restoration failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
