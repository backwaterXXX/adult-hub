"""Conservative scene searches driven by extracted Telegram text."""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import quote

from common.scene_titles import SceneLookupUnavailable, lookup_title_variants
from sync import scene_scrapers as _core


def _title_key(value: str) -> str:
    """Normalize only punctuation and spacing; keep every title token."""
    return "".join(_core.re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def search_tpdb_scene_by_exact_title(
    title: str,
    *,
    duration: int,
    http_client: Optional[Any] = None,
    duration_tolerance: int = 2,
) -> Optional[_core.ScrapedScene]:
    """Accept a title lookup only when title and runtime identify one TPDB row."""
    query_title = str(title or "").strip()
    query_key = _title_key(query_title)
    try:
        runtime = int(duration or 0)
        tolerance = max(0, int(duration_tolerance))
    except (TypeError, ValueError):
        return None
    if not query_key or runtime <= 0:
        return None

    token = (_core.os.environ.get("THEPORNDB_API_TOKEN", "").strip()
             or _core.os.environ.get("THEPORNDB_API_KEY", "").strip())
    client = http_client or _core.httpx
    if not token or client is None:
        return None

    results = _tpdb_title_results(query_title, token, client)
    if results is None:
        return None
    candidates = _tpdb_exact_matches(results, query_key, runtime, tolerance)
    return candidates[0] if len(candidates) == 1 else None


def _tpdb_title_results(query_title: str, token: str, client) -> Optional[list]:
    response = _core.request_with_host_limit(
        client,
        "get",
        "https://api.theporndb.net/scenes",
        params={"q": query_title, "per_page": 10, "page": 1},
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                 "User-Agent": _core.USER_AGENT},
        timeout=_core.DEFAULT_TIMEOUT,
    )
    if callable(getattr(response, "raise_for_status", None)):
        response.raise_for_status()
    payload = response.json() if callable(getattr(response, "json", None)) else _core._decode_scene_payload(response)
    results = payload.get("data") if isinstance(payload, dict) else None
    return results if isinstance(results, list) else None


def _tpdb_exact_matches(results: list, query_key: str, runtime: int, tolerance: int) -> list:
    """Return ScrapedScene candidates whose normalized title and runtime match, de-duplicated."""
    candidates = []
    seen_ids = set()
    for item in results:
        if not isinstance(item, dict):
            continue
        candidate_title = str(item.get("title") or "").strip()
        candidate_key = _title_key(candidate_title)
        try:
            candidate_duration = int(item.get("duration") or 0)
        except (TypeError, ValueError):
            continue
        if (not candidate_key or candidate_key != query_key or candidate_duration <= 0
                or abs(candidate_duration - runtime) > tolerance):
            continue
        external_id = str(item.get("id") or item.get("_id") or "")
        dedupe_key = external_id or f"{candidate_key}:{candidate_duration}"
        if dedupe_key in seen_ids:
            continue
        seen_ids.add(dedupe_key)

        site = item.get("site") if isinstance(item.get("site"), dict) else {}
        cast = item.get("performers") if isinstance(item.get("performers"), list) else []
        names = [str(person.get("name") or "").strip()
                 for person in cast if isinstance(person, dict) and person.get("name")]
        tags = item.get("tags") if isinstance(item.get("tags"), list) else []
        tag_names = [str(tag.get("name") or "").strip()
                     for tag in tags if isinstance(tag, dict) and tag.get("name")]
        image = item.get("poster_image") or item.get("image") or item.get("poster") or ""
        candidates.append(_core.ScrapedScene(
            title=candidate_title,
            plot=str(item.get("description") or "").strip(),
            date=str(item.get("date") or "")[:10],
            duration=candidate_duration,
            poster_url=str(image).strip(),
            performers=names,
            tags=tag_names,
            studio=str(site.get("name") or "").strip(),
            external_id=external_id,
            url=str(item.get("url") or "").strip(),
            confidence=0.99,
            source="theporndb:title_exact",
        ))
    return candidates


