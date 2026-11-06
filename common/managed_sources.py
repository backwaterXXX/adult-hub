"""Revisioned, atomic source-configuration overrides shared by API and workers.

The checked-in/read-only YAML remains the base. This file stores only explicit
operator overrides and never contains Telegram sessions or credentials.
"""

from __future__ import annotations

import copy
import fcntl
import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from common.config_loader import ConfigError, parse_and_validate_config


MAX_OVERLAY_BYTES = 256 * 1024
SOURCE_FIELDS = {"enabled", "stream_mode", "max_pages", "sections"}
TELEGRAM_SOURCE_FIELDS = {"enabled", "defaults", "channels", "removed_channels"}
TELEGRAM_DEFAULT_FIELDS = {
    "stream_mode", "max_pages", "min_duration_seconds", "history_batch_size",
    "tags_whitelist", "tags_blacklist",
}
TELEGRAM_CHANNEL_FIELDS = {
    "enabled", "studio", "max_pages", "min_duration_seconds", "stream_mode",
    "history_batch_size", "tags_whitelist", "tags_blacklist",
}


class ManagedConfigConflict(RuntimeError):
    """The requested source revision is stale."""


class ManagedAuditPending(RuntimeError):
    """A prior source revision still needs its durable audit event recorded."""


class ManagedConfigLockTimeout(RuntimeError):
    """The source configuration lock could not be acquired within the timeout."""


DEFAULT_CONFIG_LOCK_TIMEOUT = 5.0


