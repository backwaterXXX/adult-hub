#!/usr/bin/env python3
"""Database migrations and schema recreation CLI for Adult Hub.

Usage:
    python3 migrate_db.py status [--db-url URL]
    python3 migrate_db.py apply [--db-url URL] [--target N]
    python3 migrate_db.py recreate [--db-url URL] [--yes]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

from common.cli_env import load_dotenv_database_url
from common.cli_env import normalize_host_url as _normalize_host_url
from common.migrations import (
    apply_migrations,
    get_migration_status,
    recreate_database,
)


def _resolve_url(args: argparse.Namespace) -> str:
    load_dotenv_database_url(BASE_DIR)
    url = (args.db_url or os.getenv("DATABASE_URL") or "").strip()
    if not url:
        print("DATABASE_URL is not configured (use --db-url or set DATABASE_URL)", file=sys.stderr)
        sys.exit(1)
    return _normalize_host_url(url)


def cmd_status(args: argparse.Namespace) -> int:
    url = _resolve_url(args)
    status = get_migration_status(url)

    print("Adult Hub Database Migrations Status")
    print("=" * 60)
    print(f"Target Database: {url.split('@')[-1] if '@' in url else url}")
    print(f"Current Version: {status.current_version}")
    print(f"Status:          {'UP TO DATE' if status.is_up_to_date else f'{len(status.pending)} PENDING'}")
    print("-" * 60)
    print(f"{'Ver':<5} | {'Migration Name':<35} | {'Applied At'}")
    print("-" * 60)

    rows = [
        (record.version, record.name, str(record.applied_at)[:19])
        for record in status.applied
    ]
    rows.extend(
        (migration.version, migration.name, "[PENDING]")
        for migration in status.pending
    )
    for version, name, applied_at in sorted(rows):
        print(f"{version:<5} | {name:<35} | {applied_at}")

    print("=" * 60)
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    url = _resolve_url(args)
    target = getattr(args, "target", None)
    print(f"Applying migrations to database... (target: {target if target is not None else 'latest'})")
    applied = apply_migrations(url, target_version=target)
    if not applied:
        print("Database schema is already up to date. No migrations applied.")
    else:
        print(f"Successfully applied {len(applied)} migration(s):")
        for rec in applied:
            print(f"  + [{rec.version:04d}] {rec.name} (at {rec.applied_at})")
    return 0


def cmd_recreate(args: argparse.Namespace) -> int:
    url = _resolve_url(args)
    if not args.yes:
        confirm = input(
            f"WARNING: This will DROP ALL TABLES and recreate the schema on {url}.\n"
            "Are you sure you want to proceed? [y/N]: "
        )
        if confirm.strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return 1

    print("Recreating database schema from scratch...")
    applied = recreate_database(url)
    print(f"Successfully recreated database and applied {len(applied)} migration(s):")
    for rec in applied:
        print(f"  + [{rec.version:04d}] {rec.name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("--db-url", default="", help="PostgreSQL connection URL (default: DATABASE_URL env)")

    parser = argparse.ArgumentParser(description="Adult Hub database migration and recreation manager", parents=[parent])
    subparsers = parser.add_subparsers(dest="command", required=True)

    # status
    p_status = subparsers.add_parser("status", help="Show current migration status", parents=[parent])
    p_status.set_defaults(func=cmd_status)

    # apply
    p_apply = subparsers.add_parser("apply", help="Apply pending migrations", parents=[parent])
    p_apply.add_argument("--target", type=int, default=None, help="Target migration version limit")
    p_apply.set_defaults(func=cmd_apply)

    # recreate
    p_recreate = subparsers.add_parser("recreate", help="Drop all tables and re-apply all migrations", parents=[parent])
    p_recreate.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    p_recreate.set_defaults(func=cmd_recreate)

    parsed = parser.parse_args(argv)
    return parsed.func(parsed)


if __name__ == "__main__":
    sys.exit(main())