def fetch_tpdb_scene_by_id(
    external_id: str,
    *,
    http_client: Optional[Any] = None,
) -> Optional[_core.ScrapedScene]:
    """Fetch a linked ThePornDB scene directly by its saved ID."""
    scene_id = str(external_id or "").strip()
    token = (_core.os.environ.get("THEPORNDB_API_TOKEN", "").strip()
             or _core.os.environ.get("THEPORNDB_API_KEY", "").strip())
    client = http_client or _core.httpx
    if not scene_id or not token or client is None:
        return None
    response = _core.request_with_host_limit(
        client, "get", f"https://api.theporndb.net/scenes/{quote(scene_id, safe='')}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                 "User-Agent": _core.USER_AGENT},
        timeout=_core.DEFAULT_TIMEOUT,
    )
    if callable(getattr(response, "raise_for_status", None)):
        response.raise_for_status()
    payload = response.json() if callable(getattr(response, "json", None)) else _core._decode_scene_payload(response)
    item = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(item, dict):
        return None
    site = item.get("site") if isinstance(item.get("site"), dict) else {}
    image = item.get("poster_image") or item.get("image") or item.get("poster") or ""
    return _core.ScrapedScene(
        title=str(item.get("title") or ""),
        plot=str(item.get("description") or ""),
        date=str(item.get("date") or "")[:10],
        duration=int(item.get("duration") or 0),
        poster_url=str(image).strip(),
        studio=str(site.get("name") or ""),
        external_id=str(item.get("id") or item.get("_id") or scene_id),
        url=str(item.get("url") or f"https://theporndb.net/scenes/{scene_id}"),
        confidence=1.0,
        source="theporndb:linked_id",
    )


def search_stashdb_scene_by_exact_title(
    title: str,
    *,
    duration: int,
    http_client: Optional[Any] = None,
    duration_tolerance: int = 2,
) -> Optional[_core.ScrapedScene]:
    """Search StashDB by title and require one exact-title/runtime result."""
    query_title = str(title or "").strip()
    query_key = _title_key(query_title)
    try:
        runtime = int(duration or 0)
        tolerance = max(0, int(duration_tolerance))
    except (TypeError, ValueError):
        return None
    token = (_core.os.environ.get("STASHDB_API_KEY", "").strip()
             or _core.os.environ.get("STASH_API_KEY", "").strip())
    client = http_client or _core.httpx
    if not query_key or runtime <= 0 or not token or client is None:
        return None

    query = """
    query SearchScenesByTitle($term: String!, $limit: Int) {
      searchScenes(term: $term, limit: $limit) {
        count
        scenes {
          id title details date release_date duration
          studio { name }
          performers { performer { name } }
          tags { name }
          images { url }
          urls { url }
        }
      }
    }
    """
    response = _core.request_with_host_limit(
        client,
        "post",
        _core.os.environ.get("STASHDB_API_URL", "https://stashdb.org/graphql"),
        headers={"ApiKey": token, "Accept": "application/json", "Content-Type": "application/json",
                 "User-Agent": "AdultHubSync/1.0 (scene-title-exact)"},
        json={"query": query, "variables": {"term": query_title, "limit": 10}},
        timeout=_core.DEFAULT_TIMEOUT,
    )
    if callable(getattr(response, "raise_for_status", None)):
        response.raise_for_status()
    payload = response.json() if callable(getattr(response, "json", None)) else _core._decode_scene_payload(response)
    result = (((payload or {}).get("data") or {}).get("searchScenes")
              if isinstance(payload, dict) else None)
    scenes = result.get("scenes") if isinstance(result, dict) else None
    if not isinstance(scenes, list):
        return None

    candidates = []
    seen_ids = set()
    for item in scenes:
        if not isinstance(item, dict):
            continue
        candidate_title = str(item.get("title") or "").strip()
        try:
            candidate_duration = int(item.get("duration") or 0)
        except (TypeError, ValueError):
            continue
        if (_title_key(candidate_title) != query_key or candidate_duration <= 0
                or abs(candidate_duration - runtime) > tolerance):
            continue
        external_id = str(item.get("id") or "")
        if external_id and external_id in seen_ids:
            continue
        seen_ids.add(external_id)
        studio = item.get("studio") if isinstance(item.get("studio"), dict) else {}
        appearances = item.get("performers") if isinstance(item.get("performers"), list) else []
        names = [str((appearance.get("performer") or {}).get("name") or "").strip()
                 for appearance in appearances
                 if isinstance(appearance, dict) and isinstance(appearance.get("performer"), dict)]
        tags = item.get("tags") if isinstance(item.get("tags"), list) else []
        images = item.get("images") if isinstance(item.get("images"), list) else []
        candidates.append(_core.ScrapedScene(
            title=candidate_title,
            plot=str(item.get("details") or "").strip(),
            date=str(item.get("release_date") or item.get("date") or "")[:10],
            duration=candidate_duration,
            poster_url=str((images[0] or {}).get("url") or "")
            if images and isinstance(images[0], dict) else "",
            performers=[name for name in names if name],
            tags=[str(tag.get("name") or "").strip()
                  for tag in tags if isinstance(tag, dict) and tag.get("name")],
            studio=str(studio.get("name") or "").strip(),
            external_id=external_id,
            url=f"https://stashdb.org/scenes/{external_id}" if external_id else "",
            confidence=0.99,
            source="stashdb:title_exact",
        ))
    return candidates[0] if len(candidates) == 1 else None


