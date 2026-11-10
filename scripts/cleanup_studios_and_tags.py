#!/usr/bin/env python3
"""Cleanup studios and tags for Variant 1 compliance.

- Separates actual production studios (MenAtPlay, RS, Dragon Media...) from Telegram channels.
- Production studios go to <studio> and tag 'Studio: [Name]'.
- Telegram channels go to tag 'Kanał: [Name]', never to <studio>.
- Completely eliminates 'Telegram' and 't.me' tags and channel IDs (-100...) from DB and NFO files.
"""

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Optional, Set, Tuple

BASE_DIR = Path(__file__).resolve().parent.parent

from sync.studio_tags import detect_production_studio_names

MEDIA_DIR = Path("/media/adult") if Path("/media/adult").exists() else BASE_DIR / "media" / "adult"
BACKUP_DIR = Path("/data/backups") if Path("/data").exists() else BASE_DIR / "data" / "backups"

KNOWN_CHANNELS: Set[str] = set()
CHANNEL_ID_MAP: dict[str, str] = {}


def _init_channel_config() -> None:
    global KNOWN_CHANNELS, CHANNEL_ID_MAP
    try:
        from common.config_loader import load_config
        cfg = load_config()
        tg = cfg.get_source("telegram")
        if tg and getattr(tg, "channels", None):
            for ch in tg.channels:
                studio = getattr(ch, "studio", "") or ""
                cid = str(getattr(ch, "id", "") or "")
                if studio:
                    KNOWN_CHANNELS.add(studio)
                if cid and studio:
                    CHANNEL_ID_MAP[cid] = studio
    except Exception as exc:
        print(f"Error loading channel config: {exc}", file=sys.stderr)
        sys.exit(1)

    if not KNOWN_CHANNELS:
        print("Error: No known channels loaded from configuration. Refusing to run with empty allow-list.", file=sys.stderr)
        sys.exit(1)


_init_channel_config()


def clean_xml_element_text(elem: Optional[ET.Element]) -> str:
    return (elem.text or "").strip() if elem is not None else ""


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


def _read_nfo_root(nfo_path: Path) -> Optional[ET.Element]:
    try:
        content = nfo_path.read_text(encoding="utf-8")
        return ET.fromstring(content)
    except Exception as exc:
        print(f"Error reading {nfo_path}: {exc}")
        return None


def _channel_from_studios(existing_studios: List[str]) -> str:
    for s in existing_studios:
        clean_s = s.replace("&amp;", "&")
        if clean_s in KNOWN_CHANNELS or any(kc.lower() == clean_s.lower() for kc in KNOWN_CHANNELS):
            return clean_s
    return ""


def _channel_from_tags(tags) -> str:
    for t in tags:
        if t.startswith("Kanał: "):
            return t[7:].strip()
        if t in KNOWN_CHANNELS:
            return t
    return ""


def _channel_from_uniqueid(root: ET.Element, nfo_path: Path) -> str:
    uniq_elem = root.find("uniqueid")
    uniq_text = clean_xml_element_text(uniq_elem)
    for cid, cname in CHANNEL_ID_MAP.items():
        if cid in uniq_text or cid in str(nfo_path):
            return cname
    return ""


def _detect_channel_name(root: ET.Element, existing_studios: List[str],
                         existing_tags: List[str], nfo_path: Path) -> str:
    channel_name = _channel_from_studios(existing_studios)
    if not channel_name:
        channel_name = _channel_from_tags(existing_tags)
    if not channel_name:
        channel_name = _channel_from_uniqueid(root, nfo_path)
    return channel_name


def _detect_production_studios(search_title: str, existing_tags: List[str],
                               channel_name: str) -> Set[str]:
    prod_studios: Set[str] = set()
    for t in existing_tags:
        if t.startswith("Studio: "):
            prod_studios.add(t.split("Studio: ", 1)[1].strip())

    detected_from_title = detect_production_studio_names(search_title, existing_tags=existing_tags)
    prod_studios.update(detected_from_title)

    # Filter out channel name if accidentally detected
    return {s for s in prod_studios if s and not is_unwanted_tag(s, channel_name) and s not in KNOWN_CHANNELS}


def _index_before(root: ET.Element, target_tags: Tuple[str, ...]) -> int:
    """Index of the first child matching a target tag, in priority order; else the end."""
    for target_tag in target_tags:
        elem = root.find(target_tag)
        if elem is not None:
            return list(root).index(elem)
    return len(root)


def _replace_studio_elements(root: ET.Element, prod_studios: Set[str]) -> bool:
    changed = False
    # Remove all current studio elements
    for s in root.findall("studio"):
        root.remove(s)
        changed = True

    # Find insertion index (e.g. before dateadded or runtime or genre or tag)
    insert_idx = _index_before(root, ("dateadded", "website", "plot", "runtime", "genre", "tag", "actor"))

    # Insert true production studios
    if prod_studios:
        for idx, ps in enumerate(sorted(prod_studios)):
            new_s = ET.Element("studio")
            new_s.text = ps
            root.insert(insert_idx + idx, new_s)
        changed = True
    return changed


