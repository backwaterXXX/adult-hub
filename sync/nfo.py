"""Kodi/Jellyfin NFO metadata generation and performer image lookup."""

import datetime
import json
import logging
import os
from common import db
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, Set
from xml.dom import minidom

logger = logging.getLogger(__name__)


def video_for_nfo(video: Dict[str, Any], conn=None, *, db_url=None):
    """Resolve persistent metadata overrides before writing an NFO file."""
    if isinstance(video.get("metadata_fields"), dict):
        return video
    video_id = video.get("_video_id") or video.get("video_id") or video.get("id")
    if not isinstance(video_id, str) or not video_id:
        return video
    owned = conn is None
    if owned:
        conn = db.connect(db_url)
    try:
        try:
            row = conn.execute(
                "SELECT fields FROM video_metadata_overrides WHERE video_id=%s", (video_id,)
            ).fetchone()
        except db.UndefinedTable:
            row = None
        overrides = row[0] if row and isinstance(row[0], dict) else {}
        if row and isinstance(row[0], str):
            try:
                overrides = json.loads(row[0])
            except (TypeError, ValueError):
                overrides = {}
        from common.video_metadata import resolve_metadata
        return resolve_metadata(video, overrides)
    finally:
        if owned and conn is not None:
            conn.close()


def _metadata_key(element, *, movie_root=False):
    """Compare metadata, ignoring XML formatting and root lockdata placement.

    Ordering remains significant for actors, tags, sets and other repeated
    elements. Only lockdata may move because the migration appends it while
    the generator emits it first.
    """
    children = list(element)
    locks = ()
    if movie_root:
        locks = tuple(_metadata_key(child) for child in children if child.tag == "lockdata")
        children = [child for child in children if child.tag != "lockdata"]
    return (
        element.tag, tuple(sorted(element.attrib.items())),
        (element.text or "").strip(),
        tuple(_metadata_key(child) for child in children), locks,
    )


def format_dateadded(ts) -> str:
    """Epoch -> Kodi/Jellyfin <dateadded> ("YYYY-MM-DD HH:MM:SS", server time)."""
    try:
        return datetime.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def load_performer_thumbs(conn: db.Connection, names) -> Dict[str, str]:
    """{name: photo_url}; loads performer thumbnails from performer_photos."""
    wanted = {str(n).strip().lower() for n in (names or []) if str(n).strip()}
    if not wanted:
        return {}
    out: Dict[str, str] = {}
    found = set()
    try:
        placeholders = ",".join("%s" for _ in wanted)
        rows = conn.execute(
            f"SELECT name, thumb FROM performer_photos WHERE thumb IS NOT NULL AND thumb != '' AND lower(name) IN ({placeholders})",
            tuple(wanted),
        ).fetchall()
        for name, thumb in rows:
            key = str(name).strip()
            if key.lower() in wanted and key.lower() not in found and str(thumb).strip():
                out[key] = str(thumb).strip()
                found.add(key.lower())
    except db.OperationalError as exc:
        logger.debug("Could not load optional performer thumbnails (%s).", type(exc).__name__)
        pass
    return out


def load_performer_links(conn: db.Connection, names) -> Dict[str, Dict[str, str]]:
    """{lower_name: {"stashdb": stashdb_url, "pbc": pbc_url}} from performer_bios."""
    wanted = {str(n).strip().lower() for n in (names or []) if str(n).strip()}
    if not wanted:
        return {}
    out: Dict[str, Dict[str, str]] = {}
    try:
        placeholders = ",".join("%s" for _ in wanted)
        rows = conn.execute(
            f"SELECT name, stashdb_url, pbc_url FROM performer_bios WHERE lower(name) IN ({placeholders})",
            tuple(wanted),
        ).fetchall()
        for name, stashdb_url, pbc_url in rows:
            key = str(name).strip().lower()
            links: Dict[str, str] = {}
            if stashdb_url and str(stashdb_url).strip():
                links["stashdb"] = str(stashdb_url).strip()
            if pbc_url and str(pbc_url).strip():
                links["pbc"] = str(pbc_url).strip()
            if links:
                out[key] = links
    except db.OperationalError as exc:
        logger.debug("Could not load optional performer links (%s).", type(exc).__name__)
        pass
    except Exception as exc:
        logger.debug("Could not load optional performer links (%s).", type(exc).__name__)
        pass
    return out


