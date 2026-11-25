#!/usr/bin/env python3
"""Cleanup studios and tags for Variant 1 compliance.

- Separates actual production studios (MenAtPlay, RS, Dragon Media...) from Telegram channels.
- Production studios go to the studio column and tag 'Studio: [Name]'.
- Telegram channels go to tag 'Kanał: [Name]', never to the studio column.
- Completely eliminates 'Telegram' and 't.me' tags and channel IDs (-100...) from the DB.
"""

import json
import sys
from pathlib import Path
from typing import Set

BASE_DIR = Path(__file__).resolve().parent.parent

from sync.studio_tags import detect_production_studio_names

BACKUP_DIR = Path("/data/backups") if Path("/data").exists() else BASE_DIR / "data" / "backups"

KNOWN_CHANNELS: Set[str] = set()


def _init_channel_config() -> None:
    global KNOWN_CHANNELS
    try:
        from common.config_loader import load_config
        cfg = load_config()
        tg = cfg.get_source("telegram")
        if tg and getattr(tg, "channels", None):
            for ch in tg.channels:
                studio = getattr(ch, "studio", "") or ""
                if studio:
                    KNOWN_CHANNELS.add(studio)
    except Exception as exc:
        print(f"Error loading channel config: {exc}", file=sys.stderr)
        sys.exit(1)

    if not KNOWN_CHANNELS:
        print("Error: No known channels loaded from configuration. Refusing to run with empty allow-list.", file=sys.stderr)
        sys.exit(1)


_init_channel_config()


def is_unwanted_tag(val: str, channel_name: str = "") -> bool:
    v = str(val).strip()
    low = v.lower()
    if not low:
        return True
    if low in ("telegram", "t.me", "telegram (t.me)"):
        return True
    if low.startswith("-100") or (low.startswith("-") and low[1:].isdigit()):
        return True
    if channel_name and low == channel_name.lower():
        return True
    if any(low == kc.lower() for kc in KNOWN_CHANNELS):
        return True
    return False


def _channel_from_tags(tags) -> str:
    for t in tags:
        if t.startswith("Kanał: "):
            return t[7:].strip()
        if t in KNOWN_CHANNELS:
            return t
    return ""


def _build_clean_tags(tags, channel: str, prod_studios) -> Set[str]:
    clean_tags: Set[str] = set()
    for t in tags:
        if not is_unwanted_tag(t, channel):
            clean_tags.add(t)

    if channel:
        clean_tags.add(f"Kanał: {channel}")

    for ps in prod_studios:
        clean_tags.add(f"Studio: {ps}")
    return clean_tags


def _pg_channel(old_studio, tags) -> str:
    channel = old_studio if old_studio in KNOWN_CHANNELS else ""
    if not channel:
        channel = _channel_from_tags(tags)
    return channel


def _pg_production_studios(title, tags, channel: str) -> list:
    prod_studios = detect_production_studio_names(title, existing_tags=tags)
    for t in tags:
        if t.startswith("Studio: "):
            prod_studios.append(t.split("Studio: ", 1)[1].strip())
    return sorted({
        s for s in prod_studios
        if s and not is_unwanted_tag(s, channel) and s not in KNOWN_CHANNELS
    })


def _update_pg_rows(conn, rows) -> int:
    updated_pg = 0
    with conn:
        for vid_id, title, tags_raw, old_studio in rows:
            try:
                tags = json.loads(tags_raw or "[]")
            except Exception:
                tags = []

            channel = _pg_channel(old_studio, tags)
            prod_studios = _pg_production_studios(title, tags, channel)
            clean_tags = _build_clean_tags(tags, channel, prod_studios)

            new_tags_json = json.dumps(sorted(clean_tags))
            new_studio = prod_studios[0] if prod_studios else None

            if new_tags_json != tags_raw or new_studio != old_studio:
                conn.execute("UPDATE videos SET tags = %s, studio = %s WHERE id = %s",
                             (new_tags_json, new_studio, vid_id))
                updated_pg += 1
    return updated_pg


def run_cleanup():
    print("=== Adult Hub: Variant 1 Studios & Tags Cleanup ===")

    # Update the PostgreSQL catalog.
    try:
        from common import db
        conn = db.connect()
        print("\nUpdating PostgreSQL 'videos' table...")
        rows = conn.execute("SELECT id, title, tags, studio FROM videos").fetchall()
        print(f"Found {len(rows)} videos in PostgreSQL.")

        updated_pg = _update_pg_rows(conn, rows)

        print(f"Updated {updated_pg} records in PostgreSQL.")
    except Exception as exc:
        print(f"PostgreSQL update skipped or error: {exc}")

    print("\nCleanup successfully completed!")


if __name__ == "__main__":
    run_cleanup()
