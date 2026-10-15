"""Base models and types for database schema migrations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable


@dataclass(frozen=True)
class MigrationRecord:
    """Applied migration stored in schema_migrations."""

    version: int
    name: str
    applied_at: datetime


@dataclass
class Migration:
    """Executable migration step."""

    version: int
    name: str
    description: str
    apply_fn: Callable[..., None]

    def apply(self, conn: Any, **kwargs) -> None:
        self.apply_fn(conn, **kwargs)


@dataclass(frozen=True)
class MigrationStatus:
    """Current state of database migrations."""

    current_version: int
    applied: list[MigrationRecord]
    pending: list[Migration]

    @property
    def is_up_to_date(self) -> bool:
        return len(self.pending) == 0
