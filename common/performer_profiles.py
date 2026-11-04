"""Revisioned manual performer profile overrides shared with enrichment workers."""

from __future__ import annotations

import json
import ipaddress
import re
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from psycopg.types.json import Jsonb

from common.performers import is_safe_alias, is_valid_performer_name


PROFILE_FIELDS = ("bio", "photo", "aliases")
MAX_BIO_LENGTH = 12_000
MAX_ALIASES = 100
MAX_ALIAS_LENGTH = 200
_SENSITIVE_QUERY_KEY = re.compile(r"token|key|auth|signature|secret|session|credential|password", re.I)


class PerformerProfileError(ValueError):
    """A profile update failed input or catalog validation."""


class PerformerProfileConflict(RuntimeError):
    """A profile revision or alias ownership changed since it was read."""

    def __init__(self, message: str, revision: int | None = None):
        super().__init__(message)
        self.revision = revision


def _json_list(value) -> list[str]:
    if isinstance(value, list):
        raw = value
    elif isinstance(value, str) and value:
        try:
            raw = json.loads(value)
        except (TypeError, ValueError):
            raw = []
    else:
        raw = []
    return [item.strip() for item in raw if isinstance(item, str) and item.strip()] if isinstance(raw, list) else []


def _json_object(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def validate_photo_url(value) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > 2048:
        raise PerformerProfileError("Artwork URL must be at most 2048 characters.")
    value = value.strip()
    if not value:
        return ""
    parts = urlsplit(value)
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or parts.fragment or any(_SENSITIVE_QUERY_KEY.search(key) for key, _ in parse_qsl(parts.query, keep_blank_values=True))):
        raise PerformerProfileError("Artwork must use a safe HTTPS URL without credentials, fragments, or secret query parameters.")
    try:
        host = parts.hostname.encode("idna").decode("ascii").lower()
        port = parts.port
    except UnicodeError as exc:
        raise PerformerProfileError("Artwork URL host is invalid.") from exc
    except ValueError as exc:
        raise PerformerProfileError("Artwork URL host or port is invalid.") from exc
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")) or "." not in host:
        raise PerformerProfileError("Artwork host must be a public hostname.")
    try:
        literal_ip = ipaddress.ip_address(host)
    except ValueError:
        literal_ip = None
    if literal_ip is not None and not literal_ip.is_global:
        raise PerformerProfileError("Artwork host must not be a private or reserved IP address.")
    netloc = host + (f":{port}" if port and port != 443 else "")
    return urlunsplit(("https", netloc, parts.path or "/", parts.query, ""))


def validate_profile(name: str, bio, photo, aliases) -> dict:
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > MAX_ALIAS_LENGTH:
        raise PerformerProfileError("Performer name is invalid.")
    if not isinstance(bio, str) or len(bio) > MAX_BIO_LENGTH or any(ord(char) < 32 and char not in "\n\r\t" for char in bio):
        raise PerformerProfileError("Biography is invalid or too long.")
    photo = validate_photo_url(photo)
    if not isinstance(aliases, list) or len(aliases) > MAX_ALIASES:
        raise PerformerProfileError(f"Provide at most {MAX_ALIASES} aliases.")
    canonical = name.strip()
    cleaned = []
    seen = set()
    for raw in aliases:
        if not isinstance(raw, str) or not raw.strip() or len(raw.strip()) > MAX_ALIAS_LENGTH:
            raise PerformerProfileError("Each alias must contain 1–200 characters.")
        alias = " ".join(raw.strip().split())
        key = alias.casefold()
        if key == canonical.casefold() or key in seen:
            raise PerformerProfileError("Aliases must be unique and different from the canonical name.")
        if not is_safe_alias(alias, canonical):
            raise PerformerProfileError(f"Alias is not safe to use for performer matching: {alias}.")
        seen.add(key)
        cleaned.append(alias)
    if not is_valid_performer_name(canonical):
        raise PerformerProfileError("Canonical performer name is invalid.")
    return {"bio": bio.strip(), "photo": photo, "aliases": cleaned}


