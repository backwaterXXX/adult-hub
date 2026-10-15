"""Data models shared by scene metadata providers and matching pipelines."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass
class ScrapedScene:
    title: str
    plot: str = ""
    date: str = ""  # YYYY-MM-DD
    duration: int = 0  # seconds
    poster_url: str = ""
    performers: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    studio: str = ""
    external_id: str = ""
    url: str = ""
    confidence: float = 0.0
    source: str = ""


@dataclass(frozen=True)
class TelegramOshashResult:
    fingerprint: str
    bytes_read: int
    media_probe: dict

    def __iter__(self):
        # Keep the existing two-value unpacking contract for internal callers.
        yield self.fingerprint
        yield self.bytes_read
