#!/usr/bin/env python3
"""Deep PBC (Porn Base Central) enrichment for Jellyfin performers.

Pulls biographies, birth details, aliases, and high-resolution portraits from PBC (pbc.xxx)
and publishes them directly to Jellyfin person pages without using StashDB.
"""

import base64
import json
import os
import re
from common import db
from common.logging_setup import configure_logging
from common.http_rate_limit import request_with_host_limit
from common.performer_profiles import get_override_fields, lock_profile_enrichment
import time
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import quote

import httpx

try:
    from common.performers import is_valid_performer_name, is_safe_alias
except ImportError:
    from performers import is_valid_performer_name, is_safe_alias

try:
    from .pbc_parsing import parse_person, profile_matches_name
except ImportError:
    from pbc_parsing import parse_person, profile_matches_name

logger = configure_logging("deep_enrich_pbc")

PBC_API_URL = (
    "https://pbc.xxx/w/api.php"
    "?action=query&prop=revisions|pageimages&rvprop=content&piprop=original"
    "&format=json&formatversion=2&redirects=1&titles="
)
USER_AGENT = "AdultHubSync/1.0 (deep-pbc-enrichment)"
TIMEOUT = 15.0

LEGACY_MARKERS = ("StashDB:", "Informacje ze StashDB:", "Dane o wykonawcy:")
FORMAT_VERSION = 5


def page_title(name: str) -> str:
    """Map performer name to MediaWiki page title."""
    return re.sub(r"\s+", "_", name.strip())


def parse_pbc_wikitext(wikitext: str) -> Tuple[str, str, List[str]]:
    """Parse wikitext to extract (bio, birth, aliases)."""
    if not wikitext or not isinstance(wikitext, str):
        return "", "", []

    # 1. Aliases
    aliases = []
    # From infobox/biobox
    for m in re.finditer(r"(?im)^\s*\|\s*aliases?\s*[:=|]\s*(.+?)\s*$", wikitext):
        val = m.group(1).lstrip("|").strip()
        for a in re.split(r"[,;]", val):
            cand = a.strip().strip("\"'")
            if len(cand) >= 2 and cand.lower() not in ("none", "n/a", "unknown", "?"):
                if cand not in aliases:
                    aliases.append(cand)

    # 2. Birth details
    birth = ""
    for field in ["born", "birth_date", "birth_place", "birthplace", "origin"]:
        m = re.search(r"(?im)^\s*\|\s*" + field + r"\s*[:=|]\s*(.+?)\s*$", wikitext)
        if m:
            val = m.group(1).lstrip("|").strip()
            val = re.sub(r"<[^>]+>", " ", val)
            val = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]+)\]\]", r"\1", val)
            val = re.sub(r"\s+", " ", val).strip()
            if val and val.lower() not in ("none", "n/a", "unknown", "?", "tbd"):
                birth = val
                break

    # 3. Bio / Prose or Career summary
    bio = ""
    # Try finding prose paragraphs first
    blocks = [b.strip() for b in wikitext.split("\n\n") if b.strip()]
    candidates = []
    for b in blocks:
        if (
            "File:" not in b
            and "<gallery" not in b
            and "Category:" not in b
            and "{" not in b
            and "=" not in b
            and len(b) >= 40
        ):
            candidates.append(b)

    if candidates:
        # Score candidates
        best = max(candidates, key=len)
        # Clean prose
        clean = re.sub(r"<ref\b[^>]*>.*?</ref\s*>", " ", best, flags=re.IGNORECASE | re.DOTALL)
        clean = re.sub(r"<ref\b[^>]*/\s*>", " ", clean, flags=re.IGNORECASE)
        clean = re.sub(r"<[^>]+>", " ", clean)
        clean = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]+)\]\]", r"\1", clean)
        clean = re.sub(r"'''''|'''|''", "", clean)
        clean = re.sub(r"\s+", " ", clean).strip()
        if len(clean) >= 40:
            bio = clean

    # If no prose bio found, construct career summary from studios
    if not bio:
        studios = []
        for m in re.finditer(r"\{\{GayPornographicCareerList\|site=\s*([^|\n}]+)", wikitext):
            s = m.group(1).strip().capitalize()
            if s and s not in studios:
                studios.append(s)
        if studios:
            bio = f"Adult film performer known for appearances with {', '.join(studios[:10])}."

    return bio, birth, aliases


