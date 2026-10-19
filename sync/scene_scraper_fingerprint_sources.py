"""External catalogue lookups by exact OSHASH and approximate PHASH."""

from __future__ import annotations

from typing import Any, Optional

if __package__:
    from . import scene_scrapers as _core
else:
    import scene_scrapers as _core


def search_tpdb_scene_by_oshash(
    fingerprint: str, *, http_client: Optional[Any] = None,
    duration: Optional[int] = None, duration_tolerance: int = 2,
) -> Optional[_core.ScrapedScene]:
    """Use ThePornDB's exact OSHASH endpoint and require a duration match."""
    token = (_core.os.environ.get("THEPORNDB_API_TOKEN", "").strip()
             or _core.os.environ.get("THEPORNDB_API_KEY", "").strip())
    client = http_client or _core.httpx
    if not token or client is None:
        return None
    response = _core.request_with_host_limit(
        client,
        "get",
        f"https://api.theporndb.net/scenes/hash/{fingerprint}",
        params={"type": "OSHASH"},
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                 "User-Agent": _core.USER_AGENT},
        timeout=_core.DEFAULT_TIMEOUT,
    )
    if response.status_code == 404:
        return None
    if callable(getattr(response, "raise_for_status", None)):
        response.raise_for_status()
    payload = response.json() if callable(getattr(response, "json", None)) else _core._decode_scene_payload(response)
    item = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(item, dict) or not str(item.get("title") or "").strip():
        return None
    site = item.get("site") if isinstance(item.get("site"), dict) else {}
    cast = item.get("performers") if isinstance(item.get("performers"), list) else []
    names = [str(person.get("name") or "").strip()
             for person in cast if isinstance(person, dict) and person.get("name")]
    tags = item.get("tags") if isinstance(item.get("tags"), list) else []
    tag_names = [str(tag.get("name") or "").strip()
                 for tag in tags if isinstance(tag, dict) and tag.get("name")]
    image = item.get("poster_image") or item.get("image") or item.get("poster") or ""
    scene = _core.ScrapedScene(
        title=str(item.get("title") or "").strip(),
        plot=str(item.get("description") or "").strip(),
        date=str(item.get("date") or "")[:10],
        duration=int(item.get("duration") or 0),
        poster_url=str(image).strip(), performers=names, tags=tag_names,
        studio=str(site.get("name") or "").strip(),
        external_id=str(item.get("id") or item.get("_id") or ""),
        url=str(item.get("url") or "").strip(), confidence=1.0, source="theporndb:oshash",
    )
    if not _duration_matches(duration, scene.duration, duration_tolerance):
        return None
    return scene


def _duration_matches(
    source_duration: Optional[int], catalog_duration: Optional[int], tolerance: int,
) -> bool:
    """Require known, positive durations that agree within the configured tolerance."""
    try:
        source_seconds = int(source_duration or 0)
        catalog_seconds = int(catalog_duration or 0)
        max_delta = max(0, int(tolerance))
    except (TypeError, ValueError):
        return False
    return (
        source_seconds > 0
        and catalog_seconds > 0
        and abs(source_seconds - catalog_seconds) <= max_delta
    )


def search_stashdb_scene_by_oshash(
    fingerprint: str, *, http_client: Optional[Any] = None,
    duration: Optional[int] = None, duration_tolerance: int = 2,
) -> Optional[_core.ScrapedScene]:
    """Query StashDB by exact OSHASH, requiring duration and one unambiguous scene."""
    return _core._search_stashdb_scene_by_fingerprint(
        fingerprint, "OSHASH", http_client=http_client, duration=duration,
        duration_tolerance=duration_tolerance,
    )


