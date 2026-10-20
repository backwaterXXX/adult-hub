#!/usr/bin/env python3
"""Restore catalogue titles for Telegram entries matched against StashDB/ThePornDB.

A confirmed scene match is the canonical title source, but a provider listing
can overwrite that title before the NFO is written, which leaves the sidecar
that Jellyfin imports carrying the channel caption instead of the catalogue
name. This script re-reads every linked scene by its stored identifier and
reports (or applies) the title difference.

Default is report-only. Pass --apply to store the catalogue title in the
database and in the NFO sidecar.
"""

import argparse
import json
import os
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _candidate in (ROOT, ROOT / "sync", Path("/app"), Path.cwd()):
    if _candidate.is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

from common import db
import display_titles
from media_files import stable_media_basename
from scene_scraper_text_sources import fetch_stashdb_scene_by_id, fetch_tpdb_scene_by_id

clean_display_title = display_titles.clean_display_title
MEDIA_DIR = Path(os.getenv("MEDIA_DIR", str(ROOT / "media" / "adult")))
CATALOG_SOURCES = ("stashdb", "theporndb", "tpdb")


def _is_catalog_source(source: str) -> bool:
    lowered = str(source or "").casefold()
    return any(provider in lowered for provider in CATALOG_SOURCES)


def _catalog_uid_type(source: str) -> str:
    lowered = str(source or "").casefold()
    if "theporndb" in lowered or lowered.startswith("tpdb"):
        return "theporndb"
    return "stashdb"


def _load_candidates(conn, limit=None):
    query = """
        SELECT v.id, v.title, v.slug, v.performers, v.scene_match_source,
               v.scene_match_external_id
        FROM videos v
        WHERE v.provider = 'telegram'
          AND v.scene_match_external_id IS NOT NULL
          AND (v.scene_match_source ILIKE '%%stashdb%%'
               OR v.scene_match_source ILIKE '%%theporndb%%'
               OR v.scene_match_source ILIKE '%%tpdb%%')
          AND NOT EXISTS (SELECT 1 FROM video_metadata_overrides o
                          WHERE o.video_id = v.id AND o.fields ? 'title')
        ORDER BY v.id
    """
    if limit is not None:
        query += " LIMIT %s"
        rows = conn.execute(query, (limit,)).fetchall()
    else:
        rows = conn.execute(query).fetchall()
    return rows


def _fetch_scene(source: str, external_id: str):
    lowered = str(source or "").casefold()
    lookup = (
        fetch_tpdb_scene_by_id
        if "theporndb" in lowered or "tpdb" in lowered
        else fetch_stashdb_scene_by_id
    )
    return lookup(external_id)


def _nfo_path(video_id: str) -> Path:
    return MEDIA_DIR / f"{stable_media_basename(video_id)}.nfo"


def _patch_nfo(path: Path, title: str, provider_title: str, uid_type: str,
               external_id: str) -> bool:
    """Set the catalogue title while keeping the provider caption as provenance."""
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return False
    title_elem = root.find("title")
    if title_elem is None:
        title_elem = ET.SubElement(root, "title")
    title_elem.text = title
    original_elem = root.find("originaltitle")
    if original_elem is None:
        original_elem = ET.SubElement(root, "originaltitle")
    original_elem.text = provider_title
    if uid_type and external_id:
        provider_uid = None
        for uid in root.findall("uniqueid"):
            if uid.get("type") == uid_type:
                provider_uid = uid
                break
        if provider_uid is None:
            provider_uid = ET.SubElement(root, "uniqueid", type=uid_type, default="false")
        provider_uid.text = external_id
    import xml.dom.minidom
    pretty = xml.dom.minidom.parseString(ET.tostring(root, encoding="utf-8")).toprettyxml(
        indent="  ", encoding="utf-8")
    clean_lines = [line for line in pretty.decode("utf-8").splitlines() if line.strip()]
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text("\n".join(clean_lines) + "\n", encoding="utf-8")
    temp_path.replace(path)
    return True


def _jellyfin_item_ids(url: str, api_key: str, wanted: set) -> dict:
    """Map the changed Telegram entries to their Jellyfin item identifiers."""
    import requests
    session = requests.Session()
    headers = {
        "X-Emby-Token": api_key,
        "X-Emby-Authorization": (
            'MediaBrowser Client="adult-hub", Device="sync", DeviceId="adult-hub-sync", '
            f'Version="1.0.0", Token="{api_key}"'
        ),
        "Authorization": (
            'MediaBrowser Client="adult-hub", Device="sync", DeviceId="adult-hub-sync", '
            f'Version="1.0.0", Token="{api_key}"'
        ),
    }
    mapping = {}
    start = 0
    while True:
        response = session.get(
            f"{url.rstrip('/')}/Items", headers=headers, timeout=30,
            params={"Recursive": "true", "IncludeItemTypes": "Movie", "Fields": "ProviderIds",
                    "StartIndex": start, "Limit": 500},
        )
        response.raise_for_status()
        page = response.json()
        items = page.get("Items") or []
        for item in items:
            provider_ids = item.get("ProviderIds") or {}
            telegram_id = ""
            for key, value in provider_ids.items():
                if str(key).casefold() == "telegram":
                    telegram_id = str(value or "")
                    break
            if telegram_id in wanted:
                mapping[telegram_id] = item.get("Id")
        start += len(items)
        if not items or start >= page.get("TotalRecordCount", start):
            break
    return mapping


