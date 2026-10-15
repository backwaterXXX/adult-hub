"""AlgoliaScraper provider implementation."""
from __future__ import annotations

try:
    from sync.scene_scraper_support import Any, Dict, List, Optional, Tuple, DEFAULT_TIMEOUT, GAMMA_IMAGE_CDN, ScrapedScene, USER_AGENT, httpx, json, logger, re, request_with_host_limit, time
except ModuleNotFoundError:
    from scene_scraper_support import Any, Dict, List, Optional, Tuple, DEFAULT_TIMEOUT, GAMMA_IMAGE_CDN, ScrapedScene, USER_AGENT, httpx, json, logger, re, request_with_host_limit, time


class AlgoliaScraper:
    """Scrapes Gamma Entertainment studios using Algolia search API."""

    # Default known working App ID and API Keys
    DEFAULT_APP_ID = "TSMKFA364Q"
    STUDIO_CONFIGS = {
        "falconstudios": {
            "name": "Falcon Studios",
            "homepage": "https://www.falconstudios.com",
            "fallback_key": "MzI5NDY5NzgxMjY4NGI1OTMyNDY3YWMxZGIwNjY3ODIxZGFkMzQ0Y2FmMWZiNTAwZjUyZTIxMGI4ZjZkOTdmNHZhbGlkVW50aWw9MTc5MTA0NjQyMyZyZXN0cmljdEluZGljZXM9YWxsJTJBJmZpbHRlcnM9c2VnbWVudCUzQWZhbGNvbnN0dWRpb3MrQU5EK05PVCtibG9ja2VkQ291bnRyaWVzJTNBUEw=",
            "index": "all_scenes",
        },
        "nextdoorstudios": {
            "name": "NextDoor Studios",
            "homepage": "https://www.nextdoorstudios.com",
            "fallback_key": "ODA3NDQ5OWI3YTRlYjg2OThmOTBkN2ZmNmE0MDE1NWU4ODM3ZTU5NTk0MjM2ZDRiYjY4NWFjZjU2NDY2MjhhZXZhbGlkVW50aWw9MTc5MTA0NjQzNSZyZXN0cmljdEluZGljZXM9YWxsJTJBJmZpbHRlcnM9c2VnbWVudCUzQWFzZ21heCtBTkQrJTI4YWN0b3JfaWQrJTNFKzArT1IrTk9UK3NpdGVfaWQrJTNEKzIlMjkrQU5EK05PVCtibG9ja2VkQ291bnRyaWVzJTNBUEw=",
            "index": "all_scenes",
        },
    }

    _token_cache: Dict[str, Tuple[str, str, float]] = {}  # site -> (app_id, api_key, expire_ts)

    @classmethod
    def get_credentials(cls, site_key: str, http_client: Optional[Any] = None) -> Tuple[str, str]:
        """Get (app_id, api_key) with automatic extraction and fallback."""
        now = time.time()
        cached = cls._token_cache.get(site_key)
        if cached and cached[2] > now:
            return cached[0], cached[1]

        cfg = cls.STUDIO_CONFIGS.get(site_key, cls.STUDIO_CONFIGS["falconstudios"])
        homepage = cfg["homepage"]
        fallback_key = cfg["fallback_key"]

        # Attempt to dynamically parse window.env from the homepage
        try:
            client = http_client or httpx
            if client:
                res = request_with_host_limit(
                    client, "get", homepage, headers={"User-Agent": USER_AGENT}, timeout=5.0,
                )
                if res.status_code == 200:
                    m = re.search(r"window\.env\s*=\s*(\{.+?\});", res.text)
                    if m:
                        data = json.loads(m.group(1))
                        algolia_info = data.get("api", {}).get("algolia", {})
                        app_id = algolia_info.get("applicationID")
                        api_key = algolia_info.get("apiKey")
                        if app_id and api_key:
                            cls._token_cache[site_key] = (app_id, api_key, now + 86400)
                            return app_id, api_key
        except Exception as e:
            logger.debug("Failed to extract dynamic Algolia token for %s: %s", site_key, e)

        # Fallback
        cls._token_cache[site_key] = (cls.DEFAULT_APP_ID, fallback_key, now + 3600)
        return cls.DEFAULT_APP_ID, fallback_key

    @classmethod
    def search(
        cls,
        site_key: str,
        query: str,
        limit: int = 5,
        http_client: Optional[Any] = None,
    ) -> List[Dict[str, Any]]:
        """Search Algolia all_scenes index."""
        if not query or not query.strip():
            return []

        app_id, api_key = cls.get_credentials(site_key, http_client=http_client)
        cfg = cls.STUDIO_CONFIGS.get(site_key, cls.STUDIO_CONFIGS["falconstudios"])
        index_name = cfg["index"]
        homepage = cfg["homepage"]

        url = f"https://{app_id}-dsn.algolia.net/1/indexes/{index_name}/query"
        headers = {
            "X-Algolia-Application-Id": app_id,
            "X-Algolia-API-Key": api_key,
            "Content-Type": "application/json",
            "Origin": homepage,
            "Referer": f"{homepage}/",
            "User-Agent": USER_AGENT,
        }
        payload = {
            "query": query.strip(),
            "hitsPerPage": limit,
        }

        try:
            client = http_client or httpx
            if not client:
                return []
            res = request_with_host_limit(
                client, "post", url, headers=headers, json=payload, timeout=DEFAULT_TIMEOUT,
            )
            if res.status_code == 200:
                data = res.json()
                return data.get("hits", [])
            logger.debug("Algolia search for %s returned HTTP %s", site_key, res.status_code)
        except Exception as e:
            logger.warning("Algolia search failed for %s (%r): %s", site_key, query, e)
        return []

    @classmethod
    def to_scraped_scene(cls, hit: Dict[str, Any], default_studio: str = "") -> ScrapedScene:
        """Parse raw Algolia hit into ScrapedScene."""
        title = (hit.get("title") or "").strip()
        plot = (hit.get("description") or hit.get("movie_desc") or "").strip()
        date = (hit.get("release_date") or "").strip()[:10]
        duration = int(hit.get("length") or 0)
        studio = (hit.get("studio_name") or default_studio).strip()
        external_id = str(hit.get("clip_id") or hit.get("objectID") or "")

        # Performers
        performers = []
        for actor in hit.get("actors", []):
            if isinstance(actor, dict) and actor.get("name"):
                performers.append(actor["name"].strip())
            elif isinstance(actor, str):
                performers.append(actor.strip())

        # Tags
        tags = []
        for cat in hit.get("categories", []):
            if isinstance(cat, dict) and cat.get("name"):
                tags.append(cat["name"].strip())
            elif isinstance(cat, str):
                tags.append(cat.strip())
        for ctag in hit.get("content_tags", []):
            if isinstance(ctag, str) and ctag.strip():
                tags.append(ctag.strip())

        # Poster image (prefer highest resolution: resized 1280x853 HD -> 720x480 -> 638x360)
        pics = hit.get("pictures", {})
        poster_path = ""
        if isinstance(pics, dict):
            if pics.get("resized"):
                poster_path = pics["resized"]
            else:
                nsfw = pics.get("nsfw", {})
                if isinstance(nsfw, dict) and nsfw.get("top", {}).get("resized"):
                    poster_path = nsfw["top"]["resized"]
                elif isinstance(nsfw, dict) and nsfw.get("top", {}).get("720x480"):
                    poster_path = nsfw["top"]["720x480"]
                elif pics.get("720x480"):
                    poster_path = pics["720x480"]
                elif pics.get("638x360"):
                    poster_path = pics["638x360"]

        poster_url = f"{GAMMA_IMAGE_CDN}/movies{poster_path}" if poster_path else ""

        url_title = hit.get("url_title") or hit.get("url_movie_title") or ""
        scene_url = f"https://www.{hit.get('sitename', 'falconstudios')}.com/en/video/{url_title}/{external_id}" if url_title else ""

        return ScrapedScene(
            title=title,
            plot=plot,
            date=date,
            duration=duration,
            poster_url=poster_url,
            performers=performers,
            tags=sorted(list(set(tags))),
            studio=studio,
            external_id=external_id,
            url=scene_url,
        )
