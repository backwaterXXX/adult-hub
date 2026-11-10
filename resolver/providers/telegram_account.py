"""Default account boundary and session path management for Telegram providers."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Tuple

from resolver.providers.telegram_session import (
    get_pending_session_file_path,
    get_sqlite_session_file_path,
    validate_api_credentials,
)

DEFAULT_ACCOUNT_ID = "default"
ACCOUNT_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def validate_account_id(account_id: Any) -> str:
    """Validate that account_id is a safe, non-traversing identifier.

    Rejects path separators, traversal sequences (..), whitespace, null bytes,
    and invalid characters to preserve filesystem and account isolation boundaries.
    """
    if not isinstance(account_id, str):
        raise ValueError("Account ID must be a string.")
    normalized = account_id.strip()
    if not normalized:
        raise ValueError("Account ID cannot be empty.")
    if "/" in normalized or "\\" in normalized or ".." in normalized or "\x00" in normalized:
        raise ValueError(f"Path traversal or separator characters are not permitted in account ID: {account_id!r}")
    if not ACCOUNT_ID_PATTERN.match(normalized):
        raise ValueError(
            f"Invalid account ID {account_id!r}: must contain only alphanumeric characters, underscores, and hyphens (1-64 chars)."
        )
    return normalized


class TelegramAccountContext:
    """Internal account boundary scoping session paths and credentials.

    The application remains single-account in product UI and sync behavior.
    This abstraction internally scopes session storage, credentials, and
    transient login challenges to a stable default account identity so
    additional accounts can be added later without redesigning the auth flow.
    """

    def __init__(
        self,
        account_id: str | None = None,
        *,
        session_path: str | None = None,
        api_id: Any = None,
        api_hash: Any = None,
    ) -> None:
        if account_id is None:
            self.account_id = DEFAULT_ACCOUNT_ID
        else:
            self.account_id = validate_account_id(account_id)
        self._session_path_override = session_path
        self._api_id_override = api_id
        self._api_hash_override = api_hash

    @property
    def is_default(self) -> bool:
        return self.account_id == DEFAULT_ACCOUNT_ID

    def get_session_path(self) -> str:
        """Resolve the SQLite session file path for this account."""
        if self._session_path_override:
            return get_sqlite_session_file_path(self._session_path_override)

        base_path = os.getenv("TELEGRAM_SESSION_PATH", "/data/telegram.session")
        if self.is_default:
            return get_sqlite_session_file_path(base_path)

        p = Path(base_path)
        stem = p.stem
        suffix = p.suffix or ".session"
        scoped_filename = f"{stem}.{self.account_id}{suffix}"
        return str(p.with_name(scoped_filename))

    def get_pending_session_path(self) -> str:
        """Resolve the isolated pending session file path for this account."""
        target = self.get_session_path()
        return get_pending_session_file_path(target)

    def get_api_credentials(self) -> Tuple[int, str]:
        """Fetch and validate API ID and Hash for this account.

        Keeps credentials as environment secrets; never written to disk.
        """
        raw_id = self._api_id_override if self._api_id_override is not None else os.getenv("TELEGRAM_API_ID")
        raw_hash = self._api_hash_override if self._api_hash_override is not None else os.getenv("TELEGRAM_API_HASH")
        return validate_api_credentials(raw_id, raw_hash)

    def has_api_credentials(self) -> bool:
        """Check if API credentials are provided and structurally valid."""
        try:
            self.get_api_credentials()
            return True
        except (ValueError, TypeError):
            return False