def _profile_snapshot(conn, name: str) -> dict:
    try:
        bio_row = conn.execute(
            "SELECT bio,aliases,stashdb_url,pbc_url FROM performer_bios WHERE lower(name)=lower(%s) ORDER BY name LIMIT 1",
            (name,),
        ).fetchone()
        stashdb_url = str(bio_row[2] or "") if bio_row and len(bio_row) > 2 else ""
        pbc_url = str(bio_row[3] or "") if bio_row and len(bio_row) > 3 else ""
    except Exception:
        bio_row = conn.execute(
            "SELECT bio,aliases FROM performer_bios WHERE lower(name)=lower(%s) ORDER BY name LIMIT 1",
            (name,),
        ).fetchone()
        stashdb_url = ""
        pbc_url = ""
    photo_row = conn.execute(
        "SELECT thumb FROM performer_photos WHERE lower(name)=lower(%s) ORDER BY name LIMIT 1",
        (name,),
    ).fetchone()
    mappings = conn.execute(
        "SELECT alias,canonical FROM performer_aliases WHERE lower(canonical)=lower(%s) ORDER BY lower(alias),alias",
        (name,),
    ).fetchall()
    bio_aliases = _json_list(bio_row[1]) if bio_row else []
    mapped_aliases = [str(row[0]) for row in mappings]
    aliases, seen = [], set()
    for alias in [*bio_aliases, *mapped_aliases]:
        key = alias.casefold()
        if key != name.casefold() and key not in seen:
            seen.add(key)
            aliases.append(alias)
    return {
        "bio": str(bio_row[0] or "") if bio_row else "",
        "photo": str(photo_row[0] or "") if photo_row else "",
        "aliases": aliases,
        "stashdb_url": stashdb_url,
        "pbc_url": pbc_url,
        "alias_snapshot": {
            "bio_aliases": bio_aliases,
            "mapped": [{"alias": str(row[0]), "canonical": str(row[1])} for row in mappings],
        },
    }


def read_profile(conn, name: str) -> dict:
    current = _profile_snapshot(conn, name)
    row = conn.execute(
        "SELECT fields,revision,updated_at FROM performer_profile_overrides WHERE lower(name)=lower(%s)",
        (name,),
    ).fetchone()
    fields = _json_object(row[0]) if row else {}
    for field in PROFILE_FIELDS:
        entry = fields.get(field)
        if isinstance(entry, dict) and field in entry:
            current[field] = entry[field]
    updated = row[2].isoformat() if row and hasattr(row[2], "isoformat") else (row[2] if row else None)
    return {"name": name, "bio": current["bio"], "photo": current["photo"],
            "aliases": current["aliases"], "stashdb_url": current.get("stashdb_url", ""),
            "pbc_url": current.get("pbc_url", ""),
            "revision": int(row[1]) if row else 0,
            "overrides": sorted(field for field in PROFILE_FIELDS if field in fields),
            "updated_at": updated}


def get_override_fields(conn, name: str) -> set[str]:
    """Return manually locked fields (migration 40 is required before worker startup)."""
    row = conn.execute(
        "SELECT fields FROM performer_profile_overrides WHERE lower(name)=lower(%s)", (name,)
    ).fetchone()
    fields = _json_object(row[0]) if row else {}
    return {field for field in PROFILE_FIELDS if field in fields}


def lock_profile_enrichment(conn, name: str) -> None:
    """Serialize provider writes with manual edits for this identity."""
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"performer-profile:{name.casefold()}",))


