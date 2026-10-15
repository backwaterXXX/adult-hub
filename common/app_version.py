"""Runtime version of the Adult-Hub application."""

from __future__ import annotations

import os
from pathlib import Path


def get_app_version() -> str:
    """Read the version file mounted into the app or copied into its image."""
    candidates = (
        Path.cwd() / "VERSION",
        Path("/opt/adult-hub/VERSION"),
    )
    for path in candidates:
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return value
    return os.getenv("APP_VERSION", "0+unknown")


__version__ = get_app_version()
