"""Shared parsing helpers for media durations."""

import re
from typing import Optional


def parse_duration_seconds(value: Optional[str]) -> int:
    """Parse HH:MM:SS, MM:SS, seconds, or min/sec text into seconds."""
    if not value:
        return 0
    text = value.strip()
    if "min" in text.lower() or "sec" in text.lower():
        total = 0
        minutes = re.search(r"(\d+)\s*min", text, re.I)
        if minutes:
            total += int(minutes.group(1)) * 60
        seconds = re.search(r"(\d+)\s*sec", text, re.I)
        if seconds:
            total += int(seconds.group(1))
        if total > 0:
            return total

    parts = text.split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        if len(parts) == 1 and parts[0].isdigit():
            return int(parts[0])
    except (ValueError, TypeError):
        pass
    return 0
