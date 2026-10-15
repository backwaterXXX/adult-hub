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
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "sync"))

try:
    from sync.studio_tags import detect_production_studio_names
except ImportError:
    from studio_tags import detect_production_studio_names

MEDIA_DIR = Path("/media/adult") if Path("/media/adult").exists() else BASE_DIR / "media" / "adult"
BACKUP_DIR = Path("/data/backups") if Path("/data").exists() else BASE_DIR / "data" / "backups"

KNOWN_CHANNELS: Set[str] = set()
CHANNEL_ID_MAP: dict[str, str] = {}


def _init_channel_config() -> None:
    global KNOWN_CHANNELS, CHANNEL_ID_MAP
    try:
        try:
            from common.config_loader import load_config
        except ImportError:
            from config_loader import load_config
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
    except Exception:
        pass


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


def process_nfo_file(nfo_path: Path, dry_run: bool = False) -> Tuple[bool, Optional[str], List[str]]:
    """Process a single NFO file. Returns (changed, detected_channel, detected_studios)."""
    try:
        content = nfo_path.read_text(encoding="utf-8")
        root = ET.fromstring(content)
    except Exception as exc:
        print(f"Error reading {nfo_path}: {exc}")
        return False, None, []

    changed = False

    # 1. Determine title and uniqueid
    title_elem = root.find("title")
    orig_title_elem = root.find("originaltitle")
    title = clean_xml_element_text(title_elem)
    orig_title = clean_xml_element_text(orig_title_elem)
    search_title = f"{title} {orig_title}".strip()

    # 2. Extract existing studio and tags
    existing_studios = [clean_xml_element_text(s) for s in root.findall("studio") if clean_xml_element_text(s)]
    existing_tags = [clean_xml_element_text(t) for t in root.findall("tag") if clean_xml_element_text(t)]

    # 3. Determine Telegram channel name
    channel_name = ""
    for s in existing_studios:
        clean_s = s.replace("&amp;", "&")
        if clean_s in KNOWN_CHANNELS or any(kc.lower() == clean_s.lower() for kc in KNOWN_CHANNELS):
            channel_name = clean_s
            break

    if not channel_name:
        for t in existing_tags:
            if t.startswith("Kanał: "):
                channel_name = t[7:].strip()
                break
            elif t in KNOWN_CHANNELS:
                channel_name = t
                break

    if not channel_name:
        uniq_elem = root.find("uniqueid")
        uniq_text = clean_xml_element_text(uniq_elem)
        for cid, cname in CHANNEL_ID_MAP.items():
            if cid in uniq_text or cid in str(nfo_path):
                channel_name = cname
                break

    # 4. Detect true production studios
    prod_studios: Set[str] = set()
    for t in existing_tags:
        if t.startswith("Studio: "):
            prod_studios.add(t.split("Studio: ", 1)[1].strip())

    detected_from_title = detect_production_studio_names(search_title, existing_tags=existing_tags)
    prod_studios.update(detected_from_title)

    # Filter out channel name if accidentally detected
    prod_studios = {s for s in prod_studios if s and not is_unwanted_tag(s, channel_name) and s not in KNOWN_CHANNELS}

    # 5. Clean up <studio> elements
    # Remove all current studio elements
    studios_to_remove = root.findall("studio")
    for s in studios_to_remove:
        root.remove(s)
        changed = True

    # Find insertion index (e.g. before dateadded or runtime or genre or tag)
    insert_idx = None
    for target_tag in ("dateadded", "website", "plot", "runtime", "genre", "tag", "actor"):
        elem = root.find(target_tag)
        if elem is not None:
            insert_idx = list(root).index(elem)
            break
    if insert_idx is None:
        insert_idx = len(root)

    # Insert true production studios
    if prod_studios:
        for idx, ps in enumerate(sorted(prod_studios)):
            new_s = ET.Element("studio")
            new_s.text = ps
            root.insert(insert_idx + idx, new_s)
        changed = True

    # 6. Clean up <tagline> and <plot>
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

    # 7. Clean up <tag> elements
    clean_tags: Set[str] = set()
    for t in existing_tags:
        if not is_unwanted_tag(t, channel_name):
            clean_tags.add(t)

    if channel_name:
        clean_tags.add(f"Kanał: {channel_name}")

    for ps in prod_studios:
        clean_tags.add(f"Studio: {ps}")

    # Check if tags actually changed
    old_tags_set = set(existing_tags)
    if clean_tags != old_tags_set:
        changed = True

    # Remove all existing tag elements
    for t in root.findall("tag"):
        root.remove(t)

    # Insert clean tags (before <set> or <actor> or at end)
    tag_insert_idx = None
    for target_tag in ("set", "actor", "uniqueid"):
        elem = root.find(target_tag)
        if elem is not None:
            tag_insert_idx = list(root).index(elem)
            break
    if tag_insert_idx is None:
        tag_insert_idx = len(root)

    for idx, t_text in enumerate(sorted(clean_tags)):
        new_tag = ET.Element("tag")
        new_tag.text = t_text
        root.insert(tag_insert_idx + idx, new_tag)

    if changed and not dry_run:
        # Format XML nicely
        ET.indent(root, space="  ")
        xml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        # Ensure clean trailing newline
        if not xml_bytes.endswith(b"\n"):
            xml_bytes += b"\n"
        tmp_path = nfo_path.with_suffix(".tmp.nfo")
        tmp_path.write_bytes(xml_bytes)
        tmp_path.replace(nfo_path)

    return changed, channel_name, sorted(prod_studios)


