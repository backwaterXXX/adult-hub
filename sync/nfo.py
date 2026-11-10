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
    except Exception as exc:
        logger.debug("Could not load optional performer links (%s).", type(exc).__name__)
    return out


def _nfo_channel_name(video: Dict[str, Any], site_name: str) -> str:
    channel_name = (video.get("channel") or video.get("studio") or "").strip()
    if channel_name.lower() in (site_name.lower(), "telegram", "t.me"):
        channel_name = ""
    return channel_name


def _add_title_elements(root, display_title: str, raw_title: str, original_title) -> None:
    title_elem = ET.SubElement(root, "title")
    title_elem.text = display_title

    # Gdy tytul zostal przejety przez dopasowanie katalogowe, <title> nosi
    # nazwe ze StashDB/ThePornDB, a podpis dostawcy zostaje w <originaltitle>.
    orig_title_elem = ET.SubElement(root, "originaltitle")
    orig_title_elem.text = str(original_title or raw_title)


def _add_tagline(root, channel_name: str, site_name: str) -> None:
    tagline_elem = ET.SubElement(root, "tagline")
    if channel_name:
        tagline_elem.text = f"Kanał: {channel_name}"
    elif site_name and site_name.lower() not in ("telegram", "t.me"):
        tagline_elem.text = f"Source: {site_name}"
    else:
        tagline_elem.text = ""


def _production_studio_names(video: Dict[str, Any], tags: Set[str], raw_title: str) -> Set[str]:
    # Wytwórnia produkcyjna (MenAtPlay, RS, Dragon Media...) trafia do <studio>
    prod_studios = set(video.get("production_studios") or [])
    for t in tags:
        st = str(t).strip()
        if st.startswith("Studio: "):
            prod_studios.add(st.split("Studio: ", 1)[1].strip())

    try:
        from sync.studio_tags import detect_production_studio_names
        existing_for_detection = list(tags)
        if video.get("studio"):
            existing_for_detection.append(str(video.get("studio")))
        detected_prod = detect_production_studio_names(raw_title, existing_tags=existing_for_detection)
        prod_studios.update(detected_prod)
    except Exception as exc:
        logger.debug("Production-studio detection skipped (%s).", type(exc).__name__)
    return prod_studios


def _add_studio_elements(root, video: Dict[str, Any], prod_studios: Set[str], provider_key: str,
                         channel_name: str) -> None:
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


def _add_date_elements(root, video: Dict[str, Any]) -> None:
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


def _add_website_element(root, video: Dict[str, Any], provider_key: str, pinfo) -> str:
    """Add <website> when one applies and return the resolved video page URL."""
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
    return video_url


def _add_plot_element(root, video: Dict[str, Any], channel_name: str, site_name: str,
                      video_url: str, title: str) -> None:
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


