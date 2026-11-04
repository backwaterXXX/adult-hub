"""Publish PBC performer details and photos on Jellyfin person pages."""

import base64
import logging
import re
from common import db
from common.http_rate_limit import wait_for_host_slot
from urllib.parse import quote, urlsplit

import httpx

logger = logging.getLogger("jellyfin_people")
LEGACY_MARKERS = ("StashDB:", "PBC:", "Informacje ze StashDB:", "Dane o wykonawcy:")
FORMAT_VERSION = 7


def format_pbc_overview(bio: str, birth: str = "", nationality: str = "",
                        stashdb_url: str = "", pbc_url: str = "") -> str:
    """Format bio, birth, and nationality details from PBC/TPDB/StashDB with links."""
    parts = []
    if bio and bio.strip():
        parts.append(bio.strip())
    meta = []
    bio_lower = (bio or "").lower()
    if birth and birth.strip() and birth.strip().lower() not in bio_lower:
        meta.append(f"Born: {birth.strip()}")
    if nationality and nationality.strip() and nationality.strip().lower() not in bio_lower:
        meta.append(f"Nationality: {nationality.strip()}")
    if meta:
        parts.append(" · ".join(meta))
    links = []
    if stashdb_url and stashdb_url.strip():
        links.append(f"StashDB: {stashdb_url.strip()}")
    if pbc_url and pbc_url.strip():
        links.append(f"PBC: {pbc_url.strip()}")
    if links:
        parts.append(" | ".join(links))
    return "\n\n".join(parts)


