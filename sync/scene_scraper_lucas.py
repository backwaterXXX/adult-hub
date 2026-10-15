"""LucasScraper provider implementation."""
from __future__ import annotations

try:
    from sync.scene_scraper_support import Any, Dict, List, Optional, DEFAULT_TIMEOUT, ScrapedScene, USER_AGENT, httpx, logger, quote, re, request_with_host_limit
except ModuleNotFoundError:
    from scene_scraper_support import Any, Dict, List, Optional, DEFAULT_TIMEOUT, ScrapedScene, USER_AGENT, httpx, logger, quote, re, request_with_host_limit


class LucasScraper:
    """Scrapes Lucas Entertainment."""

    BASE_URL = "https://www.lucasentertainment.com"

    @classmethod
    def search(cls, query: str, limit: int = 6, http_client: Optional[Any] = None) -> List[Dict[str, Any]]:
        client = http_client or httpx
        if not client or not query or not query.strip():
            return []
        url = f"{cls.BASE_URL}/?s={quote(query.strip())}"
        try:
            res = request_with_host_limit(
                client, "get", url, headers={"User-Agent": USER_AGENT},
                timeout=DEFAULT_TIMEOUT, follow_redirects=True,
            )
            if res.status_code != 200:
                return []
            cards = re.findall(
                r'<a[^>]+href=[\"\'](https://www\.lucasentertainment\.com/scenes/play/[^\"\']+)[\"\'][^>]+title=[\"\']([^\"\']+)[\"\'][^>]*>(.*?)</a>',
                res.text,
                re.DOTALL,
            )
            hits = []
            seen = set()
            for href, title, body in cards:
                if href in seen:
                    continue
                seen.add(href)
                img = re.search(r'data-original=[\"\']([^\"\']+)[\"\']', body)
                date_m = re.search(r'<span>(\d{2})\.(\d{2})\.(\d{2})</span>', body)
                desc = re.search(r'<p[^>]*class=[\"\'][^\"\']*model-info-desc[^\"\']*[\"\'][^>]*>(.*?)</p>', body, re.DOTALL)

                date_str = ""
                if date_m:
                    m, d, y = date_m.group(1), date_m.group(2), date_m.group(3)
                    year = f"20{y}" if int(y) < 70 else f"19{y}"
                    date_str = f"{year}-{m}-{d}"

                hits.append({
                    "url": href,
                    "title": title.strip(),
                    "poster_url": img.group(1).strip() if img else "",
                    "date": date_str,
                    "plot": re.sub(r'<[^<]+?>', '', desc.group(1)).strip() if desc else "",
                })
                if len(hits) >= limit:
                    break

            if len(hits) < limit:
                words = [w for w in query.strip().split() if len(w) >= 3]
                candidates_to_check = []
                if 2 <= len(words) <= 3:
                    candidates_to_check.append("-".join(words).lower())
                elif len(words) >= 4:
                    candidates_to_check.append(f"{words[0]}-{words[1]}".lower())

                for model_slug in candidates_to_check:
                    try:
                        m_url = f"{cls.BASE_URL}/models/view/{model_slug}"
                        m_res = request_with_host_limit(
                            client, "get", m_url, headers={"User-Agent": USER_AGENT},
                            timeout=DEFAULT_TIMEOUT, follow_redirects=True,
                        )
                        if m_res.status_code == 200:
                            m_cards = re.findall(
                                r'<a[^>]+href=[\"\'](https://www\.lucasentertainment\.com/scenes/play/[^\"\']+)[\"\'][^>]+title=[\"\']([^\"\']+)[\"\'][^>]*>(.*?)</a>',
                                m_res.text,
                                re.DOTALL,
                            )
                            for href, title, body in m_cards:
                                if href in seen:
                                    continue
                                seen.add(href)
                                img = re.search(r'data-original=[\"\']([^\"\']+)[\"\']', body)
                                date_m = re.search(r'<span>(\d{2})\.(\d{2})\.(\d{2})</span>', body)
                                desc = re.search(r'<p[^>]*class=[\"\'][^\"\']*model-info-desc[^\"\']*[\"\'][^>]*>(.*?)</p>', body, re.DOTALL)

                                date_str = ""
                                if date_m:
                                    m, d, y = date_m.group(1), date_m.group(2), date_m.group(3)
                                    year = f"20{y}" if int(y) < 70 else f"19{y}"
                                    date_str = f"{year}-{m}-{d}"

                                hits.append({
                                    "url": href,
                                    "title": title.strip(),
                                    "poster_url": img.group(1).strip() if img else "",
                                    "date": date_str,
                                    "plot": re.sub(r'<[^<]+?>', '', desc.group(1)).strip() if desc else "",
                                })
                                if len(hits) >= limit:
                                    break
                    except Exception:
                        pass
                    if len(hits) >= limit:
                        break

            return hits
        except Exception as e:
            logger.warning("Lucas search failed for %r: %s", query, e)
            return []

    @classmethod
    def to_scraped_scene(cls, hit: Dict[str, Any], default_studio: str = "Lucas Entertainment", http_client: Optional[Any] = None) -> ScrapedScene:
        url = hit.get("url", "")
        title = hit.get("title", "")
        plot = hit.get("plot", "")
        date = hit.get("date", "")
        poster_url = hit.get("poster_url", "")
        performers = []

        if title:
            act_match = re.split(r'\s+(?:tops|fucks|bottoms(?:\s+for)?|services|seduces|slams|and|&)\s+', title.split("|")[0], flags=re.IGNORECASE)
            if len(act_match) >= 2:
                for a in act_match:
                    a_clean = re.sub(r'^(?:for|by|with)\s+', '', a.strip(), flags=re.IGNORECASE).strip()
                    if a_clean and len(a_clean.split()) <= 3 and a_clean.lower() not in ("raw", "uncut", "bareback"):
                        performers.append(a_clean)
            elif "," in title.split("|")[0]:
                for a in title.split("|")[0].split(","):
                    a_clean = re.sub(r'^(?:for|by|with)\s+', '', a.strip(), flags=re.IGNORECASE).strip()
                    if a_clean and len(a_clean.split()) <= 3:
                        performers.append(a_clean)

        return ScrapedScene(
            title=title or (url.rstrip("/").split("/")[-1].replace("-", " ").title() if url else ""),
            plot=plot,
            date=date,
            duration=0,
            poster_url=poster_url,
            performers=performers,
            tags=["Lucas Entertainment", "Bareback"],
            studio=default_studio,
            external_id=url,
            url=url,
        )
