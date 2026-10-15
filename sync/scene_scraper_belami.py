"""BelAmiScraper provider implementation."""
from __future__ import annotations

try:
    from sync.scene_scraper_support import Any, Dict, List, Optional, DEFAULT_TIMEOUT, ScrapedScene, USER_AGENT, httpx, json, logger, quote, re, request_with_host_limit
except ModuleNotFoundError:
    from scene_scraper_support import Any, Dict, List, Optional, DEFAULT_TIMEOUT, ScrapedScene, USER_AGENT, httpx, json, logger, quote, re, request_with_host_limit


class BelAmiScraper:
    """Scrapes BelAmiOnline (newtour.belamionline.com)."""

    BASE_URL = "https://newtour.belamionline.com"

    @classmethod
    def search(cls, query: str, limit: int = 4, http_client: Optional[Any] = None) -> List[Dict[str, Any]]:
        client = http_client or httpx
        if not client or not query or not query.strip():
            return []
        url = f"{cls.BASE_URL}/search.aspx?q={quote(query.strip())}"
        try:
            res = request_with_host_limit(
                client, "get", url, headers={"User-Agent": USER_AGENT},
                timeout=DEFAULT_TIMEOUT, follow_redirects=True,
            )
            if res.status_code != 200:
                return []
            video_ids = re.findall(r'playvideo\.aspx\?VideoID=(\d+)', res.text)
            hits = []
            seen = set()
            for vid in video_ids:
                if vid in seen:
                    continue
                seen.add(vid)
                hits.append({
                    "video_id": vid,
                    "url": f"{cls.BASE_URL}/playvideo.aspx?VideoID={vid}",
                })
                if len(hits) >= limit:
                    break
            return hits
        except Exception as e:
            logger.warning("BelAmi search failed for %r: %s", query, e)
            return []

    @classmethod
    def to_scraped_scene(cls, hit: Dict[str, Any], default_studio: str = "BelAmi", http_client: Optional[Any] = None) -> ScrapedScene:
        url = hit.get("url", "")
        vid_id = hit.get("video_id", "")
        title = ""
        plot = ""
        date = ""
        duration = 0
        poster_url = ""
        performers = []

        if url:
            client = http_client or httpx
            if client:
                try:
                    res = request_with_host_limit(
                        client, "get", url, headers={"User-Agent": USER_AGENT},
                        timeout=DEFAULT_TIMEOUT, follow_redirects=True,
                    )
                    if res.status_code == 200:
                        text = res.text
                        for jm in re.finditer(r'<script\s+type=[\"\']application/ld\+json[\"\']>(.*?)</script>', text, re.DOTALL):
                            try:
                                data = json.loads(jm.group(1))
                                if data.get("@type") == "VideoObject":
                                    raw_name = data.get("name", "")
                                    title = re.sub(r'(?i)\s+Trailer\s+@\s+BelAmi.*', '', raw_name).strip()
                                    plot = (data.get("description") or "").strip()
                                    poster_url = (data.get("thumbnailUrl") or "").strip()
                                    date = (data.get("uploadDate") or "").strip()[:10]
                                    dur_str = data.get("duration", "")
                                    dm = re.search(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', dur_str)
                                    if dm:
                                        h = int(dm.group(1) or 0)
                                        m = int(dm.group(2) or 0)
                                        s = int(dm.group(3) or 0)
                                        duration = h * 3600 + m * 60 + s
                                    break
                            except Exception:
                                pass

                        models = re.findall(r'href=[\"\'][^\"\']*model_detail\.aspx\?ModelID=\d+[\"\'][^>]*>(.*?)</a>', text, re.DOTALL)
                        for m in models:
                            mname = re.sub(r'<[^<]+?>', '', m).strip()
                            if mname and mname not in performers and mname.lower() not in ("cast", "join"):
                                performers.append(mname)

                        if not performers:
                            names = re.findall(r'<div class=[\"\']name[\"\']>([^<]+)</div>', text)
                            for n in names:
                                n_clean = n.strip()
                                if n_clean and n_clean not in performers and n_clean.lower() not in ("cast", "join"):
                                    performers.append(n_clean)

                        if not performers:
                            m_imgs = re.findall(r'<img[^>]+src=[\"\'][^\"\']*Data/Models/[^\"\']*[\"\'][^>]+alt=[\"\']([^\"\']+)[\"\']', text)
                            for mname in m_imgs:
                                mname = mname.strip()
                                if mname and mname not in performers:
                                    performers.append(mname)
                except Exception as e:
                    logger.debug("Failed fetching BelAmi scene detail %s: %s", url, e)

        return ScrapedScene(
            title=title or f"BelAmi Scene {vid_id}",
            plot=plot,
            date=date,
            duration=duration,
            poster_url=poster_url,
            performers=performers,
            tags=["BelAmi", "European"],
            studio=default_studio,
            external_id=str(vid_id),
            url=url,
        )