def run_cleanup():
    print("=== Adult Hub: Variant 1 Studios & Tags Cleanup ===")
    print(f"Media directory: {MEDIA_DIR}")

    # Check NFO files
    nfo_files = sorted(MEDIA_DIR.glob("*.nfo"))
    print(f"Found {len(nfo_files)} .nfo files to inspect.")

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

    print("\n--- NFO Processing Complete ---")
    print(f"Updated NFO files: {updated_nfo_count}/{len(nfo_files)}")
    print(f"Distinct Channels ({len(channels_found)}):")
    for c, cnt in sorted(channels_found.items(), key=lambda x: -x[1]):
        print(f"  {c}: {cnt}")
    print(f"Distinct Production Studios detected ({len(studios_found)}):")
    for s, cnt in sorted(studios_found.items(), key=lambda x: -x[1])[:25]:
        print(f"  {s}: {cnt}")

    # Update the PostgreSQL catalog.
    try:
        from common import db
        conn = db.connect()
        print("\nUpdating PostgreSQL 'videos' table...")
        rows = conn.execute("SELECT id, title, tags, studio FROM videos").fetchall()
        print(f"Found {len(rows)} videos in PostgreSQL.")

        updated_pg = 0
        with conn:
            for vid_id, title, tags_raw, old_studio in rows:
                try:
                    tags = json.loads(tags_raw or "[]")
                except Exception:
                    tags = []

                # Determine channel
                channel = old_studio if old_studio in KNOWN_CHANNELS else ""
                if not channel:
                    for t in tags:
                        if t.startswith("Kanał: "):
                            channel = t[7:].strip()
                            break
                        elif t in KNOWN_CHANNELS:
                            channel = t
                            break

                # Detect production studios
                prod_studios = detect_production_studio_names(title, existing_tags=tags)
                for t in tags:
                    if t.startswith("Studio: "):
                        prod_studios.append(t.split("Studio: ", 1)[1].strip())
                prod_studios = sorted(list(set(s for s in prod_studios if s and not is_unwanted_tag(s, channel) and s not in KNOWN_CHANNELS)))

                # Clean tags
                clean_tags = set()
                for t in tags:
                    if not is_unwanted_tag(t, channel):
                        clean_tags.add(t)
                if channel:
                    clean_tags.add(f"Kanał: {channel}")
                for ps in prod_studios:
                    clean_tags.add(f"Studio: {ps}")

                new_tags_json = json.dumps(sorted(list(clean_tags)))
                new_studio = prod_studios[0] if prod_studios else None

                if new_tags_json != tags_raw or new_studio != old_studio:
                    conn.execute("UPDATE videos SET tags = %s, studio = %s WHERE id = %s",
                                 (new_tags_json, new_studio, vid_id))
                    updated_pg += 1

        print(f"Updated {updated_pg} records in PostgreSQL.")
    except Exception as exc:
        print(f"PostgreSQL update skipped or error: {exc}")

    print("\nCleanup successfully completed!")


if __name__ == "__main__":
    run_cleanup()
