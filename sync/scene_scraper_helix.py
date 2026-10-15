"""HelixScraper provider implementation."""
from __future__ import annotations

try:
    from sync.scene_scraper_support import Any, Dict, List, Optional, DEFAULT_TIMEOUT, ScrapedScene, USER_AGENT, httpx, logger, quote, re, request_with_host_limit
except ModuleNotFoundError:
    from scene_scraper_support import Any, Dict, List, Optional, DEFAULT_TIMEOUT, ScrapedScene, USER_AGENT, httpx, logger, quote, re, request_with_host_limit


class HelixScraper:
    """Scrapes Helix Studios (Adult Empire platform)."""

    BASE_URL = "https://www.helixstudios.com"
    _confirmed: bool = False

    @classmethod
    def _ensure_age(cls, client: Any) -> None:
        if not cls._confirmed:
            try:
                request_with_host_limit(
                    client, "get", f"{cls.BASE_URL}/Account/AgeConfirmation?ageConfirmationClicked=true",
                    timeout=5.0,
                )
                cls._confirmed = True
            except Exception:
                pass

    @classmethod
    def search(cls, query: str, limit: int = 4, http_client: Optional[Any] = None) -> List[Dict[str, Any]]:
        client = http_client or httpx
        if not client or not query or not query.strip():
            return []
        cls._ensure_age(client)
        url = f"{cls.BASE_URL}/search?q={quote(query.strip())}"
        try:
            res = request_with_host_limit(
                client, "get", url, headers={"User-Agent": USER_AGENT},
                timeout=DEFAULT_TIMEOUT, follow_redirects=True,
            )
            if res.status_code != 200:
                return []
            matches = re.findall(
                r'<a[^>]+href=[\"\'](/shop/\d+/[^\"\']+-porn-(?:videos|movies)\.html)[\"\'][^>]*>(.*?)</a>',
                res.text,
                re.DOTALL,
            )
            hits = []
            seen_urls = set()
            for href, content in matches:
                if href in seen_urls:
                    continue
                seen_urls.add(href)
                t_match = re.findall(r'title=[\"\']([^\"\']+)[\"\']', content)
                title = t_match[0].strip() if t_match else ""
                alt_match = re.findall(r'alt=[\"\']([^\"\']+)[\"\']', content)
                if not title and alt_match:
                    title = alt_match[0].replace(" Image", "").strip()
                img_match = re.findall(r'data-src=[\"\']([^\"\']+)[\"\']|src=[\"\']([^\"\']+)[\"\']', content)
                thumb = ""
                if img_match:
                    thumb = img_match[0][0] or img_match[0][1]
                hits.append({
                    "url": f"{cls.BASE_URL}{href}",
                    "title": title,
                    "thumb": thumb,
                })
                if len(hits) >= limit:
                    break
            return hits
        except Exception as e:
            logger.warning("Helix search failed for %r: %s", query, e)
            return []

    @classmethod
    def to_scraped_scene(cls, hit: Dict[str, Any], default_studio: str = "Helix Studios", http_client: Optional[Any] = None) -> ScrapedScene:
        url = hit.get("url", "")
        title = hit.get("title", "")
        thumb = hit.get("thumb", "")
        plot = ""
        date = ""
        performers = []
        tags = []
        poster_url = ""

        if url:
            client = http_client or httpx
            if client:
                try:
                    cls._ensure_age(client)
                    res = request_with_host_limit(
                        client, "get", url, headers={"User-Agent": USER_AGENT},
                        timeout=DEFAULT_TIMEOUT, follow_redirects=True,
                    )
                    if res.status_code == 200:
                        text = res.text
                        og_title = re.search(r'<meta\s+property=[\"\']og:title[\"\']\s+content=[\"\']([^\"\']+)[\"\']', text)
                        if og_title:
                            clean_og = re.sub(r'(?i)\s+streaming\s+at\s+Helix\s+Studios.*', '', og_title.group(1)).strip()
                            if clean_og:
                                title = clean_og

                        syn_m = re.search(r'<div[^>]+class=[\"\'][^\"\']*synopsis[^\"\']*[\"\'][^>]*>(.*?)</div>', text, re.DOTALL)
                        if syn_m:
                            plot = re.sub(r'<[^<]+?>', ' ', syn_m.group(1))
                            plot = re.sub(r'\s+', ' ', plot).strip()

                        og_img = re.search(r'<meta\s+property=[\"\']og:image[\"\']\s+content=[\"\']([^\"\']+)[\"\']', text)
                        if og_img:
                            poster_url = re.sub(r'\?.*', '?w=1200', og_img.group(1))

                        actor_links = re.findall(r'<a[^>]+href=[\"\']/[^\"\']*-pornstars\.html[\"\'][^>]*>(.*?)</a>', text)
                        for a in actor_links:
                            aname = re.sub(r'<[^<]+?>', '', a).strip()
                            if aname and aname not in performers:
                                performers.append(aname)

                        cat_links = re.findall(r'<a[^>]+href=[\"\']/[^\"\']*/category/[^\"\']*-streaming-porn-videos\.html[\"\'][^>]*>(.*?)</a>', text)
                        for c in cat_links:
                            cname = re.sub(r'<[^<]+?>', '', c).strip()
                            if cname and cname not in tags:
                                tags.append(cname)
                except Exception as e:
                    logger.debug("Failed fetching Helix scene detail %s: %s", url, e)

        if not poster_url and thumb:
            poster_url = re.sub(r'\?.*', '?w=1200', thumb)

        return ScrapedScene(
            title=title,
            plot=plot,
            date=date,
            duration=0,
            poster_url=poster_url,
            performers=performers,
            tags=sorted(tags),
            studio=default_studio,
            external_id=url,
            url=url,
        )