@contextmanager
def config_lock(
    target: Path,
    timeout: float = DEFAULT_CONFIG_LOCK_TIMEOUT,
    poll_interval: float = 0.05,
) -> Iterator[Any]:
    """Acquire an exclusive lock on the configuration target within a bounded timeout."""
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_name(target.name + ".lock")
    deadline = time.monotonic() + max(0.0, timeout)
    with lock_path.open("a+b") as lock:
        fileno = lock.fileno()
        acquired = False
        while True:
            try:
                fcntl.flock(fileno, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    break
                time.sleep(poll_interval)
        if not acquired:
            raise ManagedConfigLockTimeout(
                "Source configuration is temporarily locked by another operation. Retry shortly."
            )
        try:
            yield lock
        finally:
            try:
                fcntl.flock(fileno, fcntl.LOCK_UN)
            except OSError:
                pass


def canonical_channel_id(channel_id: str) -> str:
    clean = channel_id.strip()
    if clean.startswith("-100"):
        return clean[4:]
    return clean.lstrip("-")


def channel_aliases(channel_id: str) -> set[str]:
    clean = channel_id.strip()
    values = {channel_id, clean}
    if clean.startswith("-100"):
        values.add(clean[4:])
    else:
        values.add("-100" + clean.lstrip("-"))
    return values


def channel_lock_path(channel_id: str, path: str | Path | None = None) -> Path:
    target = overrides_path(path)
    clean = canonical_channel_id(channel_id)
    return target.with_name(f"{target.name}.channel-{clean}.lock")


def is_channel_locked(channel_id: str, path: str | Path | None = None) -> bool:
    lock_file = channel_lock_path(channel_id, path)
    if not lock_file.exists():
        return False
    try:
        with lock_file.open("a+b") as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                return False
            except (BlockingIOError, OSError):
                return True
    except OSError:
        return False


def overrides_path(path: str | Path | None = None) -> Path:
    if path is not None:
        return Path(path)
    configured = os.getenv("ADMIN_SOURCE_OVERRIDES_PATH")
    return Path(configured) if configured else Path("/data/admin-source-overrides.json")


def audit_outbox_path(path: str | Path | None = None) -> Path:
    target = overrides_path(path)
    return target.with_name(target.name + ".audit-outbox.json")


def read_overrides(path: str | Path | None = None) -> dict[str, Any]:
    target = overrides_path(path)
    if not target.exists():
        return {"revision": 0, "overrides": {}}
    try:
        if target.stat().st_size > MAX_OVERLAY_BYTES:
            raise ConfigError("Managed source settings exceed the permitted size.")
        value = json.loads(target.read_text(encoding="utf-8"))
    except ConfigError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ConfigError("Managed source settings could not be read.") from None
    return validate_overlay(value)


def validate_overlay(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"revision", "overrides"}:
        raise ConfigError("Managed source settings have an invalid structure.")
    revision = value["revision"]
    overrides = value["overrides"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ConfigError("Managed source revision must be a non-negative integer.")
    if not isinstance(overrides, dict):
        raise ConfigError("Managed source overrides must be an object.")
    return {"revision": revision, "overrides": copy.deepcopy(overrides)}


def _validate_pending_audit(value: Any) -> dict[str, Any]:
    if (not isinstance(value, dict) or set(value) != {"revision", "changes", "truncated"}
            or type(value["revision"]) is not int or value["revision"] < 1
            or not isinstance(value["changes"], list) or len(value["changes"]) > 1000
            or type(value["truncated"]) is not bool):
        raise ConfigError("Managed source audit outbox has an invalid structure.")
    for item in value["changes"]:
        if (not isinstance(item, dict) or set(item) != {"path", "before", "after"}
                or not isinstance(item["path"], list) or not 2 <= len(item["path"]) <= 4
                or any(not isinstance(part, str) or not part or len(part) > 256
                       or any(ord(char) < 32 for char in part) for part in item["path"])):
            raise ConfigError("Managed source audit outbox has an invalid structure.")
        for key in ("before", "after"):
            data = item[key]
            if (data is not None and type(data) not in (bool, int, str, list)
                    or isinstance(data, str) and len(data) > 1000
                    or isinstance(data, list) and (len(data) > 100
                        or any(not isinstance(part, str) or len(part) > 500 for part in data))):
                raise ConfigError("Managed source audit outbox has an invalid structure.")
    try:
        if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > 128 * 1024:
            raise ConfigError("Managed source audit outbox exceeds the permitted size.")
    except (TypeError, ValueError):
        raise ConfigError("Managed source audit outbox has an invalid structure.") from None
    return copy.deepcopy(value)


def read_pending_source_audit(path: str | Path | None = None) -> dict[str, Any] | None:
    target = audit_outbox_path(path)
    if not target.exists():
        return None
    try:
        if target.stat().st_size > 128 * 1024:
            raise ConfigError("Managed source audit outbox exceeds the permitted size.")
        return _validate_pending_audit(json.loads(target.read_text(encoding="utf-8")))
    except ConfigError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ConfigError("Managed source audit outbox could not be read.") from None


def _publish_overlay(target: Path, value: dict[str, Any]) -> None:
    content = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    if len(content.encode("utf-8")) > MAX_OVERLAY_BYTES:
        raise ConfigError("Managed source settings exceed the permitted size.")
    fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, target)
        directory_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def _remove_audit_outbox(target: Path) -> None:
    try:
        target.unlink()
    except FileNotFoundError:
        return
    directory_fd = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def clear_pending_source_audit(*, expected_revision: int, audit_revision: int,
                               path: str | Path | None = None,
                               lock_timeout: float = DEFAULT_CONFIG_LOCK_TIMEOUT) -> bool:
    """Clear an outbox item only after its idempotent PostgreSQL insert succeeds."""
    target = overrides_path(path)
    with config_lock(target, timeout=lock_timeout):
        current = read_overrides(target)
        pending = read_pending_source_audit(target)
        if current["revision"] != expected_revision or not pending:
            return False
        if pending["revision"] == current["revision"] == audit_revision:
            _remove_audit_outbox(audit_outbox_path(target))
            return True
        if pending["revision"] == current["revision"] + 1 == audit_revision:
            # The process wrote the outbox but crashed before publishing its overlay.
            _remove_audit_outbox(audit_outbox_path(target))
            return True
        else:
            return False


def _validate_override_fields(fields: Any, allowed: set[str], context: str) -> dict[str, Any]:
    if not isinstance(fields, dict):
        raise ConfigError(f"Overrides for {context} must be an object.")
    unknown = set(fields) - allowed
    if unknown:
        raise ConfigError(f"Unsupported managed field(s) for {context}: {sorted(unknown)}")
    if any(value is None for value in fields.values()):
        raise ConfigError(f"Use omission to inherit a base setting for {context}; null is not supported.")
    return copy.deepcopy(fields)


def _apply_telegram_channels(source: dict[str, Any], channel_overrides: Any) -> None:
    if not isinstance(channel_overrides, dict):
        raise ConfigError("Telegram channel overrides must be keyed by immutable channel ID.")
    channels = source.get("channels", [])
    by_id = {
        str(channel.get("id", "")).strip(): channel
        for channel in channels if isinstance(channel, dict)
    }
    for channel_id, channel_patch in channel_overrides.items():
        if not isinstance(channel_id, str) or not channel_id.strip() or channel_id != channel_id.strip():
            raise ConfigError("A managed Telegram channel must use a non-empty, trimmed immutable ID.")
        fields = _validate_override_fields(
            channel_patch, TELEGRAM_CHANNEL_FIELDS, f"Telegram channel '{channel_id}'"
        )
        if channel_id in by_id:
            by_id[channel_id].update(fields)
        else:
            # New channels live only in the managed overlay. Requiring
            # a studio label makes an accidentally pasted ID visible
            # and reviewable before the next sync consumes it.
            studio = fields.get("studio")
            if not isinstance(studio, str) or not studio.strip():
                raise ConfigError("A new managed Telegram channel requires a studio label.")
            channel = {"id": channel_id, **fields}
            source.setdefault("channels", []).append(channel)
            by_id[channel_id] = channel


def _apply_telegram_removed(source: dict[str, Any], removed: Any) -> None:
    if (not isinstance(removed, list) or len(removed) > 5000
            or any(not isinstance(channel_id, str) or not channel_id
                   or channel_id != channel_id.strip() or len(channel_id) > 256
                   or any(ord(char) < 32 for char in channel_id)
                   for channel_id in removed)
            or len(set(removed)) != len(removed)):
        raise ConfigError("Removed Telegram channels must be a list of unique immutable IDs.")
    removed_ids = set(removed)
    source["channels"] = [
        channel for channel in source.get("channels", [])
        if str(channel.get("id", "")).strip() not in removed_ids
    ]


def _apply_telegram_override(source: dict[str, Any], raw: Any, provider: str) -> None:
    patch = _validate_override_fields(raw, TELEGRAM_SOURCE_FIELDS, f"source '{provider}'")
    if "defaults" in patch:
        defaults = _validate_override_fields(
            patch["defaults"], TELEGRAM_DEFAULT_FIELDS, "Telegram defaults"
        )
        source["defaults"] = {**source.get("defaults", {}), **defaults}
    if "channels" in patch:
        _apply_telegram_channels(source, patch["channels"])
    if "removed_channels" in patch:
        _apply_telegram_removed(source, patch["removed_channels"])
    if "enabled" in patch:
        source["enabled"] = patch["enabled"]


def apply_overrides(base_data: Any, overlay: Any) -> dict[str, Any]:
    """Merge an already parsed base YAML object with a validated partial overlay."""
    normalized = validate_overlay(overlay)
    if not isinstance(base_data, dict):
        raise ConfigError("Base configuration must be an object before managed settings are applied.")
    result = copy.deepcopy(base_data)
    base_sources = result.get("sources", [])
    if not isinstance(base_sources, list):
        raise ConfigError("Base source configuration must be a list.")
    by_provider = {
        str(source.get("provider", "")).strip().lower(): source
        for source in base_sources if isinstance(source, dict)
    }
    for provider, raw in normalized["overrides"].items():
        if not isinstance(provider, str) or provider not in by_provider:
            if provider == "telegram":
                tg_source = {
                    "provider": "telegram",
                    "enabled": True,
                    "defaults": {
                        "stream_mode": "proxy",
                        "max_pages": 1,
                        "min_duration_seconds": 420,
                        "history_batch_size": 100,
                        "tags_whitelist": [],
                        "tags_blacklist": [],
                    },
                    "channels": [],
                }
                base_sources.append(tg_source)
                result["sources"] = base_sources
                by_provider["telegram"] = tg_source
            else:
                raise ConfigError("Managed settings may only override providers present in the base configuration.")
        source = by_provider[provider]
        if provider == "telegram":
            _apply_telegram_override(source, raw, provider)
        else:
            patch = _validate_override_fields(raw, SOURCE_FIELDS, f"source '{provider}'")
            source.update(patch)
    # Validate the complete effective configuration here, before a writer can
    # atomically publish a revision that either service cannot consume.
    parse_and_validate_config(result)
    return result


def _safe_sources(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    config = parse_and_validate_config(data)
    result: dict[str, dict[str, Any]] = {}
    for provider, source in config.sources.items():
        item: dict[str, Any] = {"enabled": source.enabled}
        if provider == "telegram":
            item["defaults"] = copy.deepcopy(source.defaults)
            item["channels"] = {
                channel.id: {
                    "enabled": channel.enabled,
                    "studio": channel.studio,
                    "max_pages": channel.max_pages,
                    "min_duration_seconds": channel.min_duration_seconds,
                    "stream_mode": channel.stream_mode,
                    "history_batch_size": channel.history_batch_size,
                    "tags_whitelist": list(channel.tags_whitelist),
                    "tags_blacklist": list(channel.tags_blacklist),
                }
                for channel in source.channels
            }
        else:
            # Addresses are deliberately excluded from every preview response.
            item.update({"stream_mode": source.stream_mode, "max_pages": source.max_pages,
                         "sections": list(source.sections)})
        result[provider] = item
    return result


def _nested_source_changes(provider: str, section: str, old_values: dict[str, Any],
                           new_values: dict[str, Any]) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    left = old_values.get(section, {})
    right = new_values.get(section, {})
    keys = sorted(set(left) | set(right))
    for key in keys:
        if section == "channels":
            subleft, subright = left.get(key, {}), right.get(key, {})
            for field in sorted(set(subleft) | set(subright)):
                old, new = subleft.get(field), subright.get(field)
                if old != new:
                    changes.append({"path": [provider, section, key, field], "before": old, "after": new})
        elif left.get(key) != right.get(key):
            changes.append({"path": [provider, section, key], "before": left.get(key), "after": right.get(key)})
    return changes


def _provider_source_changes(provider: str, before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for section in ("enabled", "defaults", "channels", "stream_mode", "max_pages", "sections"):
        old_values, new_values = before.get(provider, {}), after.get(provider, {})
        if section in ("defaults", "channels"):
            changes.extend(_nested_source_changes(provider, section, old_values, new_values))
        else:
            old, new = old_values.get(section), new_values.get(section)
            if old != new:
                changes.append({"path": [provider, section], "before": old, "after": new})
    return changes


def _collect_source_changes(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for provider in sorted(set(before) | set(after)):
        changes.extend(_provider_source_changes(provider, before, after))
    return changes


def preview_overrides(
    base_config_path: str | Path,
    overrides: dict[str, Any],
    *,
    expected_revision: int,
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Return a revision-checked, secret-free effective diff without writing."""
    import yaml

    target = overrides_path(path)
    current = read_overrides(target)
    if (isinstance(expected_revision, bool) or not isinstance(expected_revision, int)
            or expected_revision != current["revision"]):
        raise ManagedConfigConflict("Managed source settings changed; reload and retry.")
    encoded = json.dumps(overrides, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_OVERLAY_BYTES:
        raise ConfigError("Managed source settings exceed the permitted size.")
    try:
        base = yaml.safe_load(Path(base_config_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        raise ConfigError("Base source configuration could not be read.") from None
    if base is None:
        base = {}
    before = _safe_sources(apply_overrides(base, current))
    next_overlay = validate_overlay({"revision": current["revision"] + 1, "overrides": overrides})
    after = _safe_sources(apply_overrides(base, next_overlay))
    changes = _collect_source_changes(before, after)
    return {"revision": current["revision"], "next_revision": next_overlay["revision"],
            "changes": changes[:1000], "truncated": len(changes) > 1000,
            "has_override_changes": current["overrides"] != next_overlay["overrides"]}


def _wait_for_channel_unlock(ch_id: str, target: Path, lock_timeout: float) -> None:
    if is_channel_locked(ch_id, target):
        deadline = time.monotonic() + min(lock_timeout, 3.0)
        while is_channel_locked(ch_id, target) and time.monotonic() < deadline:
            time.sleep(0.05)
        if is_channel_locked(ch_id, target):
            raise ManagedConfigConflict(
                f"Channel {ch_id} is currently being deleted; retry after deletion completes."
            )


def _wait_for_deleting_channels(patch: dict[str, Any], target: Path, lock_timeout: float) -> None:
    tg_patch = patch.get("telegram")
    if isinstance(tg_patch, dict):
        channels_patch = tg_patch.get("channels")
        removed_patch = set(tg_patch.get("removed_channels", []))
        removed_patch_canon = {canonical_channel_id(cid) for cid in removed_patch}
        if isinstance(channels_patch, dict):
            for ch_id, ch_conf in channels_patch.items():
                if isinstance(ch_conf, dict) and ch_conf.get("enabled", True) is not False:
                    if canonical_channel_id(str(ch_id)) in removed_patch_canon or str(ch_id) in removed_patch:
                        continue
                    _wait_for_channel_unlock(ch_id, target, lock_timeout)


def _check_pending_audit(target: Path, current: dict[str, Any], allow_compensation: bool) -> None:
    pending = read_pending_source_audit(target)
    if pending and pending["revision"] == current["revision"]:
        if not allow_compensation:
            raise ManagedAuditPending("A prior source change is waiting for its audit record.")
    elif pending:
        if pending["revision"] == current["revision"] + 1:
            # An interrupted write left an outbox for a revision that never published.
            _remove_audit_outbox(audit_outbox_path(target))
        else:
            raise ManagedAuditPending("A source audit is waiting for manual reconciliation.")


def _audit_outbox_event(audit_event: dict[str, Any] | None, candidate_revision: int) -> dict[str, Any] | None:
    if audit_event is None:
        return None
    if (not isinstance(audit_event, dict) or set(audit_event) != {"changes", "truncated"}
            or not isinstance(audit_event["changes"], list)
            or type(audit_event["truncated"]) is not bool):
        raise ConfigError("Source audit event has an invalid structure.")
    if audit_event["changes"] or audit_event["truncated"]:
        return _validate_pending_audit({
            "revision": candidate_revision,
            "changes": copy.deepcopy(audit_event["changes"]),
            "truncated": audit_event["truncated"],
        })
    return None


def update_overrides(
    base_config_path: str | Path,
    patch: dict[str, Any],
    *,
    expected_revision: int,
    audit_event: dict[str, Any] | None = None,
    path: str | Path | None = None,
    lock_timeout: float = DEFAULT_CONFIG_LOCK_TIMEOUT,
    allow_compensation: bool = False,
) -> dict[str, Any]:
    """Replace the partial override map under a process-shared lock.

    The complete effective config is validated before any bytes are replaced.
    Omitting an override restores inheritance from the read-only base YAML.
    """
    import yaml

    target = overrides_path(path)
    # Wait for a running channel deletion before taking the config lock. The deletion
    # job holds the channel lock and then needs the config lock, so waiting while holding
    # the config lock would block it and the writer would timeout against each other.
    _wait_for_deleting_channels(patch, target, lock_timeout)
    with config_lock(target, timeout=lock_timeout):
        # Re-check without waiting: a deletion may have started during the wait above.
        _wait_for_deleting_channels(patch, target, 0.0)
        current = read_overrides(target)
        if (isinstance(expected_revision, bool) or not isinstance(expected_revision, int)
                or expected_revision != current["revision"]):
            raise ManagedConfigConflict("Managed source settings changed; reload and retry.")
        _check_pending_audit(target, current, allow_compensation)
        candidate = validate_overlay({"revision": current["revision"] + 1, "overrides": patch})
        outbox_event = _audit_outbox_event(audit_event, candidate["revision"])
        try:
            base = yaml.safe_load(Path(base_config_path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError):
            raise ConfigError("Base source configuration could not be read.") from None
        if base is None:
            base = {}
        apply_overrides(base, candidate)
        if outbox_event is not None:
            _publish_overlay(audit_outbox_path(target), outbox_event)
        elif allow_compensation:
            _remove_audit_outbox(audit_outbox_path(target))
        _publish_overlay(target, candidate)
        return candidate