def _push_to_jellyfin(changed_ids: list) -> str:
    """Import the changed sidecars through the supported Jellyfin item API."""
    url = (os.getenv("JELLYFIN_URL") or "").strip()
    api_key = (os.getenv("JELLYFIN_API_KEY") or "").strip()
    if not url or not api_key:
        return "skipped: jellyfin not configured"
    wanted = {str(video_id).split(":", 1)[-1] for video_id in changed_ids}
    item_ids = list(_jellyfin_item_ids(url, api_key, wanted).values())
    if not item_ids:
        return "skipped: no matching Jellyfin items"
    from jellyfin_db import sync_jellyfin_telegram_metadata as reconcile
    state_path = Path(os.getenv("DATA_DIR", "/data")) / "telegram-metadata-state.json"
    result = reconcile(url, api_key, str(MEDIA_DIR), state_path=state_path,
                       limit=len(item_ids), item_ids=item_ids, workers=4)
    return (f"checked={result.get('checked')} needed={result.get('needed')} "
            f"updated={result.get('updated')} failed={result.get('failed')}")


def run(apply: bool = False, limit: int | None = None, sleep_sec: float = 0.5,
        skip_jellyfin: bool = False) -> dict:
    conn = db.connect((os.getenv("DATABASE_URL") or "").strip() or None)
    report = {"videos": 0, "unchanged": 0, "changed": [], "failed": [],
              "nfos_updated": 0, "nfos_missing": 0, "applied": apply}
    try:
        rows = _load_candidates(conn, limit)
        report["videos"] = len(rows)
        for index, (video_id, title, slug, performers_raw, source, external_id) in enumerate(rows):
            if index and sleep_sec > 0:
                time.sleep(sleep_sec)
            try:
                scene = _fetch_scene(source, external_id)
            except Exception as exc:
                report["failed"].append({"video_id": video_id,
                                         "error": type(exc).__name__})
                continue
            catalog_title = str(getattr(scene, "title", "") or "").strip()
            if not catalog_title:
                report["failed"].append({"video_id": video_id, "error": "no_catalogue_title"})
                continue
            if catalog_title == str(title or "").strip():
                report["unchanged"] += 1
                continue
            performers = []
            try:
                performers = json.loads(performers_raw) if performers_raw else []
            except (TypeError, ValueError):
                performers = []
            display_title = clean_display_title(
                catalog_title, performers=performers, provider="telegram", slug=slug or "",
            ) or catalog_title
            report["changed"].append({
                "video_id": video_id,
                "old_title": title,
                "catalogue_title": catalog_title,
                "display_title": display_title,
                "source": source,
            })
            if not apply:
                continue
            with conn:
                conn.execute("UPDATE videos SET title=%s WHERE id=%s", (catalog_title, video_id))
            path = _nfo_path(video_id)
            if not path.is_file():
                report["nfos_missing"] += 1
                continue
            try:
                root = ET.parse(path).getroot()
            except (OSError, ET.ParseError):
                report["nfos_missing"] += 1
                continue
            current_title = str(root.findtext("title") or "").strip()
            current_original = str(root.findtext("originaltitle") or "").strip()
            # The sidecar still shows the provider caption when it matches the
            # stored title; that caption becomes the original title.
            provider_title = current_original or current_title
            if _patch_nfo(path, display_title, provider_title,
                          _catalog_uid_type(source), str(external_id or "")):
                report["nfos_updated"] += 1
    finally:
        conn.close()
    if apply and report["changed"] and not skip_jellyfin:
        try:
            report["jellyfin_import"] = _push_to_jellyfin(
                [entry["video_id"] for entry in report["changed"]])
        except Exception as exc:
            report["jellyfin_import"] = f"failed: {type(exc).__name__}"
        try:
            import sync as sync_module
            report["jellyfin_refresh"] = (
                "triggered" if sync_module.trigger_jellyfin_refresh() else "not_confirmed"
            )
        except Exception as exc:
            report["jellyfin_refresh"] = f"failed: {type(exc).__name__}"
    return report


def import_only(report_path: str) -> dict:
    """Import the sidecars listed in an earlier report into Jellyfin."""
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    changed = [entry["video_id"] for entry in report.get("changed", [])]
    if not changed:
        return {"items": 0, "jellyfin_import": "skipped: no changed entries"}
    return {"items": len(changed), "jellyfin_import": _push_to_jellyfin(changed)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="store the catalogue title in the database and the NFO")
    parser.add_argument("--limit", type=int,
                        help="only inspect the first N matched entries")
    parser.add_argument("--skip-jellyfin", action="store_true",
                        help="do not import the changed sidecars into Jellyfin")
    parser.add_argument("--import-only", metavar="REPORT",
                        help="import the changed sidecars listed in a previous report")
    parser.add_argument("--output", help="write the JSON report to this file")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit requires a positive number")
    if args.import_only:
        report = import_only(args.import_only)
    else:
        report = run(args.apply, args.limit, skip_jellyfin=args.skip_jellyfin)
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