def _write_base_field(conn, name: str, field: str, value, now_epoch: int) -> None:
    if field == "bio":
        existing = conn.execute(
            "SELECT name FROM performer_bios WHERE lower(name)=lower(%s) ORDER BY name LIMIT 1 FOR UPDATE", (name,)
        ).fetchone()
        if existing:
            conn.execute("UPDATE performer_bios SET bio=%s,updated_at=%s WHERE name=%s",
                         (value, now_epoch, existing[0]))
        else:
            conn.execute("INSERT INTO performer_bios(name,bio,updated_at) VALUES (%s,%s,%s)",
                         (name, value, now_epoch))
    elif field == "photo":
        existing = conn.execute(
            "SELECT name FROM performer_photos WHERE lower(name)=lower(%s) ORDER BY name LIMIT 1 FOR UPDATE", (name,)
        ).fetchone()
        if existing:
            conn.execute("UPDATE performer_photos SET thumb=%s,updated_at=%s WHERE name=%s",
                         (value, now_epoch, existing[0]))
        else:
            conn.execute("INSERT INTO performer_photos(name,thumb,updated_at) VALUES (%s,%s,%s)",
                         (name, value, now_epoch))
    elif field == "aliases":
        existing = conn.execute(
            "SELECT name FROM performer_bios WHERE lower(name)=lower(%s) ORDER BY name LIMIT 1 FOR UPDATE", (name,)
        ).fetchone()
        if existing:
            conn.execute("UPDATE performer_bios SET aliases=%s,updated_at=%s WHERE name=%s",
                         (Jsonb(value), now_epoch, existing[0]))
        else:
            conn.execute("INSERT INTO performer_bios(name,aliases,updated_at) VALUES (%s,%s,%s)",
                         (name, Jsonb(value), now_epoch))
        conn.execute("DELETE FROM performer_aliases WHERE lower(canonical)=lower(%s)", (name,))
        for alias in value:
            conn.execute("INSERT INTO performer_aliases(alias,canonical) VALUES (%s,%s)", (alias.lower(), name))


def _check_alias_collisions(conn, name: str, aliases: list[str]) -> None:
    if not aliases:
        return
    names = conn.execute(
        "SELECT DISTINCT performer_name FROM video_performers WHERE lower(performer_name)=ANY(%s)",
        ([alias.lower() for alias in aliases],),
    ).fetchall()
    other_canonical = {str(row[0]).casefold() for row in names if str(row[0]).casefold() != name.casefold()}
    if other_canonical:
        raise PerformerProfileConflict("An alias matches another canonical performer name.")
    mappings = conn.execute(
        "SELECT alias,canonical FROM performer_aliases WHERE lower(alias)=ANY(%s) AND lower(canonical)<>lower(%s)",
        ([alias.lower() for alias in aliases], name),
    ).fetchall()
    if mappings:
        raise PerformerProfileConflict("An alias is already assigned to another performer.")


def update_profile(conn, *, name: str, expected_revision: int, profile: dict,
                   reset_fields: list[str] | None = None) -> dict:
    """Atomically save/reset profile fields with CAS, source snapshots, and audit."""
    from common.admin_audit_events import record_performer_profile_change
    import time

    reset_fields = reset_fields or []
    _check_profile_request(expected_revision, reset_fields)
    valid = validate_profile(name, profile.get("bio"), profile.get("photo"), profile.get("aliases"))
    # Serializes first-write requests too, where no override row exists yet.
    lock_profile_enrichment(conn, name)
    name, old_fields, revision = _lock_profile_override(conn, name)
    if expected_revision != revision:
        raise PerformerProfileConflict("Performer profile changed. Reload it before saving.", revision)
    before = _profile_snapshot(conn, name)
    next_fields = dict(old_fields)
    now = int(time.time())
    changed: list[str] = []

    if "aliases" not in reset_fields:
        _check_alias_collisions(conn, name, valid["aliases"])
    _reset_profile_fields(conn, name, reset_fields, next_fields, before, revision, now, changed)
    _save_profile_fields(conn, name, valid, reset_fields, next_fields, before, now, changed)

    changed = sorted(set(changed))
    if not changed:
        return read_profile(conn, name)
    next_revision = revision + 1
    conn.execute(
        "INSERT INTO performer_profile_overrides(name,fields,revision,updated_at) VALUES (%s,%s,%s,CURRENT_TIMESTAMP) "
        "ON CONFLICT(name) DO UPDATE SET fields=EXCLUDED.fields,revision=EXCLUDED.revision,updated_at=EXCLUDED.updated_at",
        (name, Jsonb(next_fields), next_revision),
    )
    after = _profile_snapshot(conn, name)
    for field in PROFILE_FIELDS:
        entry = next_fields.get(field)
        if isinstance(entry, dict) and "value" in entry:
            after[field] = entry["value"]
    record_performer_profile_change(conn, name=name, revision=next_revision,
                                    before=before, after=after, fields=changed)
    return {"name": name, "bio": after["bio"], "photo": after["photo"],
            "aliases": after["aliases"], "revision": next_revision,
            "overrides": sorted(next_fields), "updated_at": None}