def search_stashdb_scene_by_fingerprint(
    fingerprint: str,
    algorithm: str,
    *,
    http_client: Optional[Any] = None,
    max_distance: int = 0,
    duration: Optional[int] = None,
    duration_tolerance: int = 2,
) -> Optional[_core.ScrapedScene]:
    """Query StashDB by fingerprint and reject ambiguous or distant results."""
    token = (_core.os.environ.get("STASHDB_API_KEY", "").strip()
             or _core.os.environ.get("STASH_API_KEY", "").strip())
    client = http_client or _core.httpx
    if not token or client is None:
        return None
    query = """
    query FindScenesByFingerprint($fingerprints: [[FingerprintQueryInput!]!]!) {
      findScenesBySceneFingerprints(fingerprints: $fingerprints) {
        id title details date release_date duration
        studio { name }
        performers { performer { name } }
        tags { name }
        images { url }
        urls { url }
        fingerprints { hash algorithm }
      }
    }
    """
    response = _core.request_with_host_limit(
        client,
        "post",
        _core.os.environ.get("STASHDB_API_URL", "https://stashdb.org/graphql"),
        headers={"ApiKey": token, "Accept": "application/json", "Content-Type": "application/json",
                 "User-Agent": "AdultHubSync/1.0 (scene-fingerprint)"},
        json={"query": query,
              "variables": {"fingerprints": [[{"algorithm": algorithm, "hash": fingerprint}]]}},
        timeout=_core.DEFAULT_TIMEOUT,
    )
    if callable(getattr(response, "raise_for_status", None)):
        response.raise_for_status()
    payload = response.json() if callable(getattr(response, "json", None)) else _core._decode_scene_payload(response)
    groups = (((payload or {}).get("data") or {}).get("findScenesBySceneFingerprints")
              if isinstance(payload, dict) else None)
    if not isinstance(groups, list) or not groups or not isinstance(groups[0], list):
        return None
    candidates = []
    for scene in groups[0]:
        if not isinstance(scene, dict) or not scene.get("title"):
            continue
        if not _duration_matches(duration, scene.get("duration"), duration_tolerance):
            continue
        distance = 0
        if algorithm == "PHASH":
            stored = scene.get("fingerprints") if isinstance(scene.get("fingerprints"), list) else []
            distances = [
                (int(str(fp.get("hash") or ""), 16) ^ int(fingerprint, 16)).bit_count()
                for fp in stored
                if isinstance(fp, dict)
                and str(fp.get("algorithm") or "").upper() == "PHASH"
                and _core.re.fullmatch(r"[0-9a-fA-F]{16}", str(fp.get("hash") or ""))
            ]
            if not distances:
                continue
            distance = min(distances)
            if distance > max_distance:
                continue
        candidates.append((scene, distance))
    if len(candidates) != 1:
        return None
    item, distance = candidates[0]
    studio = item.get("studio") if isinstance(item.get("studio"), dict) else {}
    performers = item.get("performers") if isinstance(item.get("performers"), list) else []
    names = [str((person.get("performer") or {}).get("name") or "").strip()
             for person in performers
             if isinstance(person, dict) and isinstance(person.get("performer"), dict)]
    tags = item.get("tags") if isinstance(item.get("tags"), list) else []
    images = item.get("images") if isinstance(item.get("images"), list) else []
    urls = item.get("urls") if isinstance(item.get("urls"), list) else []
    return _core.ScrapedScene(
        title=str(item.get("title") or "").strip(),
        plot=str(item.get("details") or "").strip(),
        date=str(item.get("release_date") or item.get("date") or "")[:10],
        duration=int(item.get("duration") or 0),
        poster_url=str((images[0] or {}).get("url") or "")
        if images and isinstance(images[0], dict) else "",
        performers=[name for name in names if name],
        tags=[str(tag.get("name") or "").strip()
              for tag in tags if isinstance(tag, dict) and tag.get("name")],
        studio=str(studio.get("name") or "").strip(),
        external_id=str(item.get("id") or ""),
        url=f"https://stashdb.org/scenes/{item['id']}" if item.get("id") else (str((urls[0] or {}).get("url") or "") if urls and isinstance(urls[0], dict) else ""),
        confidence=1.0 if algorithm == "OSHASH" else max(0.0, 1.0 - distance / 64),
        source=f"stashdb:{algorithm.lower()}",
    )


def search_scene_by_phash(
    fingerprint: str, *, http_client: Optional[Any] = None,
    duration: Optional[int] = None, max_distance: int = 10,
    duration_tolerance: int = 2,
) -> Optional[_core.ScrapedScene]:
    """Find a duration-compatible pHash match; StashDB may return near hashes."""
    if not _core.re.fullmatch(r"[0-9a-fA-F]{16}", str(fingerprint or "")):
        return None
    fingerprint = fingerprint.lower()
    client = http_client or _core.httpx
    errors = []
    try:
        stashdb_scene = _core._search_stashdb_scene_by_fingerprint(
            fingerprint, "PHASH", http_client=client, max_distance=max_distance,
            duration=duration, duration_tolerance=duration_tolerance,
        )
    except Exception as exc:
        errors.append(exc)
        _core.logger.warning("StashDB PHASH lookup failed (%s)", type(exc).__name__)
        stashdb_scene = None
    if stashdb_scene is not None:
        return stashdb_scene

    token = (_core.os.environ.get("THEPORNDB_API_TOKEN", "").strip()
             or _core.os.environ.get("THEPORNDB_API_KEY", "").strip())
    if token and client is not None:
        try:
            response = _core.request_with_host_limit(
                client,
                "get",
                f"https://api.theporndb.net/scenes/hash/{fingerprint}",
                params={"type": "PHASH"},
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                         "User-Agent": _core.USER_AGENT},
                timeout=_core.DEFAULT_TIMEOUT,
            )
            if response.status_code != 404:
                if callable(getattr(response, "raise_for_status", None)):
                    response.raise_for_status()
                payload = response.json() if callable(getattr(response, "json", None)) else _core._decode_scene_payload(response)
                item = payload.get("data") if isinstance(payload, dict) else None
                if isinstance(item, dict) and str(item.get("title") or "").strip():
                    site = item.get("site") if isinstance(item.get("site"), dict) else {}
                    cast = item.get("performers") if isinstance(item.get("performers"), list) else []
                    names = [str(person.get("name") or "").strip()
                             for person in cast if isinstance(person, dict) and person.get("name")]
                    tags = item.get("tags") if isinstance(item.get("tags"), list) else []
                    image = item.get("poster_image") or item.get("image") or item.get("poster") or ""
                    scene = _core.ScrapedScene(
                        title=str(item.get("title") or "").strip(),
                        plot=str(item.get("description") or "").strip(),
                        date=str(item.get("date") or "")[:10],
                        duration=int(item.get("duration") or 0),
                        poster_url=str(image).strip(), performers=names,
                        tags=[str(tag.get("name") or "").strip()
                              for tag in tags if isinstance(tag, dict) and tag.get("name")],
                        studio=str(site.get("name") or "").strip(),
                        external_id=str(item.get("id") or item.get("_id") or ""),
                        url=str(item.get("url") or "").strip(), confidence=1.0,
                        source="theporndb:phash",
                    )
                    if _duration_matches(duration, scene.duration, duration_tolerance):
                        return scene
        except Exception as exc:
            errors.append(exc)
            _core.logger.warning("ThePornDB PHASH lookup failed (%s)", type(exc).__name__)
    if errors:
        raise errors[0]
    return None