def create_nfo_file(video: Dict[str, Any], genres: Set[str], tags: Set[str], dest_path: Path, *, get_provider_info_fn, clean_display_title_fn, normalize_performers_fn, map_categories_fn):
    root = ET.Element("movie")

    provider_key = video.get("provider", "telegram")
    if provider_key == "telegram":
        # Local NFO is authoritative. Remote movie matching sees only the
        # generic provider filename and can otherwise import an unrelated film.
        ET.SubElement(root, "lockdata").text = "true"
    pinfo = get_provider_info_fn(provider_key)
    site_name = pinfo["name"]
    channel_name = (video.get("channel") or video.get("studio") or "").strip()
    if channel_name.lower() in (site_name.lower(), "telegram", "t.me"):
        channel_name = ""

    raw_title = video.get("title", "")
    title = raw_title
    perfs = video.get("performers", [])
    provider = video.get("provider", provider_key)
    slug = video.get("slug", "")
    display_title = clean_display_title_fn(raw_title, performers=perfs, provider=provider, slug=slug) or raw_title

    title_elem = ET.SubElement(root, "title")
    title_elem.text = display_title

    orig_title_elem = ET.SubElement(root, "originaltitle")
    orig_title_elem.text = raw_title

    tagline_elem = ET.SubElement(root, "tagline")
    if channel_name:
        tagline_elem.text = f"Kanał: {channel_name}"
    elif site_name and site_name.lower() not in ("telegram", "t.me"):
        tagline_elem.text = f"Source: {site_name}"
    else:
        tagline_elem.text = ""

    # Wytwórnia produkcyjna (MenAtPlay, RS, Dragon Media...) trafia do <studio>
    prod_studios = set(video.get("production_studios") or [])
    for t in tags:
        st = str(t).strip()
        if st.startswith("Studio: "):
            prod_studios.add(st.split("Studio: ", 1)[1].strip())

    try:
        try:
            from sync.studio_tags import detect_production_studio_names
        except ImportError:
            from studio_tags import detect_production_studio_names
        existing_for_detection = list(tags)
        if video.get("studio"):
            existing_for_detection.append(str(video.get("studio")))
        detected_prod = detect_production_studio_names(raw_title, existing_tags=existing_for_detection)
        prod_studios.update(detected_prod)
    except Exception as exc:
        logger.debug("Production-studio detection skipped (%s).", type(exc).__name__)
        pass

    if prod_studios:
        for ps in sorted(prod_studios):
            if ps:
                studio_elem = ET.SubElement(root, "studio")
                studio_elem.text = ps
    elif video.get("production_studio"):
        studio_elem = ET.SubElement(root, "studio")
        studio_elem.text = video["production_studio"]
    elif video.get("studio") and video.get("studio") != channel_name:
        studio_elem = ET.SubElement(root, "studio")
        studio_elem.text = video.get("studio")
    elif provider_key != "telegram" and channel_name:
        studio_elem = ET.SubElement(root, "studio")
        studio_elem.text = channel_name

    # Library addition date (Kodi/Jellyfin <dateadded>).
    dateadded = (video.get("dateadded") or "").strip()
    if dateadded:
        dateadded_elem = ET.SubElement(root, "dateadded")
        dateadded_elem.text = dateadded

    # Official release date (Kodi/Jellyfin <premiered> and <year>).
    date_val = (video.get("date_released") or video.get("premiered") or "").strip()
    if date_val:
        premiered_elem = ET.SubElement(root, "premiered")
        premiered_elem.text = date_val[:10]
        year_str = date_val[:4]
        if year_str.isdigit():
            year_elem = ET.SubElement(root, "year")
            year_elem.text = year_str

    video_url = video.get("url", "")
    scene_match_url = (video.get("scene_match_url") or "").strip()
    if not video_url and video.get("slug"):
        video_url = f"{pinfo['base_url']}/video/{video.get('slug')}"

    effective_website = video_url
    if scene_match_url:
        if not effective_website or provider_key == "telegram":
            effective_website = scene_match_url

    if effective_website:
        website_elem = ET.SubElement(root, "website")
        website_elem.text = effective_website

    plot_elem = ET.SubElement(root, "plot")
    custom_plot = (video.get("plot") or "").strip()
    if custom_plot:
        # Strip any legacy "Source: Telegram (t.me)" occurrences
        custom_plot = custom_plot.replace("Source: Telegram (t.me)", f"Kanał: {channel_name}" if channel_name else "").strip()
        plot_elem.text = custom_plot
    else:
        plot_lines = []
        if channel_name:
            plot_lines.append(f"Kanał: {channel_name}")
        elif site_name and site_name.lower() not in ("telegram", "t.me"):
            plot_lines.append(f"Source: {site_name}")
        if video_url:
            plot_lines.append(f"Video page: {video_url}")
        if video.get("views"):
            plot_lines.append(f"Views: {video.get('views')}")
        if plot_lines:
            plot_lines.append("")
        plot_lines.append(title)
        plot_elem.text = "\n".join(plot_lines)

    duration_sec = video.get("duration", 0)
    if duration_sec > 0:
        runtime_elem = ET.SubElement(root, "runtime")
        runtime_elem.text = str(max(1, duration_sec // 60))

    mapped_genres = map_categories_fn(genres, title=title)
    for g in sorted(mapped_genres):
        if g and not g.startswith("Browse"):
            genre_elem = ET.SubElement(root, "genre")
            genre_elem.text = g

    # Tagi: dodanie kanalu zrodlowego, wykonawcow, oraz scisla filtracja telegram/t.me
    tags_to_add = set()
    if channel_name:
        tags_to_add.add(f"Kanał: {channel_name}")

    def _is_unwanted_tag(val: str) -> bool:
        low = val.lower().strip()
        return not low or low in ("telegram", "t.me") or low.startswith("-100") or low == channel_name.lower().strip()

    for t in tags:
        st = str(t).strip()
        if not _is_unwanted_tag(st):
            tags_to_add.add(st)

    for perf in normalize_performers_fn(video.get("performers", [])):
        st = str(perf).strip()
        if not _is_unwanted_tag(st):
            tags_to_add.add(st)

    for t in sorted(tags_to_add):
        if t:
            tag_elem = ET.SubElement(root, "tag")
            tag_elem.text = t

    _thumbs = video.get("performer_thumbs") or {}
    _thumb_map = {str(k).strip().lower(): str(v).strip()
                  for k, v in _thumbs.items() if str(v).strip()} \
        if isinstance(_thumbs, dict) else {}
    _links = video.get("performer_links") or {}
    _link_map: Dict[str, list] = {}
    if isinstance(_links, dict):
        for k, v in _links.items():
            k_low = str(k).strip().lower()
            if isinstance(v, dict):
                cur_links = []
                if v.get("stashdb"):
                    cur_links.append(str(v["stashdb"]).strip())
                if v.get("pbc"):
                    cur_links.append(str(v["pbc"]).strip())
                for ok, ov in sorted(v.items()):
                    if ok not in ("stashdb", "pbc") and ov and str(ov).strip():
                        cur_links.append(str(ov).strip())
                _link_map[k_low] = cur_links
            elif isinstance(v, (list, tuple)):
                _link_map[k_low] = [str(x).strip() for x in v if str(x).strip()]
            elif isinstance(v, str) and v.strip():
                _link_map[k_low] = [v.strip()]
    # Collections per performer: Jellyfin groups movies under Collections
    # by NFO <set><name> (Kodi format; raw text inside <set> is ignored).
    _nfo_perfs = normalize_performers_fn(video.get("performers", []))
    for perf in _nfo_perfs:
        set_elem = ET.SubElement(root, "set")
        set_name_elem = ET.SubElement(set_elem, "name")
        set_name_elem.text = perf

    for perf in _nfo_perfs:
        actor_elem = ET.SubElement(root, "actor")
        name_elem = ET.SubElement(actor_elem, "name")
        name_elem.text = perf
        role_elem = ET.SubElement(actor_elem, "role")
        role_elem.text = "Performer"
        _thumb_url = _thumb_map.get(perf.strip().lower(), "")
        if _thumb_url:
            thumb_actor_elem = ET.SubElement(actor_elem, "thumb")
            thumb_actor_elem.text = _thumb_url
        for profile_url in _link_map.get(perf.strip().lower(), []):
            if profile_url:
                profile_elem = ET.SubElement(actor_elem, "profile")
                profile_elem.text = profile_url

    thumb_url = video.get("thumbnail")
    # Telegram previews are downloaded to the matching local .jpg sidecar.
    # Do not make NFO ingestion wait for an on-demand Telegram HTTP download:
    # Jellyfin's local image provider can use the sidecar independently.
    if thumb_url and provider_key != "telegram":
        thumb_elem = ET.SubElement(root, "thumb")
        thumb_elem.text = thumb_url

    uid_elem = ET.SubElement(root, "uniqueid", type=provider_key, default="true")
    uid_elem.text = str(video.get("provider_video_id", ""))

    scene_src = str(video.get("scene_match_source") or "").strip().lower()
    scene_ext_id = str(video.get("scene_match_external_id") or video.get("stashdb_id") or "").strip()
    if (scene_src == "stashdb" or scene_src.startswith("stashdb:")) and scene_ext_id:
        stash_uid_elem = ET.SubElement(root, "uniqueid", type="stashdb", default="false")
        stash_uid_elem.text = scene_ext_id

    xml_str = ET.tostring(root, encoding="utf-8")
    parsed = minidom.parseString(xml_str)
    pretty_xml = parsed.toprettyxml(indent="  ", encoding="utf-8")

    # #10: Skip writing if existing NFO is identical (saves I/O and prevents unnecessary Jellyfin rescans)
    if dest_path.exists():
        try:
            with open(dest_path, "rb") as f:
                existing_xml = f.read()
                if existing_xml == pretty_xml:
                    return
                # Migration preserves metadata but may move lockdata and
                # serialize indentation differently. Preserve bytes/mtime so
                # the reconciliation hash does not trigger a needless import.
                if _metadata_key(ET.fromstring(existing_xml), movie_root=True) == \
                        _metadata_key(root, movie_root=True):
                    return
        except (OSError, ET.ParseError):
            pass

    # #29: Atomic write using .nfo.tmp and os.replace to prevent partial reads by Jellyfin
    tmp_path = dest_path.with_suffix(".nfo.tmp")
    try:
        with open(tmp_path, "wb") as f:
            f.write(pretty_xml)
        os.replace(tmp_path, dest_path)
    except OSError:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise
