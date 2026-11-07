"""Safe, idempotent configuration audit and migration utilities.

Provides:
1. Environment configuration inspection and validation with redacted secrets.
2. Idempotent import of static config.yaml channels into managed overlay without
   overwriting any existing Admin UI modifications.
3. Dry-run inspection mode for safe verification before changes.
"""

from __future__ import annotations

import copy
import logging
import os
from pathlib import Path
import re
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from common.config import (
    ConfigurationError,
    get_admin_write_token,
    get_database_url,
    get_stashdb_api_key,
    get_theporndb_api_token,
    parse_daily_at,
    parse_int,
    parse_url,
)
from common.config_loader import (
    ConfigError,
    _read_config_data,
    find_config_file,
)
from common.managed_sources import (
    _publish_overlay,
    config_lock,
    overrides_path,
    read_overrides,
    validate_overlay,
)

logger = logging.getLogger("common.config_migration")

SECRET_KEYS = frozenset({
    "DATABASE_URL",
    "STANDALONE_DATABASE_URL",
    "TEST_DATABASE_URL",
    "BACKUP_RESTORE_DATABASE_URL",
    "POSTGRES_PASSWORD",
    "ADMIN_WRITE_TOKEN",
    "ADMIN_DUPLICATE_TOKEN",
    "TELEGRAM_API_HASH",
    "JELLYFIN_API_KEY",
    "STANDALONE_JELLYFIN_API_KEY",
    "STASHDB_API_KEY",
    "STASH_API_KEY",
    "THEPORNDB_API_TOKEN",
    "THEPORNDB_API_KEY",
})


def redact_url_credentials(url: str) -> str:
    """Redact userinfo and sensitive query parameters from a URL."""
    try:
        parsed = urlsplit(url)
        if not parsed.scheme or not parsed.netloc:
            return url
        netloc = parsed.netloc
        if "@" in netloc:
            _, _, hostport = netloc.rpartition("@")
            netloc = f"***:***@{hostport}"
        query = parsed.query
        if query:
            # Mask entire query string if it could contain auth tokens
            query = "[REDACTED]"
        return urlunsplit((parsed.scheme, netloc, parsed.path, query, parsed.fragment))
    except Exception:
        return "[REDACTED]"


def redact_sensitive_text(text: str) -> str:
    """Mask any embedded credentials or secrets from an error or warning string."""
    if not text:
        return ""
    # Redact URLs containing credentials or tokens in userinfo: scheme://user:pass@host
    redacted = re.sub(
        r"([a-zA-Z][a-zA-Z0-9+.-]*://)([^@/\s]+)@",
        r"\1***:***@",
        text,
    )
    # Redact query parameters that look like tokens or keys
    redacted = re.sub(
        r"([?&](?:token|key|secret|password|api_key|auth)=)[^&\s]+",
        r"\1[REDACTED]",
        redacted,
        flags=re.IGNORECASE,
    )
    return redacted


def redact_value(key: str, value: Any) -> str:
    """Return redacted string for sensitive configuration values."""
    if value is None or str(value).strip() == "":
        return "<unset>"
    if key in SECRET_KEYS or any(s in key.lower() for s in ("token", "password", "secret", "hash", "key")):
        return "[REDACTED]"
    val_str = str(value)
    if "://" in val_str:
        return redact_url_credentials(val_str)
    return val_str


def _check_database_url(env_map: Mapping[str, str], warnings: list[str], summary: dict[str, str]) -> None:
    raw_db = env_map.get("DATABASE_URL") or env_map.get("STANDALONE_DATABASE_URL") or ""
    if not raw_db:
        warnings.append("DATABASE_URL is not set. A PostgreSQL 17 connection is required for operation.")
        return
    try:
        get_database_url(env=env_map)
        summary["DATABASE_URL"] = "[CONFIGURED]"
    except Exception as exc:
        clean_msg = redact_sensitive_text(str(exc))
        warnings.append(f"DATABASE_URL validation error: {clean_msg}")


def _check_legacy_aliases(env_map: Mapping[str, str], aliases: list[dict[str, str]]) -> None:
    alias_checks = (
        ("ADMIN_DUPLICATE_TOKEN", "ADMIN_WRITE_TOKEN"),
        ("STASH_API_KEY", "STASHDB_API_KEY"),
        ("THEPORNDB_API_KEY", "THEPORNDB_API_TOKEN"),
        ("STANDALONE_DATABASE_URL", "DATABASE_URL"),
    )
    for legacy, canonical in alias_checks:
        if env_map.get(legacy) and not env_map.get(canonical):
            aliases.append({
                "legacy": legacy,
                "canonical": canonical,
                "note": f"Using {legacy} as fallback for {canonical}.",
            })


def _validate_urls(env_map: Mapping[str, str], warnings: list[str], summary: dict[str, str]) -> None:
    url_keys = ("RESOLVER_PUBLIC_URL", "RESOLVER_API_URL", "JELLYFIN_URL", "STASHDB_API_URL")
    for key in url_keys:
        val = env_map.get(key)
        if not val:
            continue
        try:
            parse_url(val, name=key)
            summary[key] = redact_url_credentials(val)
        except ConfigurationError as err:
            warnings.append(redact_sensitive_text(str(err)))


def _validate_numeric_settings(env_map: Mapping[str, str], warnings: list[str], summary: dict[str, str]) -> None:
    int_checks = (
        ("BACKUP_KEEP_DAYS", 1),
        ("RESOLVER_CONCURRENCY_LIMIT", 1),
        ("CACHE_TTL_SECONDS", 1),
        ("MAX_CACHE_ENTRIES", 1),
    )
    for key, min_val in int_checks:
        val = env_map.get(key)
        if not val:
            continue
        try:
            summary[key] = str(parse_int(val, 0, minimum=min_val, name=key))
        except ConfigurationError as err:
            warnings.append(redact_sensitive_text(str(err)))

    val_daily = env_map.get("BACKUP_DAILY_AT")
    if val_daily:
        try:
            summary["BACKUP_DAILY_AT"] = parse_daily_at(val_daily, name="BACKUP_DAILY_AT")
        except ConfigurationError as err:
            warnings.append(redact_sensitive_text(str(err)))


def _check_credentials_and_tokens(env_map: Mapping[str, str], warnings: list[str], summary: dict[str, str]) -> None:
    tg_id = env_map.get("TELEGRAM_API_ID")
    tg_hash = env_map.get("TELEGRAM_API_HASH")
    if bool(tg_id) != bool(tg_hash):
        warnings.append("Both TELEGRAM_API_ID and TELEGRAM_API_HASH must be configured together.")
    if tg_id:
        summary["TELEGRAM_API_ID"] = "[CONFIGURED]"
    if tg_hash:
        summary["TELEGRAM_API_HASH"] = "[CONFIGURED]"

    try:
        if get_admin_write_token(env=env_map):
            summary["ADMIN_WRITE_TOKEN"] = "[CONFIGURED]"
    except ConfigurationError as err:
        warnings.append(redact_sensitive_text(str(err)))

    if get_stashdb_api_key(env=env_map):
        summary["STASHDB_API_KEY"] = "[CONFIGURED]"
    if get_theporndb_api_token(env=env_map):
        summary["THEPORNDB_API_TOKEN"] = "[CONFIGURED]"