def _update_tagline_and_plot(root: ET.Element, channel_name: str) -> bool:
    changed = False
    tagline_elem = root.find("tagline")
    if tagline_elem is not None:
        old_tl = clean_xml_element_text(tagline_elem)
        if "Telegram (t.me)" in old_tl or "Telegram" in old_tl:
            tagline_elem.text = f"Kanał: {channel_name}" if channel_name else ""
            changed = True

    plot_elem = root.find("plot")
    if plot_elem is not None:
        old_plot = clean_xml_element_text(plot_elem)
        if "Source: Telegram (t.me)" in old_plot:
            rep = f"Kanał: {channel_name}" if channel_name else ""
            plot_elem.text = old_plot.replace("Source: Telegram (t.me)", rep).strip()
            changed = True
    return changed


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


def _rebuild_tags(root: ET.Element, existing_tags: List[str], prod_studios: Set[str],
                  channel_name: str) -> bool:
    clean_tags = _build_clean_tags(existing_tags, channel_name, prod_studios)

    # Check if tags actually changed
    old_tags_set = set(existing_tags)
    changed = clean_tags != old_tags_set

    # Remove all existing tag elements
    for t in root.findall("tag"):
        root.remove(t)

    # Insert clean tags (before <set> or <actor> or at end)
    tag_insert_idx = _index_before(root, ("set", "actor", "uniqueid"))

    for idx, t_text in enumerate(sorted(clean_tags)):
        new_tag = ET.Element("tag")
        new_tag.text = t_text
        root.insert(tag_insert_idx + idx, new_tag)
    return changed


def _write_nfo(nfo_path: Path, root: ET.Element) -> None:
    # Format XML nicely
    ET.indent(root, space="  ")
    xml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    # Ensure clean trailing newline
    if not xml_bytes.endswith(b"\n"):
        xml_bytes += b"\n"
    tmp_path = nfo_path.with_suffix(".tmp.nfo")
    tmp_path.write_bytes(xml_bytes)
    tmp_path.replace(nfo_path)


def process_nfo_file(nfo_path: Path, dry_run: bool = False) -> Tuple[bool, Optional[str], List[str]]:
    """Process a single NFO file. Returns (changed, detected_channel, detected_studios)."""
    root = _read_nfo_root(nfo_path)
    if root is None:
        return False, None, []

    # 1. Determine title and uniqueid
    title = clean_xml_element_text(root.find("title"))
    orig_title = clean_xml_element_text(root.find("originaltitle"))
    search_title = f"{title} {orig_title}".strip()

    # 2. Extract existing studio and tags
    existing_studios = [clean_xml_element_text(s) for s in root.findall("studio") if clean_xml_element_text(s)]
    existing_tags = [clean_xml_element_text(t) for t in root.findall("tag") if clean_xml_element_text(t)]

    # 3. Determine Telegram channel name
    channel_name = _detect_channel_name(root, existing_studios, existing_tags, nfo_path)

    # 4. Detect true production studios
    prod_studios = _detect_production_studios(search_title, existing_tags, channel_name)

    # 5. Clean up <studio> elements
    changed = _replace_studio_elements(root, prod_studios)

    # 6. Clean up <tagline> and <plot>
    changed |= _update_tagline_and_plot(root, channel_name)

    # 7. Clean up <tag> elements
    changed |= _rebuild_tags(root, existing_tags, prod_studios, channel_name)

    if changed and not dry_run:
        _write_nfo(nfo_path, root)

    return changed, channel_name, sorted(prod_studios)


def _scan_nfo_files(nfo_files: List[Path]) -> Tuple[int, dict, dict]:
    updated_nfo_count = 0
    studios_found = {}
    channels_found = {}

    for i, p in enumerate(nfo_files):
        changed, channel, studios = process_nfo_file(p, dry_run=False)
        if changed:
            updated_nfo_count += 1
        if channel:
            channels_found[channel] = channels_found.get(channel, 0) + 1
        for s in studios:
            studios_found[s] = studios_found.get(s, 0) + 1

        if (i + 1) % 2000 == 0 or (i + 1) == len(nfo_files):
            print(f"Processed {i + 1}/{len(nfo_files)} NFO files...")
    return updated_nfo_count, studios_found, channels_found


def _print_nfo_summary(nfo_files: List[Path], updated_nfo_count: int,
                       studios_found: dict, channels_found: dict) -> None:
    print("\n--- NFO Processing Complete ---")
    print(f"Updated NFO files: {updated_nfo_count}/{len(nfo_files)}")
    print(f"Distinct Channels ({len(channels_found)}):")
    for c, cnt in sorted(channels_found.items(), key=lambda x: -x[1]):
        print(f"  {c}: {cnt}")
    print(f"Distinct Production Studios detected ({len(studios_found)}):")
    for s, cnt in sorted(studios_found.items(), key=lambda x: -x[1])[:25]:
        print(f"  {s}: {cnt}")


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
    print(f"Media directory: {MEDIA_DIR}")

    # Check NFO files
    nfo_files = sorted(MEDIA_DIR.glob("*.nfo"))
    print(f"Found {len(nfo_files)} .nfo files to inspect.")

    updated_nfo_count, studios_found, channels_found = _scan_nfo_files(nfo_files)
    _print_nfo_summary(nfo_files, updated_nfo_count, studios_found, channels_found)

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