def fetch_stashdb_scene_by_id(
    external_id: str,
    *,
    http_client: Optional[Any] = None,
) -> Optional[_core.ScrapedScene]:
    """Fetch a linked StashDB scene directly by its saved ID."""
    scene_id = str(external_id or "").strip()
    token = (_core.os.environ.get("STASHDB_API_KEY", "").strip()
             or _core.os.environ.get("STASH_API_KEY", "").strip())
    client = http_client or _core.httpx
    if not scene_id or not token or client is None:
        return None
    query = """
    query FindSceneById($id: ID!) {
      findScene(id: $id) {
        id title details date release_date duration
        studio { name }
        performers { performer { name } }
        tags { name }
        images { url }
        urls { url }
      }
    }
    """
    response = _core.request_with_host_limit(
        client, "post", _core.os.environ.get("STASHDB_API_URL", "https://stashdb.org/graphql"),
        headers={"ApiKey": token, "Accept": "application/json", "Content-Type": "application/json",
                 "User-Agent": "AdultHubSync/1.0 (scene-id-artwork)"},
        json={"query": query, "variables": {"id": scene_id}},
        timeout=_core.DEFAULT_TIMEOUT,
    )
    if callable(getattr(response, "raise_for_status", None)):
        response.raise_for_status()
    payload = response.json() if callable(getattr(response, "json", None)) else _core._decode_scene_payload(response)
    item = (((payload or {}).get("data") or {}).get("findScene")
            if isinstance(payload, dict) else None)
    if not isinstance(item, dict):
        return None
    studio = item.get("studio") if isinstance(item.get("studio"), dict) else {}
    images = item.get("images") if isinstance(item.get("images"), list) else []
    image_url = str((images[0] or {}).get("url") or "") if images and isinstance(images[0], dict) else ""
    return _core.ScrapedScene(
        title=str(item.get("title") or ""),
        plot=str(item.get("details") or ""),
        date=str(item.get("release_date") or item.get("date") or "")[:10],
        duration=int(item.get("duration") or 0),
        poster_url=image_url,
        studio=str(studio.get("name") or ""),
        external_id=str(item.get("id") or scene_id),
        url=f"https://stashdb.org/scenes/{scene_id}",
        confidence=1.0,
        source="stashdb:linked_id",
    )