def sync_jellyfin_people(conn: db.Connection, url: str, api_key: str,
                         limit: int = 50, http_get=None, http_post=None,
                         http_image_get=None):
    """Update existing Jellyfin Person overviews, metadata, and photos; return (checked, updated)."""
    if not url or not api_key or limit <= 0:
        return 0, 0

    # Find legacy synced names from jellyfin_person_stash_sync that need StashDB markers cleaned up
    legacy_names = set()
    try:
        tables = db.table_names(conn)
        if "jellyfin_person_stash_sync" in tables:
            legacy_names = {r[0] for r in conn.execute("SELECT name FROM jellyfin_person_stash_sync").fetchall()}
    except Exception as _exc:
        logger.debug("Ignored exception in %s", "sync_jellyfin_people", exc_info=True)

    has_nationality = False
    try:
        conn.execute("SELECT nationality FROM performer_bios LIMIT 1")
        has_nationality = True
    except Exception:
        has_nationality = False

    nat_expr = "COALESCE(b.nationality, '')" if has_nationality else "''"

    has_links = False
    try:
        conn.execute("SELECT stashdb_url, pbc_url FROM performer_bios LIMIT 1")
        has_links = True
    except Exception:
        has_links = False

    links_expr = "COALESCE(b.stashdb_url, ''), COALESCE(b.pbc_url, '')" if has_links else "'', ''"

    rows = []
    try:
        rows = conn.execute(f"""
            SELECT b.name, b.bio, b.birth, b.aliases, b.updated_at, p.thumb, {nat_expr}, {links_expr}
            FROM performer_bios b
            LEFT JOIN performer_photos p ON lower(p.name) = lower(b.name)
            LEFT JOIN jellyfin_person_sync j ON lower(j.name) = lower(b.name)
            WHERE (j.name IS NULL OR j.pbc_updated_at != b.updated_at
                   OR j.format_version != %s)
            ORDER BY b.name LIMIT %s
        """, (FORMAT_VERSION, limit)).fetchall()
    except db.OperationalError:
        rows = []

    # If limit allows, also include legacy names that need StashDB marker removal
    candidate_names = {r[0].lower() for r in rows}
    remaining_limit = limit - len(rows)
    if remaining_limit > 0 and legacy_names:
        for leg_name in list(legacy_names)[:remaining_limit]:
            if leg_name.lower() not in candidate_names:
                b_row = conn.execute(
                    f"SELECT bio, birth, aliases, updated_at, {nat_expr}, {links_expr} FROM performer_bios WHERE lower(name) = lower(%s)",
                    (leg_name,)
                ).fetchone()
                p_row = conn.execute(
                    "SELECT thumb FROM performer_photos WHERE lower(name) = lower(%s)",
                    (leg_name,)
                ).fetchone()
                bio = b_row[0] if b_row else ""
                birth = b_row[1] if b_row else ""
                aliases = b_row[2] if b_row else "[]"
                updated_at = b_row[3] if b_row else 0
                nat = b_row[4] if b_row and len(b_row) > 4 else ""
                s_url = b_row[5] if b_row and len(b_row) > 5 else ""
                p_url = b_row[6] if b_row and len(b_row) > 6 else ""
                thumb = p_row[0] if p_row else ""
                rows.append((leg_name, bio, birth, aliases, updated_at, thumb, nat, s_url, p_url))
                candidate_names.add(leg_name.lower())

    if not rows:
        return 0, 0

    headers = {"Authorization": f'MediaBrowser Token="{api_key}"'}
    get = http_get or httpx.get
    post = http_post or httpx.post
    checked = updated = 0
    for row in rows:
        name = row[0]
        bio = row[1]
        birth = row[2]
        updated_at = row[4]
        photo_url = row[5]
        nationality = row[6] if len(row) > 6 else ""
        stashdb_url = row[7] if len(row) > 7 else ""
        pbc_url = row[8] if len(row) > 8 else ""
        checked += 1
        try:
            response = get(f"{url.rstrip('/')}/Persons/{quote(name, safe='')}",
                           headers=headers, timeout=10)
            if response.status_code == 404:
                continue  # Person not yet created by Jellyfin library scanner.
            response.raise_for_status()
            item = response.json()
            if item.get("Type") != "Person" or (item.get("Name") or "").casefold() != name.casefold():
                continue
            old = item.get("Overview") or ""
            base = old
            for marker in LEGACY_MARKERS:
                base = base.split(marker, 1)[0]
            base = base.strip()

            pbc_section = format_pbc_overview(bio or "", birth or "", nationality or "",
                                              stashdb_url=stashdb_url, pbc_url=pbc_url)
            if base and pbc_section and base != pbc_section and base != (bio or "").strip():
                overview = f"{base}\n\n{pbc_section}"
            elif pbc_section:
                overview = pbc_section
            else:
                overview = base

            item_changed = False
            if old != overview:
                item["Overview"] = overview
                item_changed = True

            # Enrich ExternalUrls
            ext_urls = []
            if stashdb_url:
                ext_urls.append({"Name": "StashDB", "Url": stashdb_url})
            if pbc_url:
                ext_urls.append({"Name": "PBC", "Url": pbc_url})
            if ext_urls and item.get("ExternalUrls") != ext_urls:
                item["ExternalUrls"] = ext_urls
                item_changed = True

            # Enrich ProviderIds (StashDB)
            if stashdb_url and "/performers/" in stashdb_url:
                stashdb_id = stashdb_url.split("/performers/")[-1].strip()
                if stashdb_id:
                    pids = dict(item.get("ProviderIds") or {})
                    if pids.get("StashDB") != stashdb_id:
                        pids["StashDB"] = stashdb_id
                        item["ProviderIds"] = pids
                        item_changed = True

            # Enrich native Jellyfin person fields (ProductionLocations and Tags)
            if nationality:
                prod_locs = list(item.get("ProductionLocations") or [])
                if nationality not in prod_locs:
                    item["ProductionLocations"] = [nationality]
                    item_changed = True
                tags = list(item.get("Tags") or [])
                if nationality not in tags:
                    item["Tags"] = tags + [nationality]
                    item_changed = True

            # Enrich PremiereDate (Birthdate) if valid YYYY-MM-DD
            if birth and re.match(r"^\d{4}-\d{2}-\d{2}$", birth.strip()):
                birth_iso = f"{birth.strip()}T00:00:00.0000000Z"
                if item.get("PremiereDate") != birth_iso:
                    item["PremiereDate"] = birth_iso
                    try:
                        item["ProductionYear"] = int(birth.strip()[:4])
                    except Exception as _exc:
                        logger.debug("Ignored exception in %s", "sync_jellyfin_people", exc_info=True)
                    item_changed = True

            if item_changed:
                response = post(f"{url.rstrip('/')}/Items/{item['Id']}",
                                headers=headers, json=item, timeout=15)
                response.raise_for_status()

            if photo_url and not (item.get("ImageTags") or {}).get("Primary"):
                source = urlsplit(photo_url)
                if source.scheme not in ("http", "https"):
                    raise ValueError(f"invalid photo URL scheme: {photo_url}")
                wait_for_host_slot(photo_url)
                image_response = (http_image_get or httpx.get)(
                    photo_url, timeout=15, follow_redirects=True,
                )
                image_response.raise_for_status()
                image_type = image_response.headers.get("content-type", "").split(";", 1)[0]
                image_data = image_response.content
                if image_type not in ("image/jpeg", "image/png", "image/webp") or not 0 < len(image_data) <= 10_000_000:
                    raise ValueError("invalid image response")
                image_headers = {**headers, "Content-Type": image_type}
                response = post(
                    f"{url.rstrip('/')}/Items/{item['Id']}/Images/Primary",
                    headers=image_headers, content=base64.b64encode(image_data), timeout=15,
                )
                response.raise_for_status()

            with conn:
                conn.execute(
                    "INSERT INTO jellyfin_person_sync "
                    "(name, pbc_updated_at, format_version) VALUES (%s, %s, %s) "
                    "ON CONFLICT (name) DO UPDATE SET pbc_updated_at=EXCLUDED.pbc_updated_at, "
                    "format_version=EXCLUDED.format_version",
                    (name, updated_at or 0, FORMAT_VERSION),
                )
                if legacy_names and name in legacy_names:
                    conn.execute("DELETE FROM jellyfin_person_stash_sync WHERE name = %s", (name,))
            updated += 1
        except Exception as exc:
            logger.warning("Could not update Jellyfin person %r: %s", name, exc)

    if checked:
        logger.info("Jellyfin person metadata (PBC): checked=%d, updated=%d", checked, updated)
    return checked, updated
