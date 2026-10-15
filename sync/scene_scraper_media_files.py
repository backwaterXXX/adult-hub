"""Persist matched scene metadata to NFO sidecars and artwork files."""
from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional
from common.http_rate_limit import request_with_host_limit

try:
    from sync.scene_scraper_models import ScrapedScene
except ImportError:
    from scene_scraper_models import ScrapedScene

if __package__:
    from . import scene_scrapers as _core
else:
    import scene_scrapers as _core


def _update_media_files(
    media_dir: Path,
    video_id: str,
    matched: ScrapedScene,
    performers: List[str],
    http_client: Optional[Any] = None,
    conn: Optional[Any] = None,
) -> None:
    """Update NFO sidecar and download HD poster if available."""
    try:
        nfo_path = None
        if conn is not None:
            try:
                from sync.media_files import resolve_media_path
                file_rows = conn.execute(
                    "SELECT file_path FROM synced_files WHERE video_id=%s ORDER BY file_path", (video_id,)
                ).fetchall()
                for (file_path,) in file_rows:
                    stream_path = resolve_media_path(str(file_path), media_dir)
                    candidate = stream_path.with_suffix(".nfo")
                    if candidate.is_file() and not candidate.is_symlink():
                        nfo_path = candidate
                        break
            except Exception:
                nfo_path = None
        if nfo_path is None:
            # Compatibility for legacy files that predate the synced_files index.
            parts = video_id.split(":")
            comp_tail = parts[-2] if len(parts) > 2 else parts[-1]
            matches = list(media_dir.glob(f"*{comp_tail}*.nfo"))
            if len(matches) != 1:
                return
            nfo_path = matches[0]
        # Read and patch NFO XML
        text = nfo_path.read_text(encoding="utf-8")
        import xml.etree.ElementTree as ET
        root = ET.fromstring(text)

        overrides = {}
        if conn is not None:
            try:
                row = conn.execute(
                    "SELECT fields FROM video_metadata_overrides WHERE video_id=%s", (video_id,)
                ).fetchone()
                overrides = row[0] if row and isinstance(row[0], dict) else {}
                if row and isinstance(row[0], str):
                    overrides = _core.json.loads(row[0])
            except Exception:
                overrides = {}

        # Exact hash identity can safely supply a better title. Keep the
        # operator's value wherever an explicit metadata override exists.
        if matched.title and "title" not in overrides:
            title_elem = root.find("title")
            if title_elem is not None and not str(title_elem.text or "").strip():
                title_elem.text = matched.title

        # 1. Update <plot>
        if matched.plot and "plot" not in overrides:
            plot_elem = root.find("plot")
            if plot_elem is not None and not str(plot_elem.text or "").strip():
                plot_elem.text = matched.plot
            elif plot_elem is None:
                ET.SubElement(root, "plot").text = matched.plot

        # 2. Update <premiered> and <year>
        if matched.date and "date_released" not in overrides:
            prem_elem = root.find("premiered")
            if prem_elem is not None and not str(prem_elem.text or "").strip():
                prem_elem.text = matched.date
            elif prem_elem is None:
                ET.SubElement(root, "premiered").text = matched.date

            year = matched.date[:4]
            if year.isdigit():
                yr_elem = root.find("year")
                if yr_elem is not None and not str(yr_elem.text or "").strip():
                    yr_elem.text = year
                elif yr_elem is None:
                    ET.SubElement(root, "year").text = year

        # 3. Update <actor>, <set> and <tag> with official studio performers
        actor_links = {}
        if conn is not None:
            try:
                try:
                    from sync.nfo import load_performer_links
                except ImportError:
                    from nfo import load_performer_links
                all_actor_names = [a.findtext("name") for a in root.findall("actor") if a.findtext("name")]
                if matched.performers:
                    all_actor_names.extend(matched.performers)
                actor_links = load_performer_links(conn, all_actor_names)
            except Exception as e:
                _core.logger.debug("Could not load performer links: %s", e)
                actor_links = {}

        if matched.performers and "performers" not in overrides:
            has_actor = any(str(actor.findtext("name") or "").strip() for actor in root.findall("actor"))
            if not has_actor:
                for perf in matched.performers:
                    set_elem = ET.SubElement(root, "set")
                    ET.SubElement(set_elem, "name").text = perf
                    actor_elem = ET.SubElement(root, "actor")
                    ET.SubElement(actor_elem, "name").text = perf
                    ET.SubElement(actor_elem, "role").text = "Performer"
                    links = actor_links.get(perf.strip().lower(), {})
                    if links.get("stashdb"):
                        ET.SubElement(actor_elem, "profile").text = links["stashdb"]
                    if links.get("pbc"):
                        ET.SubElement(actor_elem, "profile").text = links["pbc"]

        if actor_links:
            for actor_elem in root.findall("actor"):
                aname = (actor_elem.findtext("name") or "").strip().lower()
                existing_profiles = {p.text.strip() for p in actor_elem.findall("profile") if p.text and p.text.strip()}
                links = actor_links.get(aname, {})
                if links.get("stashdb") and links["stashdb"] not in existing_profiles:
                    ET.SubElement(actor_elem, "profile").text = links["stashdb"]
                    existing_profiles.add(links["stashdb"])
                if links.get("pbc") and links["pbc"] not in existing_profiles:
                    ET.SubElement(actor_elem, "profile").text = links["pbc"]
                    existing_profiles.add(links["pbc"])

        if matched.studio and "studio" not in overrides:
            studios = root.findall("studio")
            if not any(str(studio.text or "").strip() for studio in studios):
                if studios:
                    studios[0].text = matched.studio
                else:
                    ET.SubElement(root, "studio").text = matched.studio

        if matched.tags and "tags" not in overrides:
            known_tags = {t.text for t in root.findall("tag") if t.text}
            for tag in matched.tags:
                if tag and tag not in known_tags:
                    ET.SubElement(root, "tag").text = tag
                    known_tags.add(tag)

        # Update scene match external id (uniqueid type="stashdb") and website
        if (matched.source == "stashdb" or str(matched.source).startswith("stashdb:")) and matched.external_id:
            stash_uid = None
            for uid in root.findall("uniqueid"):
                if uid.get("type") == "stashdb":
                    stash_uid = uid
                    break
            if stash_uid is not None:
                stash_uid.text = matched.external_id
            else:
                ET.SubElement(root, "uniqueid", type="stashdb", default="false").text = matched.external_id

        if matched.url:
            web_elem = root.find("website")
            if web_elem is not None:
                if not (web_elem.text or "").strip() or "t.me" in (web_elem.text or ""):
                    web_elem.text = matched.url
            else:
                ET.SubElement(root, "website").text = matched.url

        # 4. Write updated NFO
        xml_bytes = ET.tostring(root, encoding="utf-8")
        import xml.dom.minidom
        dom = xml.dom.minidom.parseString(xml_bytes)
        pretty = dom.toprettyxml(indent="  ", encoding="utf-8")
        clean_lines = [line for line in pretty.decode("utf-8").splitlines() if line.strip()]
        nfo_path.write_text("\n".join(clean_lines) + "\n", encoding="utf-8")

        # 5. Download HD Poster if available and valid
        if matched.poster_url:
            jpg_path = nfo_path.with_suffix(".jpg")
            client = http_client or _core.httpx
            should_download = False
            if not jpg_path.exists() and not jpg_path.is_symlink():
                should_download = True
            elif jpg_path.is_file() and not jpg_path.is_symlink():
                try:
                    from common.image_utils import is_black_image
                    if is_black_image(jpg_path) or jpg_path.stat().st_size < 15_000:
                        should_download = True
                except Exception:
                    pass
            if client and should_download:
                resp = request_with_host_limit(
                    client, "get", matched.poster_url,
                    headers={"User-Agent": _core.USER_AGENT}, timeout=10.0,
                )
                if resp.status_code == 200 and len(resp.content) > 5000:
                    tmp = jpg_path.with_suffix(".jpg.tmp")
                    tmp.write_bytes(resp.content)
                    tmp.replace(jpg_path)
                    now_ts = _core.time.time()
                    try:
                        _core.os.utime(jpg_path, (now_ts, now_ts))
                    except Exception:
                        pass
    except Exception as e:
        _core.logger.debug("Could not update media files for %s: %s", video_id, e)