def search_fansdb_scene_by_exact_title(  # noqa: C901
    title: str,
    *,
    duration: int,
    http_client: Optional[Any] = None,
    duration_tolerance: int = 2,
) -> Optional[_core.ScrapedScene]:
    """Search FansDB by title and require one exact-title/runtime result."""
    query_title = str(title or "").strip()
    query_key = _title_key(query_title)
    try:
        runtime = int(duration or 0)
        tolerance = max(0, int(duration_tolerance))
    except (TypeError, ValueError):
        return None
    token = _core.os.environ.get("FANSDB_API_KEY", "").strip()
    client = http_client or _core.httpx
    if not query_key or runtime <= 0 or not token or client is None:
        return None

    query = """
    query SearchScenesByTitle($term: String!, $limit: Int) {
      searchScenes(term: $term, limit: $limit) {
        count
        scenes {
          id title details date release_date duration
          studio { name }
          performers { performer { name } }
          tags { name }
          images { url }
          urls { url }
        }
      }
    }
    """
    try:
        response = _core.request_with_host_limit(
            client,
            "post",
            _core.os.environ.get("FANSDB_API_URL", "https://fansdb.cc/graphql"),
            headers={"ApiKey": token, "Accept": "application/json", "Content-Type": "application/json",
                     "User-Agent": "AdultHubSync/1.0 (scene-title-exact)"},
            json={"query": query, "variables": {"term": query_title, "limit": 10}},
            timeout=_core.DEFAULT_TIMEOUT,
        )
        if callable(getattr(response, "raise_for_status", None)):
            response.raise_for_status()
        payload = response.json() if callable(getattr(response, "json", None)) else _core._decode_scene_payload(response)
        if not isinstance(payload, dict):
            raise RuntimeError("FansDB returned malformed title search payload: not a JSON object")
        if payload.get("errors"):
            raise RuntimeError(f"FansDB title lookup returned GraphQL errors: {payload['errors']}")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("FansDB title search response missing data object")
        result = data.get("searchScenes")
        if not isinstance(result, dict):
            raise RuntimeError("FansDB title search response missing searchScenes object")
        scenes = result.get("scenes")
        if not isinstance(scenes, list):
            raise RuntimeError("FansDB title search response missing scenes list")
    except Exception:
        _core.record_fansdb_scene_attempt("error")
        raise

    candidates = []
    seen_ids = set()
    for item in scenes:
        if not isinstance(item, dict):
            continue
        candidate_title = str(item.get("title") or "").strip()
        try:
            candidate_duration = int(item.get("duration") or 0)
        except (TypeError, ValueError):
            continue
        if (_title_key(candidate_title) != query_key or candidate_duration <= 0
                or abs(candidate_duration - runtime) > tolerance):
            continue
        external_id = str(item.get("id") or "")
        if external_id and external_id in seen_ids:
            continue
        seen_ids.add(external_id)
        studio = item.get("studio") if isinstance(item.get("studio"), dict) else {}
        appearances = item.get("performers") if isinstance(item.get("performers"), list) else []
        names = [str((appearance.get("performer") or {}).get("name") or "").strip()
                 for appearance in appearances
                 if isinstance(appearance, dict) and isinstance(appearance.get("performer"), dict)]
        tags = item.get("tags") if isinstance(item.get("tags"), list) else []
        images = item.get("images") if isinstance(item.get("images"), list) else []
        candidates.append(_core.ScrapedScene(
            title=candidate_title,
            plot=str(item.get("details") or "").strip(),
            date=str(item.get("release_date") or item.get("date") or "")[:10],
            duration=candidate_duration,
            poster_url=str((images[0] or {}).get("url") or "")
            if images and isinstance(images[0], dict) else "",
            performers=[name for name in names if name],
            tags=[str(tag.get("name") or "").strip()
                  for tag in tags if isinstance(tag, dict) and tag.get("name")],
            studio=str(studio.get("name") or "").strip(),
            external_id=external_id,
            url=f"https://fansdb.cc/scenes/{external_id}" if external_id else "",
            confidence=0.99,
            source="fansdb:title_exact",
        ))
    if len(candidates) == 1:
        _core.record_fansdb_scene_attempt("matched")
        return candidates[0]
    _core.record_fansdb_scene_attempt("no_match")
    return None


