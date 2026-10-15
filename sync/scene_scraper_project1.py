"""Project1Scraper provider implementation."""
from __future__ import annotations

try:
    from sync.scene_scraper_support import Any, Dict, List, Optional, Tuple, DEFAULT_TIMEOUT, ScrapedScene, USER_AGENT, httpx, logger, quote, request_with_host_limit, time
except ModuleNotFoundError:
    from scene_scraper_support import Any, Dict, List, Optional, Tuple, DEFAULT_TIMEOUT, ScrapedScene, USER_AGENT, httpx, logger, quote, request_with_host_limit, time


class Project1Scraper:
    """Scrapes Aylo network (Men.com, Sean Cody) using project1service API."""

    API_BASE = "https://site-api.project1service.com/v2/releases"
    DOMAINS = {
        "seancody": {
            "name": "Sean Cody",
            "url": "https://www.seancody.com",
            "origin": "https://www.seancody.com",
        },
        "men": {
            "name": "Men.com",
            "url": "https://www.men.com",
            "origin": "https://www.men.com",
        },
    }

    _token_cache: Dict[str, Tuple[str, float]] = {}  # domain -> (instance_token, expire_ts)

    @classmethod
    def get_instance_token(cls, domain_key: str, http_client: Optional[Any] = None) -> Optional[str]:
        """Retrieve instance_token cookie from site homepage."""
        now = time.time()
        cached = cls._token_cache.get(domain_key)
        if cached and cached[1] > now:
            return cached[0]

        cfg = cls.DOMAINS.get(domain_key, cls.DOMAINS["seancody"])
        site_url = cfg["url"]

        try:
            client = http_client or httpx
            if client:
                res = request_with_host_limit(
                    client, "get",
                    site_url,
                    headers={"User-Agent": USER_AGENT},
                    timeout=8.0,
                    follow_redirects=True,
                )
                token = res.cookies.get("instance_token")
                if token:
                    cls._token_cache[domain_key] = (token, now + 86400)
                    return token
        except Exception as e:
            logger.debug("Failed to get instance_token for %s: %s", domain_key, e)
        return None

    @classmethod
    def search(
        cls,
        domain_key: str,
        query: str,
        limit: int = 5,
        http_client: Optional[Any] = None,
    ) -> List[Dict[str, Any]]:
        """Search project1service releases endpoint."""
        if not query or not query.strip():
            return []

        token = cls.get_instance_token(domain_key, http_client=http_client)
        if not token:
            logger.debug("No instance_token available for %s", domain_key)
            return []

        cfg = cls.DOMAINS.get(domain_key, cls.DOMAINS["seancody"])
        headers = {
            "Instance": token,
            "User-Agent": USER_AGENT,
            "Origin": cfg["origin"],
            "Referer": f"{cfg['origin']}/",
        }
        url = f"{cls.API_BASE}?search={quote(query.strip())}&type=scene&limit={limit}"

        try:
            client = http_client or httpx
            if not client:
                return []
            res = request_with_host_limit(
                client, "get", url, headers=headers, timeout=DEFAULT_TIMEOUT,
            )
            if res.status_code == 200:
                data = res.json()
                return data.get("result", [])
            logger.debug("Project1 search for %s returned HTTP %s", domain_key, res.status_code)
        except Exception as e:
            logger.warning("Project1 search failed for %s (%r): %s", domain_key, query, e)
        return []

    @classmethod
    def to_scraped_scene(cls, item: Dict[str, Any], default_studio: str = "") -> ScrapedScene:
        """Parse raw release item into ScrapedScene."""
        title = (item.get("title") or "").strip()
        plot = (item.get("description") or "").strip()
        date_released = (item.get("dateReleased") or "").strip()[:10]
        duration = int(item.get("duration") or 0)
        external_id = str(item.get("id") or "")

        # Studio
        studio = default_studio
        groups = item.get("groups", [])
        if groups and isinstance(groups, list) and groups[0].get("displayName"):
            studio = groups[0]["displayName"]
        elif item.get("brand"):
            studio = item["brand"].capitalize()

        # Performers
        performers = []
        for actor in item.get("actors", []):
            if isinstance(actor, dict) and actor.get("name"):
                performers.append(actor["name"].strip())

        # Tags
        tags = []
        for t in item.get("tags", []):
            if isinstance(t, dict) and t.get("name"):
                tags.append(t["name"].strip())

        # Poster (extract best resolution available: xx -> xl -> lg -> md)
        images = item.get("images", {})
        poster_url = ""
        for key in ("poster", "card_main_rect"):
            section = images.get(key)
            if isinstance(section, dict):
                # often nested as "0": {...}
                first = section.get("0") or section
                if isinstance(first, dict):
                    for size in ("xx", "xl", "lg", "md"):
                        cand = first.get(size, {})
                        if isinstance(cand, dict) and cand.get("url"):
                            poster_url = cand["url"]
                            break
            if poster_url:
                break

        return ScrapedScene(
            title=title,
            plot=plot,
            date=date_released,
            duration=duration,
            poster_url=poster_url,
            performers=performers,
            tags=sorted(list(set(tags))),
            studio=studio or default_studio,
            external_id=external_id,
            url="",
        )