def format_pbc_overview(bio: str, birth: str = "") -> str:
    parts = []
    if bio and bio.strip():
        parts.append(bio.strip())
    if birth and birth.strip() and birth.strip().lower() not in (bio or "").lower():
        parts.append(f"Born: {birth.strip()}")
    return "\n\n".join(parts)


def run_deep_enrichment(
    db_url: str | None = None,
    jf_url: Optional[str] = None,
    jf_key: Optional[str] = None,
    batch_size: int = 50,
):
    jf_url = (jf_url or os.getenv("JELLYFIN_URL", "")).rstrip("/")
    jf_key = jf_key or os.getenv("JELLYFIN_API_KEY", "")
    if not jf_url or not jf_key:
        raise ValueError("JELLYFIN_URL and JELLYFIN_API_KEY must be configured")
    logger.info("Starting deep PBC performer enrichment...")
    conn = db.connect(db_url)

    # 1. Collect all performer names from videos and Jellyfin
    names_set: Set[str] = set()

    # From DB
    c = conn.cursor()
    c.execute("SELECT performers FROM videos WHERE performers <> '[]'::jsonb")
    for (raw,) in c.fetchall():
        try:
            items = json.loads(raw)
            if isinstance(items, list):
                for i in items:
                    name = str(i).strip()
                    if is_valid_performer_name(name):
                        names_set.add(name)
        except Exception:
            pass

    # From Jellyfin
    jf_headers = {"Authorization": f'MediaBrowser Token="{jf_key}"'}
    client = httpx.Client(timeout=15.0)

    try:
        resp = client.get(f"{jf_url.rstrip('/')}/Persons?limit=4000", headers=jf_headers)
        if resp.status_code == 200:
            for item in resp.json().get("Items", []):
                pname = item.get("Name", "").strip()
                if is_valid_performer_name(pname):
                    names_set.add(pname)
            logger.info("Fetched %d persons from Jellyfin.", len(resp.json().get("Items", [])))
    except Exception as e:
        logger.warning("Failed to fetch persons from Jellyfin: %s", e)

    all_names = sorted(list(names_set), key=lambda s: s.lower())
    logger.info("Total unique performer candidates to check on PBC: %d", len(all_names))

    # 2. Query PBC in batches
    pbc_found = 0
    pbc_photos_found = 0
    pbc_bios_found = 0

    enriched_performers: Dict[str, dict] = {}

    for i in range(0, len(all_names), batch_size):
        batch = all_names[i : i + batch_size]
        title_map = {}
        for n in batch:
            t = page_title(n)
            title_map[t.lower()] = n
            title_map[n.lower()] = n

        titles_param = "|".join(quote(page_title(n), safe="") for n in batch)
        url = PBC_API_URL + titles_param

        try:
            r = request_with_host_limit(
                client, "get", url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT,
            )
            if r.status_code != 200:
                logger.warning("PBC API batch %d-%d returned %d", i, i + len(batch), r.status_code)
                time.sleep(0.5)
                continue

            data = r.json()
            query = data.get("query", {})
            pages = query.get("pages", [])

            # Handle MediaWiki normalizations / redirects
            norm_map = {}
            for norm in query.get("normalized", []):
                norm_map[norm.get("to", "").lower()] = norm.get("from", "").lower()
            for red in query.get("redirects", []):
                norm_map[red.get("to", "").lower()] = red.get("from", "").lower()

            for page in pages:
                if page.get("missing"):
                    continue

                page_title_str = page.get("title", "")
                lookup_key = page_title_str.lower()
                # Walk back redirects / normalizations safely (prevent cycles)
                seen_keys = set()
                while lookup_key in norm_map and lookup_key not in seen_keys:
                    seen_keys.add(lookup_key)
                    lookup_key = norm_map[lookup_key]

                orig_name = title_map.get(lookup_key) or title_map.get(page_title_str.lower()) or page_title_str
                if not is_valid_performer_name(orig_name):
                    continue

                rev = (page.get("revisions") or [{}])[0]
                wikitext = rev.get("content") or ""
                photo_url = (page.get("original") or {}).get("source") or ""

                parsed = parse_person(wikitext)
                if not profile_matches_name(orig_name, parsed):
                    continue
                bio = str(parsed.get("bio") or "")
                birth = str(parsed.get("birth") or "")
                aliases = parsed.get("aliases") or []
                pbc_url = f"https://pbc.xxx/wiki/{page_title_str.replace(' ', '_')}" if page_title_str else ""

                now = int(time.time())
                lock_profile_enrichment(conn, orig_name)
                locked_fields = get_override_fields(conn, orig_name)
                with conn:
                    try:
                        conn.execute("""
                            INSERT INTO performer_bios
                                (name, bio, birth, aliases, updated_at, pbc_url)
                            VALUES (%s, %s, %s, %s, %s, %s)
                            ON CONFLICT (name) DO UPDATE SET
                                bio = CASE WHEN %s THEN performer_bios.bio ELSE EXCLUDED.bio END,
                                birth = EXCLUDED.birth,
                                pbc_url = CASE WHEN %s != '' THEN %s ELSE performer_bios.pbc_url END,
                                aliases = CASE WHEN %s THEN performer_bios.aliases ELSE EXCLUDED.aliases END,
                                updated_at = EXCLUDED.updated_at
                        """, (orig_name, bio, birth, json.dumps(aliases, ensure_ascii=False), now, pbc_url,
                              "bio" in locked_fields, pbc_url, pbc_url, "aliases" in locked_fields))
                    except Exception:
                        conn.execute("""
                            INSERT INTO performer_bios
                                (name, bio, birth, aliases, updated_at)
                            VALUES (%s, %s, %s, %s, %s)
                            ON CONFLICT (name) DO UPDATE SET
                                bio = CASE WHEN %s THEN performer_bios.bio ELSE EXCLUDED.bio END,
                                birth = EXCLUDED.birth,
                                aliases = CASE WHEN %s THEN performer_bios.aliases ELSE EXCLUDED.aliases END,
                                updated_at = EXCLUDED.updated_at
                        """, (orig_name, bio, birth, json.dumps(aliases, ensure_ascii=False), now,
                              "bio" in locked_fields, "aliases" in locked_fields))

                    for a in ([] if "aliases" in locked_fields else aliases):
                        a_clean = str(a or "").strip().lower()
                        if not a_clean or a_clean == orig_name.strip().lower():
                            continue
                        if not is_safe_alias(a_clean, orig_name):
                            continue
                        rev = conn.execute(
                            "SELECT 1 FROM performer_aliases WHERE alias = %s AND lower(canonical) = %s",
                            (orig_name.strip().lower(), a_clean)
                        ).fetchone()
                        if rev:
                            continue
                        conn.execute("""
                            INSERT INTO performer_aliases (alias, canonical)
                            VALUES (%s, %s)
                            ON CONFLICT (alias) DO NOTHING
                        """, (a_clean, orig_name))

                    if photo_url and "photo" not in locked_fields:
                        conn.execute("""
                            INSERT INTO performer_photos (name, thumb, updated_at)
                            VALUES (%s, %s, %s)
                            ON CONFLICT (name) DO UPDATE SET
                                thumb = CASE WHEN %s THEN performer_photos.thumb ELSE EXCLUDED.thumb END,
                                updated_at = EXCLUDED.updated_at
                        """, (orig_name, photo_url, now, "photo" in locked_fields))

                pbc_found += 1
                if photo_url:
                    pbc_photos_found += 1
                if bio:
                    pbc_bios_found += 1

                enriched_performers[orig_name] = {
                    "bio": bio,
                    "birth": birth,
                    "photo": photo_url,
                    "bio_locked": "bio" in locked_fields,
                    "photo_locked": "photo" in locked_fields,
                    "updated_at": now,
                }

        except Exception as e:
            logger.warning("Error in PBC batch %d-%d: %s", i, i + len(batch), e)

        time.sleep(0.3)
        if (i // batch_size) % 5 == 0 or (i + batch_size) >= len(all_names):
            logger.info("PBC Progress: %d/%d processed (Found: %d, Photos: %d, Bios: %d)",
                        min(i + batch_size, len(all_names)), len(all_names), pbc_found, pbc_photos_found, pbc_bios_found)

    logger.info(
        "PBC scan complete: %d performers found on PBC, %d photos, %d bios.",
        pbc_found,
        pbc_photos_found,
        pbc_bios_found,
    )

    # 3. Publish to Jellyfin Person pages
    logger.info("Publishing enriched performers to Jellyfin...")
    jf_overview_updated = 0
    jf_images_uploaded = 0
    jf_errors = 0

    for name, data in enriched_performers.items():
        bio = data["bio"]
        birth = data["birth"]
        photo_url = data["photo"]
        bio_locked = data["bio_locked"]
        photo_locked = data["photo_locked"]
        updated_at = data["updated_at"]

        try:
            resp = client.get(f"{jf_url.rstrip('/')}/Persons/{quote(name, safe='')}", headers=jf_headers)
            if resp.status_code == 404:
                continue
            if resp.status_code != 200:
                continue

            person = resp.json()
            if person.get("Type") != "Person":
                continue

            person_id = person["Id"]
            old_overview = person.get("Overview") or ""

            # Clean any legacy StashDB markers
            base_overview = old_overview
            for marker in LEGACY_MARKERS:
                base_overview = base_overview.split(marker, 1)[0]
            base_overview = base_overview.strip()

            pbc_text = format_pbc_overview("" if bio_locked else bio, birth)
            if base_overview and pbc_text and base_overview != pbc_text:
                new_overview = f"{base_overview}\n\n{pbc_text}"
            elif pbc_text:
                new_overview = pbc_text
            else:
                new_overview = base_overview

            # Update bio if changed
            if old_overview != new_overview and new_overview:
                person["Overview"] = new_overview
                up_resp = client.post(f"{jf_url.rstrip('/')}/Items/{person_id}", headers=jf_headers, json=person)
                if up_resp.status_code in (200, 204):
                    jf_overview_updated += 1

            # Upload photo if missing
            has_primary = bool((person.get("ImageTags") or {}).get("Primary"))
            if photo_url and not photo_locked and not has_primary:
                try:
                    img_resp = request_with_host_limit(
                        client, "get", photo_url, timeout=15.0, follow_redirects=True,
                    )
                    if img_resp.status_code == 200 and len(img_resp.content) > 1000:
                        content_type = img_resp.headers.get("content-type", "").split(";")[0]
                        if not content_type:
                            content_type = "image/jpeg"
                        img_b64 = base64.b64encode(img_resp.content)
                        img_post = client.post(
                            f"{jf_url.rstrip('/')}/Items/{person_id}/Images/Primary",
                            headers={**jf_headers, "Content-Type": content_type},
                            content=img_b64,
                        )
                        if img_post.status_code in (200, 204):
                            jf_images_uploaded += 1
                except Exception as img_err:
                    logger.debug("Failed to upload image for %s: %s", name, img_err)

            with conn:
                conn.execute("""
                    INSERT INTO jellyfin_person_sync
                        (name, pbc_updated_at, format_version)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (name) DO UPDATE SET
                        pbc_updated_at = EXCLUDED.pbc_updated_at,
                        format_version = EXCLUDED.format_version
                """, (name, updated_at, FORMAT_VERSION))

        except Exception as e:
            jf_errors += 1
            logger.debug("Error updating Jellyfin for %s: %s", name, e)

    conn.close()
    logger.info("=== ENRICHMENT SUMMARY ===")
    logger.info("Total performers checked: %d", len(all_names))
    logger.info("Performers found on PBC: %d", pbc_found)
    logger.info("High-res photos collected: %d", pbc_photos_found)
    logger.info("Biographies / career details collected: %d", pbc_bios_found)
    logger.info("Jellyfin actor overviews updated: %d", jf_overview_updated)
    logger.info("Jellyfin actor portraits uploaded: %d", jf_images_uploaded)


if __name__ == "__main__":
    run_deep_enrichment()
