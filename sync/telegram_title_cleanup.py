"""Resumable cleanup of Telegram titles and caption hashtags."""

from __future__ import annotations

import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from xml.dom import minidom

from common import db
from common.title_cleaning import clean_title_text, extract_title_hashtags


TASK_ID = "telegram-title-cleanup"
TASK_VERSION = 1
TASK_BATCH_SIZE = 500
STATUS_PATH = Path(os.getenv(
    "TELEGRAM_TITLE_CLEANUP_STATUS_PATH", "/data/cache/telegram-title-cleanup.json"
))
_CATALOG_SOURCES = ("stashdb", "theporndb", "tpdb")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_state(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True) + "\n",
                         encoding="utf-8")
    os.replace(temporary, path)


def _json_object(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}


def _text_list(value) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            value = []
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _merge_values(existing, additions) -> tuple[list[str], int]:
    merged = _text_list(existing)
    seen = {value.casefold() for value in merged}
    added = 0
    for value in additions:
        item = str(value).strip()
        key = item.casefold()
        if item and key not in seen:
            merged.append(item)
            seen.add(key)
            added += 1
    return merged, added


def _fallback_title(source_filename: str, video_id: str) -> str:
    if source_filename:
        stem = Path(source_filename).name.rsplit(".", 1)[0]
        fallback = re.sub(r"[_\-.]+", " ", stem)
        title = clean_title_text(fallback, max_length=120)
        if title:
            return title
    suffix = str(video_id or "Telegram").rsplit(":", 1)[-1]
    return clean_title_text(f"Telegram {suffix}", max_length=120)


def _display_title(title: str, performers: list[str], slug: str) -> str:
    try:
        if __package__:
            from .display_titles import clean_display_title
        else:
            from display_titles import clean_display_title
        return clean_display_title(title, performers=performers,
                                   provider="telegram", slug=slug) or title
    except Exception:
        return title


