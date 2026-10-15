#!/usr/bin/env python3
"""Use authoritative local Telegram metadata; dry-run unless --apply."""

import argparse
import os
from pathlib import Path
import shutil
import tempfile
from urllib.parse import parse_qs, urlparse
import xml.etree.ElementTree as ET


def migrated_xml(data: bytes):
    """Return changed XML, or None. Preserve non-Telegram metadata and actors."""
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
    root = ET.fromstring(data, parser=parser)
    if root.tag != "movie" or not any(
        node.get("type") == "telegram" for node in root.findall("uniqueid")
    ):
        return None
    changed = False
    lock = root.find("lockdata")
    if lock is None:
        lock = ET.SubElement(root, "lockdata")
        lock.text = "true"
        changed = True
    elif (lock.text or "").strip().lower() != "true":
        lock.text = "true"
        changed = True
    for node in root.findall("thumb"):
        url = urlparse((node.text or "").strip())
        if (url.path == "/admin/api/thumb"
                and parse_qs(url.query).get("provider") == ["telegram"]
                and url.scheme in ("", "http", "https")):
            root.remove(node)
            changed = True
    if not changed:
        return None
    return ET.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"


def migrate_directory(directory: Path, *, apply: bool = False):
    if not directory.is_dir():
        raise ValueError(f"Not a media directory: {directory}")
    changed = failed = 0
    for path in sorted(directory.glob("*.nfo")):
        if path.is_symlink():
            print(f"SKIP symlink: {path.name}")
            continue
        try:
            original = path.read_bytes()
            replacement = migrated_xml(original)
            if replacement is None:
                continue
            if apply:
                backup = path.with_suffix(".nfo.before-telegram-thumb.bak")
                if not backup.exists():
                    # Exclusive creation preserves the first pre-migration version.
                    with backup.open("xb") as stream:
                        stream.write(original)
                    shutil.copystat(path, backup)
                fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=directory)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(replacement)
                    shutil.copymode(path, temp_name)
                    if path.read_bytes() != original:
                        raise RuntimeError("NFO changed concurrently; retry with sync paused")
                    os.replace(temp_name, path)
                finally:
                    Path(temp_name).unlink(missing_ok=True)
            changed += 1
            print(f"{'UPDATED' if apply else 'WOULD UPDATE'}: {path.name}")
        except (OSError, ValueError, ET.ParseError, RuntimeError) as exc:
            failed += 1
            print(f"ERROR: {path.name}: {exc}")
    print(f"{'Applied' if apply else 'Dry-run'}: {changed} changes, {failed} errors")
    return changed, failed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("media_directory", type=Path)
    parser.add_argument("--apply", action="store_true", help="Write changes with per-file backups")
    args = parser.parse_args()
    try:
        _, failed = migrate_directory(args.media_directory, apply=args.apply)
    except ValueError as exc:
        parser.error(str(exc))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
