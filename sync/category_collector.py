from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import quote


_CATEGORY_SKIP: Set[Tuple[str, str]] = set()


@dataclass
class CategoryCollectorServices:
    fetch_categories: Callable[[str], List[Dict[str, str]]]
    fetch_category_videos: Callable[[str, str, int], List[Dict[str, Any]]]
    get_provider_info: Callable[[str], Dict[str, str]]
    format_category_name: Callable[[str], str]
    map_category: Callable[[str], Optional[str]]
    merge_video_fields: Callable[[Dict[str, Any], Dict[str, Any]], None]
    category_skips: Set[Tuple[str, str]]
    logger: logging.Logger
    sleep_fn: Callable[[float], None]
    category_fetch_delay_seconds: float = 1.0


def register_category_skip(provider: str, category_id: str, skips: Optional[Set[Tuple[str, str]]] = None) -> None:
    """Register a (provider, category) pair to skip in category walks."""
    p = (provider or "").strip().lower()
    c = (category_id or "").strip()
    target = _CATEGORY_SKIP if skips is None else skips
    if p and c:
        target.add((p, c))


def fetch_provider_categories(
    provider: str,
    resolver_api_url: str,
    fetch_json_fn: Callable[..., Any],
) -> List[Dict[str, str]]:
    url = f"{resolver_api_url.rstrip('/')}/admin/api/categories?provider={provider}"
    return fetch_json_fn(url, timeout=20.0, desc=f"categories/{provider}")


def fetch_provider_category_videos(
    provider: str,
    category_id: str,
    page: int,
    resolver_api_url: str,
    fetch_json_fn: Callable[..., Any],
) -> List[Dict[str, Any]]:
    category_path = quote(category_id, safe="")
    url = f"{resolver_api_url.rstrip('/')}/admin/api/category/{category_path}?provider={provider}&page={page}"
    return fetch_json_fn(url, timeout=20.0, desc=f"category/{provider}/{category_id}/p.{page}")


def collect_provider_categories(
    collected: Dict[str, Dict[str, Any]],
    provider: str,
    max_pages: int,
    services: CategoryCollectorServices,
) -> bool:
    """Merge category membership from a provider into the deduplicated catalog."""
    categories = services.fetch_categories(provider)
    if not categories:
        services.logger.warning("No categories returned for %s", provider)
        return True

    provider_name = services.get_provider_info(provider)["name"]
    services.logger.info("Syncing %s categories from %s", len(categories), provider_name)
    seen_categories: Set[str] = set()
    complete = True

    for category in categories:
        category_id = category.get("id", "").strip()
        if not category_id or category_id in seen_categories or (provider, category_id) in services.category_skips:
            continue
        seen_categories.add(category_id)
        category_label = (category.get("title") or services.format_category_name(category_id)).strip()
        if not category_label:
            continue
        if not _collect_category_pages(collected, provider, provider_name, category_id,
                                       category_label, max_pages, services):
            complete = False

    return complete


def _collect_category_pages(collected, provider: str, provider_name: str, category_id: str,
                            category_label: str, max_pages: int, services: CategoryCollectorServices) -> bool:
    """Merge up to ``max_pages`` pages of one category; return False when any page failed."""
    complete = True
    for page in range(1, max_pages + 1):
        try:
            videos = services.fetch_category_videos(provider, category_id, page)
            services.logger.info("%s category '%s' p.%s: %s videos", provider_name, category_id, page, len(videos))
            for video in videos:
                _merge_category_video(collected, provider, provider_name, category_label, video, services)
        except Exception as e:
            complete = False
            services.logger.error("Failed to fetch %s category '%s' p.%s: %s", provider_name, category_id, page, e)
        delay = max(0.0, float(services.category_fetch_delay_seconds))
        if delay:
            services.sleep_fn(delay)
    return complete


def _merge_category_video(collected, provider: str, provider_name: str, category_label: str,
                          video: dict, services: CategoryCollectorServices) -> None:
    if video.get("provider", provider) != provider:
        return
    video_id = video.get("provider_video_id") or video.get("id", "").split(":", 1)[-1]
    if not video_id:
        return
    composite_id = f"{provider}:{video_id}"
    if composite_id not in collected:
        collected[composite_id] = {
            "video": video,
            "genres": set(),
            "tags": set(),
        }
    else:
        services.merge_video_fields(collected[composite_id]["video"], video)
    item = collected[composite_id]
    item["tags"].add(provider_name)
    mapped_cat = services.map_category(category_label)
    if mapped_cat:
        item["genres"].add(mapped_cat)
    for c in video.get("categories", []):
        mc = services.map_category(c)
        if mc:
            item["genres"].add(mc)