def fetch_fansdb_scene_by_id(
    external_id: str,
    *,
    http_client: Optional[Any] = None,
) -> Optional[_core.ScrapedScene]:
    """Fetch a linked FansDB scene directly by its saved ID."""
    scene_id = str(external_id or "").strip()
    token = _core.os.environ.get("FANSDB_API_KEY", "").strip()
    client = http_client or _core.httpx
    if not scene_id or not token or client is None:
        return None
    query = """
    query FindSceneById($id: ID!) {
      findScene(id: $id) {
        id title details date release_date duration
        studio { name }
        performers { performer { name } }
        tags { name }
        images { url }
        urls { url }
      }
    }
    """
    try:
        response = _core.request_with_host_limit(
            client, "post", _core.os.environ.get("FANSDB_API_URL", "https://fansdb.cc/graphql"),
            headers={"ApiKey": token, "Accept": "application/json", "Content-Type": "application/json",
                     "User-Agent": "AdultHubSync/1.0 (scene-id-artwork)"},
            json={"query": query, "variables": {"id": scene_id}},
            timeout=_core.DEFAULT_TIMEOUT,
        )
        if callable(getattr(response, "raise_for_status", None)):
            response.raise_for_status()
        payload = response.json() if callable(getattr(response, "json", None)) else _core._decode_scene_payload(response)
        if not isinstance(payload, dict):
            raise RuntimeError("FansDB returned malformed scene-by-id payload: not a JSON object")
        if payload.get("errors"):
            raise RuntimeError(f"FansDB scene-by-id lookup returned GraphQL errors: {payload['errors']}")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("FansDB scene-by-id response missing data object")
        if "findScene" not in data:
            raise RuntimeError("FansDB scene-by-id response missing findScene object")
        item = data.get("findScene")
        if item is None:
            _core.record_fansdb_scene_attempt("no_match")
            return None
        if not isinstance(item, dict):
            raise RuntimeError("FansDB scene-by-id response findScene is not an object or null")
    except Exception:
        _core.record_fansdb_scene_attempt("error")
        raise
    studio = item.get("studio") if isinstance(item.get("studio"), dict) else {}
    images = item.get("images") if isinstance(item.get("images"), list) else []
    image_url = str((images[0] or {}).get("url") or "") if images and isinstance(images[0], dict) else ""
    _core.record_fansdb_scene_attempt("matched")
    return _core.ScrapedScene(
        title=str(item.get("title") or ""),
        plot=str(item.get("details") or ""),
        date=str(item.get("release_date") or item.get("date") or "")[:10],
        duration=int(item.get("duration") or 0),
        poster_url=image_url,
        studio=str(studio.get("name") or ""),
        external_id=str(item.get("id") or scene_id),
        url=f"https://fansdb.cc/scenes/{scene_id}",
        confidence=1.0,
        source="fansdb:linked_id",
    )


def search_scene_by_exact_title(
    title: str,
    *,
    duration: int,
    http_client: Optional[Any] = None,
    duration_tolerance: int = 2,
) -> Optional[_core.ScrapedScene]:
    """Query StashDB, FansDB, and ThePornDB with strict acceptance.

    Telegram titles often carry a studio prefix or a scene suffix. Try the
    short, catalogue-friendly spelling first, then the cleaned full title.
    """
    failures: list[BaseException] = []
    for variant in lookup_title_variants(title):
        scene, errors = _search_exact_title_variant(
            variant, duration=duration, http_client=http_client,
            duration_tolerance=duration_tolerance,
        )
        if scene is not None:
            return scene
        failures.extend(errors)
    if failures:
        raise SceneLookupUnavailable(
            f"{len(failures)} scene catalogue lookup(s) failed; retry later"
        )
    return None


def _search_exact_title_variant(
    title: str,
    *,
    duration: int,
    http_client: Optional[Any],
    duration_tolerance: int,
) -> tuple[Optional[_core.ScrapedScene], list[BaseException]]:
    """Run the provider chain for one already-normalized title spelling."""
    failures: list[BaseException] = []
    try:
        stashdb_scene = search_stashdb_scene_by_exact_title(
            title, duration=duration, http_client=http_client,
            duration_tolerance=duration_tolerance,
        )
        if stashdb_scene is not None:
            return stashdb_scene, failures
    except Exception as exc:
        _core.logger.warning("StashDB title lookup failed (%s)", type(exc).__name__)
        failures.append(exc)
    try:
        fansdb_scene = search_fansdb_scene_by_exact_title(
            title, duration=duration, http_client=http_client,
            duration_tolerance=duration_tolerance,
        )
        if fansdb_scene is not None:
            return fansdb_scene, failures
    except Exception as exc:
        _core.logger.warning("FansDB title lookup failed (%s)", type(exc).__name__)
        failures.append(exc)
    try:
        return search_tpdb_scene_by_exact_title(
            title, duration=duration, http_client=http_client,
            duration_tolerance=duration_tolerance,
        ), failures
    except Exception as exc:
        _core.logger.warning("ThePornDB title lookup failed (%s)", type(exc).__name__)
        failures.append(exc)
        return None, failures