def _check_profile_request(expected_revision, reset_fields: list[str]) -> None:
    if (type(expected_revision) is not int or expected_revision < 0
            or any(field not in PROFILE_FIELDS for field in reset_fields)):
        raise PerformerProfileError("Invalid performer profile revision or reset field.")


def _lock_profile_override(conn, name: str) -> tuple[str, dict, int]:
    row = conn.execute(
        "SELECT name,fields,revision FROM performer_profile_overrides WHERE lower(name)=lower(%s) FOR UPDATE",
        (name,),
    ).fetchone()
    if row:
        name = str(row[0])
    old_fields = _json_object(row[1]) if row else {}
    revision = int(row[2]) if row else 0
    return name, old_fields, revision


def _reset_profile_fields(conn, name: str, reset_fields: list[str], next_fields: dict,
                          before: dict, revision: int, now: int, changed: list[str]) -> None:
    for field in reset_fields:
        entry = next_fields.get(field)
        if not isinstance(entry, dict) or "base" not in entry:
            continue
        base = entry["base"]
        if field == "aliases":
            _restore_alias_base(conn, name, base, revision, now)
        else:
            _write_base_field(conn, name, field, base, now)
        if before[field] != base:
            changed.append(field)
        next_fields.pop(field, None)


def _restore_alias_base(conn, name: str, base, revision: int, now: int) -> None:
    """Put the saved provider alias snapshot back and drop the manual mappings."""
    if not isinstance(base, dict):
        raise PerformerProfileConflict("Saved alias source snapshot is invalid.", revision)
    conn.execute("DELETE FROM performer_aliases WHERE lower(canonical)=lower(%s)", (name,))
    for mapping in base.get("mapped", []):
        alias, canonical = mapping.get("alias"), mapping.get("canonical")
        if not isinstance(alias, str) or not isinstance(canonical, str):
            continue
        collision = conn.execute("SELECT canonical FROM performer_aliases WHERE lower(alias)=lower(%s)", (alias,)).fetchone()
        if collision and str(collision[0]).casefold() != canonical.casefold():
            raise PerformerProfileConflict("A saved provider alias now belongs to another performer.", revision)
        conn.execute("INSERT INTO performer_aliases(alias,canonical) VALUES (%s,%s) ON CONFLICT(alias) DO NOTHING", (alias, canonical))
    bio_row = conn.execute(
        "SELECT name FROM performer_bios WHERE lower(name)=lower(%s) ORDER BY name LIMIT 1 FOR UPDATE", (name,)
    ).fetchone()
    if bio_row:
        conn.execute("UPDATE performer_bios SET aliases=%s,updated_at=%s WHERE name=%s",
                     (Jsonb(base.get("bio_aliases", [])), now, bio_row[0]))
    else:
        conn.execute("INSERT INTO performer_bios(name,aliases,updated_at) VALUES (%s,%s,%s)",
                     (name, Jsonb(base.get("bio_aliases", [])), now))


def _save_profile_fields(conn, name: str, valid: dict, reset_fields: list[str], next_fields: dict,
                         before: dict, now: int, changed: list[str]) -> None:
    for field in PROFILE_FIELDS:
        if field in reset_fields:
            continue
        value = valid[field]
        prior = next_fields.get(field)
        if isinstance(prior, dict) and "base" in prior:
            entry = {"base": prior["base"], "value": value}
        elif value != before[field]:
            base = before["alias_snapshot"] if field == "aliases" else before[field]
            entry = {"base": base, "value": value}
        else:
            # Saving an untouched provider value must not turn it into a locked override.
            continue
        if before[field] != value or not isinstance(prior, dict):
            changed.append(field)
        next_fields[field] = entry
        _write_base_field(conn, name, field, value, now)


def merge_override_names(conn, alias: str, canonical: str) -> list[str]:
    """Return identities with active overrides; callers can require a reset first."""
    rows = conn.execute(
        "SELECT name,fields FROM performer_profile_overrides "
        "WHERE lower(name) IN (lower(%s),lower(%s)) ORDER BY lower(name)",
        (alias, canonical),
    ).fetchall()
    return [str(name) for name, raw in rows if any(field in _json_object(raw) for field in PROFILE_FIELDS)]