def _patch_nfo(path: Path, *, title: str | None, genres: list[str],
               tags: list[str]) -> bool:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return False

    changed = False
    if title is not None:
        element = root.find("title")
        if element is None:
            element = ET.SubElement(root, "title")
        if (element.text or "").strip() != title:
            element.text = title
            changed = True

    for element_name, values in (("genre", genres), ("tag", tags)):
        existing = {
            str(element.text or "").strip().casefold()
            for element in root.findall(element_name) if str(element.text or "").strip()
        }
        for value in values:
            if value.casefold() in existing:
                continue
            ET.SubElement(root, element_name).text = value
            existing.add(value.casefold())
            changed = True

    if not changed:
        return False
    pretty = minidom.parseString(ET.tostring(root, encoding="utf-8")).toprettyxml(
        indent="  ", encoding="utf-8")
    clean_lines = [line for line in pretty.decode("utf-8").splitlines() if line.strip()]
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text("\n".join(clean_lines) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return True


def cleanup_telegram_titles(conn: db.Connection, media_dir: Path, *,
                            batch_size: int = TASK_BATCH_SIZE, status_path: Path = STATUS_PATH,
                            stop_requested=lambda: False, logger=None) -> dict:
    """Clean one bounded batch and persist a keyset cursor for the next sync."""
    state = _read_state(status_path)
    try:
        if __package__:
            from .category_mapping import map_category
        else:
            from category_mapping import map_category
        if __package__:
            from .media_files import resolve_media_path
        else:
            from media_files import resolve_media_path

        if int(state.get("version") or 0) != TASK_VERSION:
            state = {
                "task_id": TASK_ID, "version": TASK_VERSION, "cursor": "",
                "processed": 0, "total": None, "titles_changed": 0,
                "tags_added": 0, "categories_added": 0, "nfos_missing": 0,
                "nfos_failed": 0, "status": "pending", "last_result": None,
            }
        if state.get("status") == "complete":
            return state

        started_at = _now()
        state.update({"task_id": TASK_ID, "version": TASK_VERSION,
                      "status": "running", "last_run_at": started_at,
                      "updated_at": started_at, "last_error": ""})
        _write_state(status_path, state)

        if state.get("total") is None:
            state["total"] = int(conn.execute(
                "SELECT count(*) FROM videos WHERE provider='telegram'"
            ).fetchone()[0])

        cursor = str(state.get("cursor") or "")
        rows = conn.execute("""
            SELECT v.id, v.title, v.slug, v.source_text, v.source_filename, v.performers,
                   v.categories, v.tags, v.scene_match_source, o.fields, sf.file_path
            FROM videos v
            LEFT JOIN video_metadata_overrides o ON o.video_id=v.id
            LEFT JOIN LATERAL (
                SELECT file_path FROM synced_files WHERE video_id=v.id
                ORDER BY created_at DESC, file_path LIMIT 1
            ) sf ON TRUE
            WHERE v.provider='telegram' AND v.id > %s
            ORDER BY v.id LIMIT %s
        """, (cursor, max(1, min(int(batch_size), TASK_BATCH_SIZE)) + 1)).fetchall()

        has_more = len(rows) > max(1, min(int(batch_size), TASK_BATCH_SIZE))
        batch = rows[:max(1, min(int(batch_size), TASK_BATCH_SIZE))]
        local_titles = local_tags = local_categories = local_missing = local_nfo_failed = 0
        processed = 0
        completed_cursor = cursor
        base_processed = int(state.get("processed") or 0)
        base_titles = int(state.get("titles_changed") or 0)
        base_tags = int(state.get("tags_added") or 0)
        base_categories = int(state.get("categories_added") or 0)
        base_missing = int(state.get("nfos_missing") or 0)
        base_nfo_failed = int(state.get("nfos_failed") or 0)
        interrupted = False

        for row in batch:
            if stop_requested():
                interrupted = True
                break
            (video_id, raw_title, slug, source_text, source_filename, performers_raw,
             categories_raw, tags_raw, scene_source, overrides_raw, file_path) = row
            video_id = str(video_id)
            overrides = _json_object(overrides_raw)
            performers = _text_list(performers_raw)
            source_text = str(source_text or "")
            hashtags = extract_title_hashtags(
                source_text + "\n" + str(raw_title or "") + "\n" + str(source_filename or "")
            )

            cleaned_title = clean_title_text(raw_title, max_length=120)
            if not cleaned_title:
                cleaned_title = _fallback_title(str(source_filename or ""), video_id)
            is_catalog_title = (
                bool(str(scene_source or "").strip())
                and any(source in str(scene_source).casefold() for source in _CATALOG_SOURCES)
            )
            can_change_title = "title" not in overrides and not is_catalog_title
            title_changed = can_change_title and cleaned_title != str(raw_title or "")

            mapped = {value for value in (map_category("#" + name) for name in hashtags) if value}
            categories, category_count = _merge_values(categories_raw, sorted(mapped))
            tags = _text_list(tags_raw)
            if "tags" in overrides:
                new_tags, tag_count = tags, 0
            else:
                new_tags, tag_count = _merge_values(tags, ["#" + name for name in hashtags])

            if title_changed or categories != _text_list(categories_raw) or new_tags != tags:
                with conn:
                    conn.execute(
                        "UPDATE videos SET title=%s,categories=%s::jsonb,tags=%s::jsonb WHERE id=%s",
                        (cleaned_title if can_change_title else raw_title,
                         json.dumps(categories, ensure_ascii=False),
                         json.dumps(new_tags, ensure_ascii=False), video_id),
                    )
                    for category in mapped:
                        conn.execute(
                            "INSERT INTO video_categories(video_id,category) VALUES (%s,%s) "
                            "ON CONFLICT DO NOTHING", (video_id, category),
                        )

            nfo_title = (_display_title(cleaned_title, performers, str(slug or ""))
                         if title_changed else None)
            nfo_genres = sorted(mapped)
            nfo_tags = [] if "tags" in overrides else ["#" + name for name in hashtags]
            nfo_changed = False
            if file_path and (nfo_title is not None or nfo_genres or nfo_tags):
                try:
                    stream_path = resolve_media_path(str(file_path), Path(media_dir))
                    nfo_path = stream_path.with_suffix(".nfo")
                    if nfo_path.is_file():
                        nfo_changed = _patch_nfo(
                            nfo_path, title=nfo_title, genres=nfo_genres, tags=nfo_tags,
                        )
                        if not nfo_changed and (nfo_title is not None or nfo_genres or nfo_tags):
                            # Existing NFO may already contain these values; malformed files are
                            # counted below by checking their XML independently.
                            try:
                                ET.parse(nfo_path)
                            except (OSError, ET.ParseError):
                                local_nfo_failed += 1
                    else:
                        local_missing += 1
                except (OSError, ValueError, ET.ParseError):
                    local_nfo_failed += 1

            local_titles += int(title_changed)
            local_tags += tag_count
            local_categories += category_count
            processed += 1
            completed_cursor = video_id
            state.update({
                "cursor": completed_cursor,
                "processed": base_processed + processed,
                "titles_changed": base_titles + local_titles,
                "tags_added": base_tags + local_tags,
                "categories_added": base_categories + local_categories,
                "nfos_missing": base_missing + local_missing,
                "nfos_failed": base_nfo_failed + local_nfo_failed,
                "updated_at": _now(),
            })
            _write_state(status_path, state)

        state["cursor"] = completed_cursor
        done = (not has_more or processed < len(batch)) and not interrupted
        state["status"] = "complete" if done else "pending"
        state["last_result"] = "done" if done else "processing"
        state["completed_at"] = _now() if done else state.get("completed_at")
        state["updated_at"] = _now()
        state["last_error"] = ""
        _write_state(status_path, state)
        if logger:
            logger.info(
                "Telegram title cleanup: %s/%s scanned, %s titles, %s tags, %s categories updated.",
                state["processed"], state["total"], local_titles, local_tags, local_categories,
            )
        return state
    except Exception as exc:
        state = _read_state(status_path)
        state.update({"task_id": TASK_ID, "version": TASK_VERSION, "status": "failed",
                      "last_result": "failed", "last_error": type(exc).__name__,
                      "updated_at": _now()})
        _write_state(status_path, state)
        raise
