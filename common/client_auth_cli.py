"""Local CLI command for secure initial owner bootstrapping and operator password management.

Usage:
    python3 -m common.client_auth_cli bootstrap-owner [--username USERNAME]
    python3 -m common.client_auth_cli update-password --username USERNAME

Passwords are NEVER passed as command-line arguments or logged to output.
Prompts interactively using getpass, or reads from stdin if piped.
"""

from __future__ import annotations

import argparse
import getpass
import sys

from common import db
from common.client_auth.schema import ensure_auth_schema
from common.client_auth.service import bootstrap_owner, update_user_password


def run_bootstrap_owner(username: str, password: str | None = None) -> int:
    if not password:
        if sys.stdin.isatty():
            pwd1 = getpass.getpass("Enter password for owner account: ")
            pwd2 = getpass.getpass("Confirm password: ")
            if pwd1 != pwd2:
                print("Error: Passwords do not match.", file=sys.stderr)
                return 1
            password = pwd1
        else:
            # Read single line from stdin for non-interactive test/container execution
            password = sys.stdin.readline().rstrip("\r\n")

    if not password:
        print("Error: Password cannot be empty.", file=sys.stderr)
        return 1

    try:
        with db.connection() as conn:
            ensure_auth_schema(conn)
            user = bootstrap_owner(conn, username, password)
            print(f"Successfully bootstrapped owner account '{user['username']}' (ID: {user['id']}).")
            return 0
    except Exception as exc:
        print(f"Error bootstrapping owner: {exc}", file=sys.stderr)
        return 1


def run_update_password(username: str, password: str | None = None) -> int:
    username = (username or "").strip()
    if not username:
        print("Error: Username cannot be empty.", file=sys.stderr)
        return 1

    if not password:
        if sys.stdin.isatty():
            pwd1 = getpass.getpass(f"Enter new password for '{username}': ")
            pwd2 = getpass.getpass("Confirm new password: ")
            if pwd1 != pwd2:
                print("Error: Passwords do not match.", file=sys.stderr)
                return 1
            password = pwd1
        else:
            # Read single line from stdin for non-interactive test/container execution
            password = sys.stdin.readline().rstrip("\r\n")

    if not password:
        print("Error: Password cannot be empty.", file=sys.stderr)
        return 1

    try:
        with db.connection() as conn:
            ensure_auth_schema(conn)
            user = update_user_password(conn, username, password)
            print(f"Successfully updated password for account '{user['username']}' (ID: {user['id']}).")
            return 0
    except Exception as exc:
        print(f"Error updating password: {exc}", file=sys.stderr)
        return 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Adult Hub Client API Administration CLI",
        prog="python3 -m common.client_auth_cli",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    bootstrap_parser = subparsers.add_parser(
        "bootstrap-owner",
        help="Securely bootstrap the initial owner account (single-use)",
    )
    bootstrap_parser.add_argument(
        "--username",
        default="owner",
        help="Username for the owner account (default: 'owner')",
    )

    update_parser = subparsers.add_parser(
        "update-password",
        help="Securely update password for an existing active user account",
    )
    update_parser.add_argument(
        "--username",
        required=True,
        help="Username of the existing account to update",
    )

    args = parser.parse_args()
    if args.subcommand == "bootstrap-owner":
        exit_code = run_bootstrap_owner(args.username)
        sys.exit(exit_code)
    elif args.subcommand == "update-password":
        exit_code = run_update_password(args.username)
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