def _add_runtime_element(root, video: Dict[str, Any]) -> None:
    duration_sec = video.get("duration", 0)
    if duration_sec > 0:
        runtime_elem = ET.SubElement(root, "runtime")
        runtime_elem.text = str(max(1, duration_sec // 60))


def _add_genre_elements(root, genres: Set[str], title: str, map_categories_fn) -> None:
    mapped_genres = map_categories_fn(genres, title=title)
    for g in sorted(mapped_genres):
        if g and not g.startswith("Browse"):
            genre_elem = ET.SubElement(root, "genre")
            genre_elem.text = g


def _is_unwanted_tag(val: str, channel_name: str) -> bool:
    low = val.lower().strip()
    return not low or low in ("telegram", "t.me") or low.startswith("-100") or low == channel_name.lower().strip()


def _add_tag_elements(root, video: Dict[str, Any], tags: Set[str], channel_name: str,
                      normalize_performers_fn) -> None:
    # Tagi: dodanie kanalu zrodlowego, wykonawcow, oraz scisla filtracja telegram/t.me
    tags_to_add = set()
    if channel_name:
        tags_to_add.add(f"Kanał: {channel_name}")

    for t in tags:
        st = str(t).strip()
        if not _is_unwanted_tag(st, channel_name):
            tags_to_add.add(st)

    for perf in normalize_performers_fn(video.get("performers", [])):
        st = str(perf).strip()
        if not _is_unwanted_tag(st, channel_name):
            tags_to_add.add(st)

    for t in sorted(tags_to_add):
        if t:
            tag_elem = ET.SubElement(root, "tag")
            tag_elem.text = t


def _performer_thumb_map(thumbs) -> Dict[str, str]:
    return {str(k).strip().lower(): str(v).strip()
            for k, v in thumbs.items() if str(v).strip()} \
        if isinstance(thumbs, dict) else {}


def _performer_link_urls(value):
    """Profile URLs for one performer link entry, or None when the entry is not recognised."""
    if isinstance(value, dict):
        cur_links = []
        if value.get("stashdb"):
            cur_links.append(str(value["stashdb"]).strip())
        if value.get("pbc"):
            cur_links.append(str(value["pbc"]).strip())
        for ok, ov in sorted(value.items()):
            if ok not in ("stashdb", "pbc") and ov and str(ov).strip():
                cur_links.append(str(ov).strip())
        return cur_links
    if isinstance(value, (list, tuple)):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return None


def _performer_link_map(links) -> Dict[str, list]:
    link_map: Dict[str, list] = {}
    if isinstance(links, dict):
        for k, v in links.items():
            urls = _performer_link_urls(v)
            if urls is not None:
                link_map[str(k).strip().lower()] = urls
    return link_map


def _add_performer_elements(root, perfs, thumb_map: Dict[str, str], link_map: Dict[str, list]) -> None:
    # Collections per performer: Jellyfin groups movies under Collections
    # by NFO <set><name> (Kodi format; raw text inside <set> is ignored).
    for perf in perfs:
        set_elem = ET.SubElement(root, "set")
        set_name_elem = ET.SubElement(set_elem, "name")
        set_name_elem.text = perf

    for perf in perfs:
        actor_elem = ET.SubElement(root, "actor")
        name_elem = ET.SubElement(actor_elem, "name")
        name_elem.text = perf
        role_elem = ET.SubElement(actor_elem, "role")
        role_elem.text = "Performer"
        _thumb_url = thumb_map.get(perf.strip().lower(), "")
        if _thumb_url:
            thumb_actor_elem = ET.SubElement(actor_elem, "thumb")
            thumb_actor_elem.text = _thumb_url
        for profile_url in link_map.get(perf.strip().lower(), []):
            if profile_url:
                profile_elem = ET.SubElement(actor_elem, "profile")
                profile_elem.text = profile_url


def _add_thumb_element(root, video: Dict[str, Any], provider_key: str) -> None:
    thumb_url = video.get("thumbnail")
    # Telegram previews are downloaded to the matching local .jpg sidecar.
    # Do not make NFO ingestion wait for an on-demand Telegram HTTP download:
    # Jellyfin's local image provider can use the sidecar independently.
    if thumb_url and provider_key != "telegram":
        thumb_elem = ET.SubElement(root, "thumb")
        thumb_elem.text = thumb_url


def _add_uniqueid_elements(root, video: Dict[str, Any], provider_key: str) -> None:
    uid_elem = ET.SubElement(root, "uniqueid", type=provider_key, default="true")
    uid_elem.text = str(video.get("provider_video_id", ""))

    scene_src = str(video.get("scene_match_source") or "").strip().lower()
    scene_ext_id = str(video.get("scene_match_external_id") or video.get("stashdb_id") or "").strip()
    if scene_ext_id:
        if scene_src == "stashdb" or scene_src.startswith("stashdb:"):
            catalog_uid_type = "stashdb"
        elif "theporndb" in scene_src or scene_src.startswith("tpdb"):
            catalog_uid_type = "theporndb"
        else:
            catalog_uid_type = ""
        if catalog_uid_type:
            catalog_uid_elem = ET.SubElement(root, "uniqueid", type=catalog_uid_type, default="false")
            catalog_uid_elem.text = scene_ext_id


def _existing_nfo_is_current(dest_path: Path, root, pretty_xml: bytes) -> bool:
    """True when the NFO on disk already says the same thing, so no write is needed."""
    if dest_path.exists():
        try:
            with open(dest_path, "rb") as f:
                existing_xml = f.read()
                if existing_xml == pretty_xml:
                    return True
                # Migration preserves metadata but may move lockdata and
                # serialize indentation differently. Preserve bytes/mtime so
                # the reconciliation hash does not trigger a needless import.
                return _metadata_key(ET.fromstring(existing_xml), movie_root=True) == \
                    _metadata_key(root, movie_root=True)
        except (OSError, ET.ParseError):
            pass
    return False


def _write_nfo_atomically(dest_path: Path, pretty_xml: bytes) -> None:
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


def create_nfo_file(video: Dict[str, Any], genres: Set[str], tags: Set[str], dest_path: Path, *, get_provider_info_fn, clean_display_title_fn, normalize_performers_fn, map_categories_fn):
    root = ET.Element("movie")

    provider_key = video.get("provider", "telegram")
    if provider_key == "telegram":
        # Local NFO is authoritative. Remote movie matching sees only the
        # generic provider filename and can otherwise import an unrelated film.
        ET.SubElement(root, "lockdata").text = "true"
    pinfo = get_provider_info_fn(provider_key)
    site_name = pinfo["name"]
    channel_name = _nfo_channel_name(video, site_name)

    raw_title = video.get("title", "")
    title = raw_title
    perfs = video.get("performers", [])
    provider = video.get("provider", provider_key)
    slug = video.get("slug", "")
    display_title = clean_display_title_fn(raw_title, performers=perfs, provider=provider, slug=slug) or raw_title

    _add_title_elements(root, display_title, raw_title, video.get("original_title"))
    _add_tagline(root, channel_name, site_name)
    prod_studios = _production_studio_names(video, tags, raw_title)
    _add_studio_elements(root, video, prod_studios, provider_key, channel_name)
    _add_date_elements(root, video)
    video_url = _add_website_element(root, video, provider_key, pinfo)
    _add_plot_element(root, video, channel_name, site_name, video_url, title)
    _add_runtime_element(root, video)
    _add_genre_elements(root, genres, title, map_categories_fn)
    _add_tag_elements(root, video, tags, channel_name, normalize_performers_fn)

    thumb_map = _performer_thumb_map(video.get("performer_thumbs") or {})
    link_map = _performer_link_map(video.get("performer_links") or {})
    _add_performer_elements(root, normalize_performers_fn(video.get("performers", [])), thumb_map, link_map)
    _add_thumb_element(root, video, provider_key)
    _add_uniqueid_elements(root, video, provider_key)

    xml_str = ET.tostring(root, encoding="utf-8")
    parsed = minidom.parseString(xml_str)
    pretty_xml = parsed.toprettyxml(indent="  ", encoding="utf-8")

    # #10: Skip writing if existing NFO is identical (saves I/O and prevents unnecessary Jellyfin rescans)
    if _existing_nfo_is_current(dest_path, root, pretty_xml):
        return
    _write_nfo_atomically(dest_path, pretty_xml)
