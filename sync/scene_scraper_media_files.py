"""Persist matched scene metadata to NFO sidecars and artwork files."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, List, Optional
import logging
import xml.dom.minidom
import xml.etree.ElementTree as ET
from common.http_rate_limit import request_with_host_limit

logger = logging.getLogger(__name__)


from sync.scene_scraper_models import ScrapedScene  # noqa: E402

from sync import scene_scrapers as _core  # noqa: E402


class _MediaTargets:
    """Files found for one video. Partially filled when the lookup stops early."""

    def __init__(self) -> None:
        self.poster_path: Optional[Path] = None
        self.registered_files = False
        self.nfo_paths: List[Path] = []


def _collect_registered_nfos(conn: Any, video_id: str, media_dir: Path, strict: bool,
                             targets: _MediaTargets) -> None:
    from sync.media_files import resolve_media_path
    file_rows = conn.execute(
        "SELECT file_path FROM synced_files WHERE video_id=%s ORDER BY file_path", (video_id,)
    ).fetchall()
    targets.registered_files = bool(file_rows)
    if strict and not targets.registered_files:
        raise FileNotFoundError(f"No registered media file for {video_id}")
    for (file_path,) in file_rows:
        stream_path = resolve_media_path(str(file_path), media_dir)
        if targets.poster_path is None:
            targets.poster_path = stream_path.with_suffix(".jpg")
        candidate = stream_path.with_suffix(".nfo")
        if candidate.is_file() and not candidate.is_symlink():
            if candidate not in targets.nfo_paths:
                targets.nfo_paths.append(candidate)
        elif strict:
            raise FileNotFoundError(f"NFO missing or unsafe for {video_id}")


def _update_each_nfo(media_dir: Path, video_id: str, matched: ScrapedScene, performers: List[str],
                     http_client: Optional[Any], conn: Any, strict: bool, nfo_paths: List[Path]) -> bool:
    saved = False
    for path in nfo_paths:
        saved = _update_media_files(media_dir, video_id, matched, performers,
                                    http_client, conn, strict, path) or saved
    return saved


def _no_unique_nfo(video_id: str, matched: ScrapedScene, strict: bool, targets: _MediaTargets,
                   http_client: Optional[Any]) -> bool:
    """Legacy fallback when the NFO cannot be identified uniquely: only the poster can be saved."""
    if strict and targets.registered_files:
        raise FileNotFoundError(f"NFO not found for {video_id}")
    if targets.poster_path is not None and matched.poster_url:
        return _download_scene_poster(
            matched.poster_url, targets.poster_path, video_id, http_client=http_client,
        )
    return False


def _resolve_media_targets(conn: Optional[Any], video_id: str, media_dir: Path, matched: ScrapedScene,
                           performers: List[str], http_client: Optional[Any], strict: bool,
                           nfo_path_arg: Optional[Path]):
    """Return (nfo_path, poster_path, early_result). A non-None early_result ends the update."""
    targets = _MediaTargets()
    nfo_path = nfo_path_arg
    if conn is not None and nfo_path_arg is None:
        try:
            _collect_registered_nfos(conn, video_id, media_dir, strict, targets)
            if len(targets.nfo_paths) > 1:
                return None, None, _update_each_nfo(media_dir, video_id, matched, performers,
                                                    http_client, conn, strict, targets.nfo_paths)
            if targets.nfo_paths:
                nfo_path = targets.nfo_paths[0]
        except Exception:
            if strict:
                raise
            nfo_path = None
    if nfo_path is None:
        # Compatibility for legacy files that predate the synced_files index.
        parts = video_id.split(":")
        comp_tail = parts[-2] if len(parts) > 2 else parts[-1]
        matches = list(media_dir.glob(f"*{comp_tail}*.nfo"))
        if len(matches) != 1:
            return None, None, _no_unique_nfo(video_id, matched, strict, targets, http_client)
        nfo_path = matches[0]
        if nfo_path.is_symlink():
            raise ValueError(f"Unsafe NFO for {video_id}")
    poster_path = targets.poster_path
    if poster_path is None:
        poster_path = nfo_path.with_suffix(".jpg")
    return nfo_path, poster_path, None


def _load_media_overrides(conn: Optional[Any], video_id: str) -> dict:
    if conn is None:
        return {}
    try:
        row = conn.execute(
            "SELECT fields FROM video_metadata_overrides WHERE video_id=%s", (video_id,)
        ).fetchone()
        overrides = row[0] if row and isinstance(row[0], dict) else {}
        if row and isinstance(row[0], str):
            overrides = _core.json.loads(row[0])
    except Exception:
        return {}
    return overrides


def _is_trusted_scene_source(source: Any) -> bool:
    return str(source or '').casefold() in {
        'stashdb:phash', 'stashdb:oshash', 'theporndb:phash', 'theporndb:oshash',
        'stashdb:title_exact', 'stashdb:linked_id', 'stashdb:split_title_duration_v1',
        'theporndb:title_exact', 'theporndb:linked_id', 'theporndb:split_title_duration_v1',
    }


def _apply_title(root: Any, matched: ScrapedScene, overrides: dict) -> None:
    # Exact hash identity can safely supply a better title. A confirmed
    # StashDB/ThePornDB scene match is the canonical title source, so it
    # also replaces the provider caption here: the NFO is what Jellyfin
    # imports for locked Telegram entries. Keep the operator's value
    # wherever an explicit metadata override exists.
    if matched.title and "title" not in overrides:
        title_elem = root.find("title")
        catalog_match = any(
            provider in str(matched.source or "").casefold()
            for provider in ("stashdb", "theporndb", "tpdb")
        )
        if title_elem is not None and (
                not str(title_elem.text or "").strip() or catalog_match):
            title_elem.text = matched.title


def _apply_plot(root: Any, matched: ScrapedScene, overrides: dict, trusted: bool) -> None:
    if matched.plot and "plot" not in overrides:
        plot_elem = root.find("plot")
        if plot_elem is not None and (trusted or not str(plot_elem.text or "").strip()):
            plot_elem.text = matched.plot
        elif plot_elem is None:
            ET.SubElement(root, "plot").text = matched.plot


def _apply_release_date(root: Any, matched: ScrapedScene, overrides: dict, trusted: bool) -> None:
    if matched.date and "date_released" not in overrides:
        prem_elem = root.find("premiered")
        if prem_elem is not None and (trusted or not str(prem_elem.text or "").strip()):
            prem_elem.text = matched.date
        elif prem_elem is None:
            ET.SubElement(root, "premiered").text = matched.date

        year = matched.date[:4]
        if year.isdigit():
            yr_elem = root.find("year")
            if yr_elem is not None and (trusted or not str(yr_elem.text or "").strip()):
                yr_elem.text = year
            elif yr_elem is None:
                ET.SubElement(root, "year").text = year


def _load_actor_links(conn: Optional[Any], root: Any, matched: ScrapedScene) -> dict:
    if conn is None:
        return {}
    try:
        from sync.nfo import load_performer_links
        all_actor_names = [a.findtext("name") for a in root.findall("actor") if a.findtext("name")]
        if matched.performers:
            all_actor_names.extend(matched.performers)
        return load_performer_links(conn, all_actor_names)
    except Exception as e:
        _core.logger.debug("Could not load performer links: %s", e)
        return {}


def _add_scene_performers(root: Any, performers: List[str], actor_links: dict) -> None:
    for perf in performers:
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


def _apply_performers(root: Any, matched: ScrapedScene, overrides: dict, trusted: bool,
                      actor_links: dict) -> None:
    if matched.performers and "performers" not in overrides:
        if trusted:
            old_names = {str(actor.findtext('name') or '').strip().casefold() for actor in root.findall('actor')}
            for actor in root.findall('actor'):
                root.remove(actor)
            for item in root.findall('set'):
                if str(item.findtext('name') or '').strip().casefold() in old_names:
                    root.remove(item)
        has_actor = any(str(actor.findtext("name") or "").strip() for actor in root.findall("actor"))
        if not has_actor:
            _add_scene_performers(root, matched.performers, actor_links)


def _merge_actor_profiles(root: Any, actor_links: dict) -> None:
    if not actor_links:
        return
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


def _apply_studio(root: Any, matched: ScrapedScene, overrides: dict, trusted: bool) -> None:
    if matched.studio and "studio" not in overrides:
        studios = root.findall("studio")
        if trusted or not any(str(studio.text or "").strip() for studio in studios):
            if studios:
                studios[0].text = matched.studio
                if trusted:
                    for studio in studios[1:]:
                        root.remove(studio)
            else:
                ET.SubElement(root, "studio").text = matched.studio


def _apply_tags(root: Any, matched: ScrapedScene, overrides: dict, trusted: bool) -> None:
    if matched.tags and "tags" not in overrides:
        if trusted:
            for tag in root.findall('tag'):
                root.remove(tag)
        known_tags = {t.text for t in root.findall("tag") if t.text}
        for tag in matched.tags:
            if tag and tag not in known_tags:
                ET.SubElement(root, "tag").text = tag
                known_tags.add(tag)


def _apply_provider_uniqueid(root: Any, matched: ScrapedScene) -> None:
    # Keep the provider identity in the NFO so Jellyfin artwork sync can
    # recognize externally matched scenes for both supported providers.
    source_lower = str(matched.source or "").casefold()
    if matched.external_id and ("stashdb" in source_lower or "theporndb" in source_lower):
        provider_name = "stashdb" if "stashdb" in source_lower else "theporndb"
        provider_uid = None
        for uid in root.findall("uniqueid"):
            if uid.get("type") == provider_name:
                provider_uid = uid
                break
        if provider_uid is not None:
            provider_uid.text = matched.external_id
        else:
            ET.SubElement(root, "uniqueid", type=provider_name, default="false").text = matched.external_id


def _apply_website(root: Any, matched: ScrapedScene) -> None:
    if matched.url:
        web_elem = root.find("website")
        if web_elem is not None:
            if not (web_elem.text or "").strip() or "t.me" in (web_elem.text or ""):
                web_elem.text = matched.url
        else:
            ET.SubElement(root, "website").text = matched.url


def _write_nfo_file(nfo_path: Path, root: Any) -> None:
    # 4. Write updated NFO
    xml_bytes = ET.tostring(root, encoding="utf-8")
    dom = xml.dom.minidom.parseString(xml_bytes)
    pretty = dom.toprettyxml(indent="  ", encoding="utf-8")
    clean_lines = [line for line in pretty.decode("utf-8").splitlines() if line.strip()]
    nfo_path.write_text("\n".join(clean_lines) + "\n", encoding="utf-8")


def _patch_nfo_file(conn: Optional[Any], video_id: str, matched: ScrapedScene, performers: List[str],
                    nfo_path: Path) -> None:
    text = nfo_path.read_text(encoding="utf-8")
    root = ET.fromstring(text)
    overrides = _load_media_overrides(conn, video_id)
    trusted = _is_trusted_scene_source(matched.source)
    _apply_title(root, matched, overrides)
    _apply_plot(root, matched, overrides, trusted)
    _apply_release_date(root, matched, overrides, trusted)
    # 3. Update <actor>, <set> and <tag> with official studio performers
    actor_links = _load_actor_links(conn, root, matched)
    _apply_performers(root, matched, overrides, trusted, actor_links)
    _merge_actor_profiles(root, actor_links)
    _apply_studio(root, matched, overrides, trusted)
    _apply_tags(root, matched, overrides, trusted)
    _apply_provider_uniqueid(root, matched)
    _apply_website(root, matched)
    _write_nfo_file(nfo_path, root)


def _update_media_files(
    media_dir: Path,
    video_id: str,
    matched: ScrapedScene,
    performers: List[str],
    http_client: Optional[Any] = None,
    conn: Optional[Any] = None,
    strict: bool = False,
    _nfo_path: Optional[Path] = None,
) -> bool:
    """Update the NFO and report whether a valid external poster was saved."""
    poster_saved = False
    try:
        nfo_path, poster_path, early_result = _resolve_media_targets(
            conn, video_id, media_dir, matched, performers, http_client, strict, _nfo_path,
        )
        if early_result is not None:
            return early_result
        _patch_nfo_file(conn, video_id, matched, performers, nfo_path)
        # 5. Download and refresh the linked scene poster when available.
        if matched.poster_url:
            poster_saved = _download_scene_poster(
                matched.poster_url, poster_path, video_id, http_client=http_client,
            )
    except Exception as e:
        if strict:
            raise
        _core.logger.debug("Could not update media files for %s: %s", video_id, e)
    return poster_saved


def _download_scene_poster(url: str, jpg_path: Path, video_id: str,
                           http_client: Optional[Any] = None) -> bool:
    """Validate and save a linked scene poster, even when the NFO is absent."""
    client = http_client or _core.httpx
    if not client or jpg_path.is_symlink():
        return False
    try:
        resp = request_with_host_limit(
            client, "get", url,
            headers={"User-Agent": _core.USER_AGENT}, timeout=10.0, follow_redirects=True,
        )
        if resp.status_code != 200 or len(resp.content) <= 5000:
            _core.logger.warning("Linked poster download failed for %s (HTTP %s, %s bytes)",
                                 video_id, resp.status_code, len(resp.content))
            return False
        from io import BytesIO
        from PIL import Image
        from common.image_utils import is_black_image
        with Image.open(BytesIO(resp.content)) as downloaded:
            downloaded.load()
            if is_black_image(downloaded):
                _core.logger.warning("Linked poster rejected for %s (black image)", video_id)
                return False
            output = BytesIO()
            downloaded.convert("RGB").save(output, format="JPEG", quality=92, optimize=True)
        image_bytes = output.getvalue()
        if len(image_bytes) <= 5000:
            _core.logger.warning("Linked poster rejected for %s (encoded image too small)", video_id)
            return False
        tmp = jpg_path.with_suffix(".jpg.tmp")
        tmp.write_bytes(image_bytes)
        tmp.replace(jpg_path)
        marker = jpg_path.with_suffix(".jpg.external")
        marker_tmp = marker.with_suffix(marker.suffix + ".tmp")
        marker_tmp.write_text(hashlib.sha256(image_bytes).hexdigest(), encoding="ascii")
        marker_tmp.replace(marker)
        try:
            _core.os.utime(jpg_path, (_core.time.time(), _core.time.time()))
        except Exception:
            logger.debug("Ignored exception in _download_scene_poster", exc_info=True)
        return True
    except Exception as image_error:
        _core.logger.warning("Could not save linked poster for %s (%s)", video_id,
                           type(image_error).__name__)
        return False