def audit_configuration(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Inspect and validate current configuration without modifying state.

    Returns summary of valid settings, active aliases, and actionable warnings.
    All secrets are strictly redacted.
    """
    env_map = os.environ if env is None else env
    warnings: list[str] = []
    aliases_in_use: list[dict[str, str]] = []
    effective_summary: dict[str, str] = {}

    _check_database_url(env_map, warnings, effective_summary)
    _check_legacy_aliases(env_map, aliases_in_use)
    _validate_urls(env_map, warnings, effective_summary)
    _validate_numeric_settings(env_map, warnings, effective_summary)
    _check_credentials_and_tokens(env_map, warnings, effective_summary)

    return {
        "valid": len(warnings) == 0,
        "warnings": warnings,
        "aliases_in_use": aliases_in_use,
        "effective_summary": effective_summary,
    }


def _build_yaml_channel_entry(cid: str, ch_dict: dict[str, Any]) -> dict[str, Any]:
    """Preserve only valid fields present in legacy YAML and ensure studio label."""
    allowed_fields = (
        "enabled", "studio", "max_pages", "min_duration_seconds",
        "stream_mode", "history_batch_size", "tags_whitelist", "tags_blacklist"
    )
    channel_entry: dict[str, Any] = {}
    for field in allowed_fields:
        if field in ch_dict:
            val = ch_dict[field]
            channel_entry[field] = list(val) if isinstance(val, list) else val
    if "studio" not in channel_entry or not channel_entry["studio"]:
        channel_entry["studio"] = cid
    return channel_entry


def _process_yaml_channels(
    channels_raw: Any,
    existing_channels: dict[str, Any],
    removed_channels: set[str],
    imported: list[str],
    skipped_existing: list[str],
    skipped_removed: list[str],
    skipped_malformed: list[Any],
) -> None:
    if not isinstance(channels_raw, list):
        skipped_malformed.append(channels_raw)
        return
    for ch_dict in channels_raw:
        if not isinstance(ch_dict, dict) or not str(ch_dict.get("id", "")).strip():
            skipped_malformed.append(ch_dict)
            continue
        cid = str(ch_dict.get("id", "")).strip()
        if cid in existing_channels:
            skipped_existing.append(cid)
        elif cid in removed_channels:
            skipped_removed.append(cid)
        else:
            existing_channels[cid] = _build_yaml_channel_entry(cid, ch_dict)
            imported.append(cid)


def _extract_pure_yaml_channels(
    config_path: str | Path | None,
    existing_channels: dict[str, Any],
    removed_channels: set[str],
) -> tuple[list[str], list[str], list[str], list[Any]]:
    imported: list[str] = []
    skipped_existing: list[str] = []
    skipped_removed: list[str] = []
    skipped_malformed: list[Any] = []

    # Locate and read pure YAML file directly without applying overlay
    # and without mutating global config cache.
    file_path = find_config_file(str(config_path) if config_path else None, optional=True)
    if file_path is None:
        return imported, skipped_existing, skipped_removed, skipped_malformed

    data = _read_config_data(file_path)
    if not data:
        return imported, skipped_existing, skipped_removed, skipped_malformed

    sources_raw = data.get("sources", [])
    if not isinstance(sources_raw, list):
        raise ConfigError("'sources' section must be a list of sources.")

    for raw_src in sources_raw:
        if not isinstance(raw_src, dict):
            skipped_malformed.append(raw_src)
            continue
        if str(raw_src.get("provider", "")).strip().lower() != "telegram":
            continue
        _process_yaml_channels(
            raw_src.get("channels", []),
            existing_channels,
            removed_channels,
            imported,
            skipped_existing,
            skipped_removed,
            skipped_malformed,
        )

    return imported, skipped_existing, skipped_removed, skipped_malformed


def import_yaml_sources_to_overlay(
    config_path: str | Path | None = None,
    overlay_file: str | Path | None = None,
    *,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Safely and idempotently import channels from config.yaml into managed overlay.

    Preserves all existing user modifications in the Admin UI.
    Startup/import NEVER overwrites existing Admin UI values.
    """
    target_overlay = overrides_path(overlay_file)

    try:
        if dry_run:
            existing_overlay = read_overrides(target_overlay)
            revision = existing_overlay.get("revision", 0)
            overrides = copy.deepcopy(existing_overlay.get("overrides", {}))
            tg_overrides = overrides.setdefault("telegram", {})
            existing_channels = tg_overrides.setdefault("channels", {})
            removed_channels = set(tg_overrides.get("removed_channels", []))

            imported, skipped_exist, skipped_rem, skipped_mal = _extract_pure_yaml_channels(
                config_path, existing_channels, removed_channels
            )
            changes_count = len(imported)
        else:
            with config_lock(target_overlay):
                existing_overlay = read_overrides(target_overlay)
                revision = existing_overlay.get("revision", 0)
                overrides = copy.deepcopy(existing_overlay.get("overrides", {}))
                tg_overrides = overrides.setdefault("telegram", {})
                existing_channels = tg_overrides.setdefault("channels", {})
                removed_channels = set(tg_overrides.get("removed_channels", []))

                imported, skipped_exist, skipped_rem, skipped_mal = _extract_pure_yaml_channels(
                    config_path, existing_channels, removed_channels
                )
                changes_count = len(imported)

                if changes_count > 0:
                    new_payload = {
                        "revision": revision + 1,
                        "overrides": overrides,
                    }
                    validated = validate_overlay(new_payload)
                    _publish_overlay(target_overlay, validated)
                    logger.info(
                        "Idempotently imported %d channel(s) from YAML into %s (new revision: %d)",
                        changes_count,
                        target_overlay,
                        revision + 1,
                    )
    except (ConfigError, ConfigurationError, ValueError, OSError, RuntimeError) as err:
        return {
            "success": False,
            "error": f"Failed to load base configuration for import: {err}",
            "dry_run": dry_run,
        }

    return {
        "success": True,
        "dry_run": dry_run,
        "imported_channels": imported,
        "skipped_existing": skipped_exist,
        "skipped_removed": skipped_rem,
        "skipped_malformed": skipped_mal,
        "changes_count": changes_count,
        "target_overlay": str(target_overlay),
    }
