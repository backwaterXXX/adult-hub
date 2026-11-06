"""CLI entrypoint for standalone Telegram authentication and session checks."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from .telegram_session import (
    interactive_login,
    is_session_authorized,
    validate_api_credentials,
)

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Adult Hub Telegram Authentication Helper")
    parser.add_argument(
        "action",
        choices=["check", "auth"],
        default="auth",
        nargs="?",
        help="Action: 'check' session validity or 'auth' interactively",
    )
    parser.add_argument(
        "--session-path",
        default=os.getenv("TELEGRAM_SESSION_PATH", "/data/telegram.session"),
        help="Path to Telegram SQLite session file",
    )
    parser.add_argument(
        "--api-id",
        default=os.getenv("TELEGRAM_API_ID"),
        help="Telegram API ID",
    )
    parser.add_argument(
        "--api-hash",
        default=os.getenv("TELEGRAM_API_HASH"),
        help="Telegram API Hash",
    )
    args = parser.parse_args(argv)

    api_id = args.api_id
    api_hash = args.api_hash
    session_path = args.session_path

    if args.action == "check":
        if not api_id or not api_hash:
            return 1
        try:
            api_id_int, api_hash_str = validate_api_credentials(api_id, api_hash)
            authorized = asyncio.run(is_session_authorized(session_path, api_id_int, api_hash_str))
            return 0 if authorized else 1
        except Exception as exc:
            logger.debug("Session check error: %s", exc)
            return 1

    elif args.action == "auth":
        try:
            if not api_id or not api_hash:
                sys.stderr.write("[ERROR] Telegram API ID and API Hash are required.\n")
                return 1
            res = asyncio.run(interactive_login(session_path, api_id, api_hash))
            if res in ("authorized", "already_authorized"):
                sys.stdout.write(f"Telegram authentication successful ({res}).\n")
                return 0
            return 1
        except Exception as exc:
            sys.stderr.write(f"[ERROR] Authentication failed: {exc}\n")
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
