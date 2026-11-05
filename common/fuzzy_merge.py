"""Preview and execution of fuzzy performer merges."""

import hashlib
import logging
from pathlib import Path
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)


def preview_performer_merge(db_url, alias, canonical, *, _core):
    """Describe the effects of merging one alias into a canonical name, read-only.

    Mirrors ``execute_performer_merges`` matching (case-insensitive exact names)
    so the reported counts match what a queued merge would actually change.
    """
    alias_c = (alias or "").strip()
    canonical_c = (canonical or "").strip()
    result: Dict[str, Any] = {
        "alias": alias_c, "canonical": canonical_c,
        "valid_canonical": False, "safe_alias": False,
        "alias_videos": 0, "canonical_videos": 0,
        "resolved_canonical": canonical_c, "aliases_affected": 0,
        "has_photo": False, "canonical_has_photo": False,
        "has_bio": False, "canonical_has_bio": False,
        "manual_overrides": [],
        "warnings": [],
    }
    if not alias_c or not canonical_c or alias_c == canonical_c:
        result["warnings"].append("Alias and canonical must be two distinct, non-empty names.")
        return result
    result["valid_canonical"] = _core["is_valid_performer_name"](canonical_c)
    if not result["valid_canonical"]:
        result["warnings"].append(f"'{canonical_c}' is not a valid canonical performer name.")
    result["safe_alias"] = _core["is_safe_alias"](alias_c, canonical_c)
    if not result["safe_alias"]:
        result["warnings"].append("The alias is too generic; it will not be recorded for future syncs.")

    path = db_url or _core["DB_URL"] or None
    alias_key, canonical_key = alias_c.lower(), canonical_c.lower()
    conn = _core["connect"](path)
    try:
        tables = _core["table_names"](conn)
        if "videos" in tables:
            for (raw,) in conn.execute(
                    "SELECT performers FROM videos WHERE performers <> '[]'::jsonb"):
                try:
                    names = _core["_json_list"](raw)
                except (TypeError, ValueError):
                    continue
                if not isinstance(names, list):
                    continue
                lowered = {str(name).lower() for name in names if isinstance(name, str)}
                if alias_key in lowered:
                    result["alias_videos"] += 1
                if canonical_key in lowered:
                    result["canonical_videos"] += 1
        mapping: Dict[str, str] = {}
        if "performer_aliases" in tables:
            for stored_alias, stored_canonical in conn.execute("SELECT alias, canonical FROM performer_aliases"):
                mapping[(stored_alias or "").strip().lower()] = (stored_canonical or "").strip()
        existing = mapping.get(alias_key)
        if existing and existing.lower() != canonical_key:
            result["warnings"].append(
                f"'{alias_c}' is already mapped to '{existing}'; the merge follows that chain.")
        resolved, seen = canonical_c, set()
        while resolved.lower() in mapping and resolved.lower() not in seen:
            seen.add(resolved.lower())
            resolved = mapping[resolved.lower()]
        result["resolved_canonical"] = resolved
        result["aliases_affected"] = 1 if _core["is_safe_alias"](alias_c, resolved) else 0
        if "performer_photos" in tables:
            result["has_photo"] = _core["_name_exists"](conn, "performer_photos", alias_c)
            result["canonical_has_photo"] = _core["_name_exists"](conn, "performer_photos", canonical_c)
        if "performer_bios" in tables:
            result["has_bio"] = _core["_name_exists"](conn, "performer_bios", alias_c)
            result["canonical_has_bio"] = _core["_name_exists"](conn, "performer_bios", canonical_c)
        from common.performer_profiles import merge_override_names
        result["manual_overrides"] = merge_override_names(conn, alias_c, resolved)
        if result["manual_overrides"]:
            result["warnings"].append(
                "Reset manual performer profile overrides before merging these identities."
            )
        result["revision"] = _core["performer_merge_revision"](conn, alias_c, resolved)
    finally:
        conn.close()
    if result["alias_videos"] == 0:
        result["warnings"].append("No videos currently reference the alias; only metadata may change.")
    if result["has_photo"] and not result["canonical_has_photo"]:
        result["warnings"].append("The alias photo will become the canonical photo.")
    if result["has_bio"] and not result["canonical_has_bio"]:
        result["warnings"].append("The alias biography will become the canonical biography.")
    return result

def performer_merge_revision(conn, alias, canonical, *, _core, legacy=False):
    """Fingerprint the catalog, people metadata, alias map and file registry a merge may change."""
    tables = _core["table_names"](conn)
    alias_key, canonical_key = alias.strip().lower(), canonical.strip().lower()
    payload: Dict[str, Any] = {"alias": alias_key, "canonical": canonical_key}
    if "performer_profile_overrides" in tables:
        payload["profile_overrides"] = conn.execute(
            "SELECT name,fields,revision FROM performer_profile_overrides "
            "WHERE lower(name) IN (%s,%s) ORDER BY lower(name)", (alias_key, canonical_key)
        ).fetchall()
    mapping = []
    if "performer_aliases" in tables:
        mapping = conn.execute("SELECT alias,canonical FROM performer_aliases ORDER BY lower(alias),alias").fetchall()
        if legacy:
            payload["aliases"] = mapping
        else:
            # Only mappings that the merge reads or rewrites affect this pair.
            payload["aliases"] = [tuple(row) for row in mapping
                                  if str(row[0] or "").strip().lower() in {alias_key, canonical_key}
                                  or str(row[1] or "").strip().lower() == alias_key]
    impacted_ids = []
    video_rows = []
    if "videos" in tables:
        for video_id, raw in conn.execute(
                "SELECT id,performers FROM videos WHERE performers <> '[]'::jsonb ORDER BY id"):
            try:
                performers = _core["_json_list"](raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(performers, list):
                continue
            names = {str(name).strip().lower() for name in performers if isinstance(name, str)}
            if alias_key in names or canonical_key in names:
                impacted_ids.append(str(video_id))
                video_rows.append((str(video_id), performers))
    payload["videos"] = video_rows
    for table, columns in (("performer_photos", "name,thumb,updated_at"),
                           ("performer_bios", "name,bio,birth,aliases,updated_at"),
                           ("jellyfin_person_sync", "name,pbc_updated_at,format_version")):
        if table not in tables:
            continue
        records = conn.execute(f"SELECT {columns} FROM {table} ORDER BY lower(name),name").fetchall()
        payload[table] = [tuple(row) for row in records
                          if str(row[0] or "").strip().lower() in {alias_key, canonical_key}]
    if "synced_files" in tables and impacted_ids:
        payload["files"] = conn.execute(
            "SELECT file_path,video_id FROM synced_files WHERE video_id=ANY(%s) ORDER BY video_id,file_path",
            (impacted_ids,),
        ).fetchall()
    canonical = _core["json"].dumps(payload, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return digest if legacy else "v2:" + digest

def _name_exists(conn, table, name, *, _core):
    return conn.execute(
        f"SELECT 1 FROM {table} WHERE lower(name)=lower(%s) LIMIT 1", (name,)).fetchone() is not None


def _merge_alias_values(existing, incoming, *, _core):
    """Union two ``performer_bios.aliases`` values, canonical entries first."""
    merged: List[str] = []
    seen = set()
    for value in (existing, incoming):
        for name in _core["_json_list"](value):
            if not isinstance(name, str):
                continue
            key = name.strip().casefold()
            if key and key not in seen:
                seen.add(key)
                merged.append(name)
    return _core["json"].dumps(merged)

def execute_performer_merges(db_url, media_dir, merges, skip_backup=False, *, _core):
    """Merge alias and canonical pairs in the database and NFO files."""
    media_dir = Path(media_dir)

    # Clean and deduplicate input pairs
    clean_merges: Dict[str, str] = {}
    for alias, canonical in merges:
        alias_c = (alias or "").strip()
        canon_c = (canonical or "").strip()
        if alias_c and canon_c and alias_c != canon_c and _core["is_valid_performer_name"](canon_c):
            clean_merges[alias_c] = canon_c

    # Resolve merge chains (A -> B and B -> C ==> A -> C)
    resolved_merges: Dict[str, str] = {}
    for alias, canon in clean_merges.items():
        curr = canon
        visited = {alias}
        while curr in clean_merges and curr not in visited:
            visited.add(curr)
            curr = clean_merges[curr]
        resolved_merges[alias] = curr

    if not resolved_merges:
        return {"ok": True, "message": "No changes to apply.", "videos_updated": 0}

    backup_path = None
    if not skip_backup:
        try:
            backup_path = str(_core["make_backup"](db_url))
            _core["logger"].info("Database backup created: %s", backup_path)
        except Exception as e:
            return {"ok": False, "error": f"Could not back up database: {e}"}

    conn = _core["connect"](db_url or _core["DB_URL"] or None)
    try:
        tables = _core["table_names"](conn)

        # Profile ownership and reset snapshots are name-bound. Do not silently
        # discard or retarget a hand-edited profile during identity merging.
        if "performer_profile_overrides" in tables:
            from common.performer_profiles import merge_override_names
            for alias, canon in resolved_merges.items():
                protected = merge_override_names(conn, alias, canon)
                if protected:
                    raise ValueError(
                        "Reset manual performer profile overrides before merging: "
                        + ", ".join(protected[:4])
                    )

        # Steps 1-5 share one transaction: a crash must not leave a half-merged
        # identity with videos renamed but bios or aliases still on the old name.
        # NFO files are rewritten only after this commits.
        with conn:
            # 1. Update videos.performers
            video_updates: List[Tuple[str, List[str]]] = []
            if "videos" in tables:
                resolved_merges_lower = {k.lower(): v for k, v in resolved_merges.items()}
                rows = conn.execute(
                    "SELECT id, performers FROM videos WHERE performers <> '[]'::jsonb"
                ).fetchall()
                for vid_id, raw_json in rows:
                    try:
                        arr = _core["_json_list"](raw_json)
                        if not arr:
                            continue
                    except Exception:
                        logger.debug("Ignored exception in execute_performer_merges", exc_info=True)
                        continue

                    changed = False
                    new_list = []
                    seen = set()
                    for p in arr:
                        target = resolved_merges.get(p) or resolved_merges_lower.get(str(p).lower(), p)
                        if target and target not in seen:
                            seen.add(target)
                            new_list.append(target)
                        if target != p or len(new_list) != len(arr):
                            changed = True

                    if changed and new_list != arr:
                        video_updates.append((vid_id, new_list))

                for vid_id, new_list in video_updates:
                    conn.execute(
                        "UPDATE videos SET performers = %s WHERE id = %s",
                        (_core["json"].dumps(new_list), vid_id),
                    )

            # 2. Migrate performer_photos
            photos_migrated = 0
            if "performer_photos" in tables:
                photo_rows = dict(
                    conn.execute("SELECT name, thumb FROM performer_photos").fetchall()
                )
                photo_rows_lower = {k.lower(): (k, v) for k, v in photo_rows.items()}
                now_ts = int(_core["time"].time())
                for alias, canon in resolved_merges.items():
                    if alias.lower() in photo_rows_lower:
                        orig_alias, alias_thumb = photo_rows_lower[alias.lower()]
                        canon_thumb = photo_rows.get(canon, "")
                        if alias_thumb and not canon_thumb:
                            conn.execute(
                                "INSERT INTO performer_photos (name, thumb, updated_at) VALUES (%s, %s, %s) "
                                "ON CONFLICT (name) DO UPDATE SET thumb=EXCLUDED.thumb, updated_at=EXCLUDED.updated_at",
                                (canon, alias_thumb, now_ts),
                            )
                            photos_migrated += 1
                        conn.execute(
                            "DELETE FROM performer_photos WHERE lower(name) = lower(%s)", (alias,)
                        )

            # 3. Migrate performer_bios
            bios_migrated = 0
            if "performer_bios" in tables:
                bio_rows = {
                    r[0]: r[1:]
                    for r in conn.execute(
                        "SELECT name, bio, birth, aliases, updated_at FROM performer_bios"
                    ).fetchall()
                }
                bio_rows_lower = {k.lower(): (k, v) for k, v in bio_rows.items()}
                for alias, canon in resolved_merges.items():
                    if alias.lower() in bio_rows_lower:
                        orig_alias, (raw_bio, raw_birth, raw_aliases, raw_updated_at) = bio_rows_lower[alias.lower()]
                        if canon not in bio_rows:
                            conn.execute(
                                "INSERT INTO performer_bios (name, bio, birth, aliases, updated_at) VALUES (%s, %s, %s, %s, %s) "
                                "ON CONFLICT (name) DO UPDATE SET bio=EXCLUDED.bio, birth=EXCLUDED.birth, "
                                "aliases=EXCLUDED.aliases, updated_at=EXCLUDED.updated_at",
                                (canon, raw_bio, raw_birth, raw_aliases, raw_updated_at),
                            )
                            bios_migrated += 1
                        else:
                            base_bio, base_birth, base_aliases, _ = bio_rows[canon]
                            if (not base_bio and raw_bio) or (not base_birth and raw_birth):
                                # Fill the gaps but keep the canonical profile's
                                # own aliases; the merged name joins them.
                                conn.execute(
                                    "INSERT INTO performer_bios (name, bio, birth, aliases, updated_at) VALUES (%s, %s, %s, %s, %s) "
                                    "ON CONFLICT (name) DO UPDATE SET bio=EXCLUDED.bio, birth=EXCLUDED.birth, "
                                    "aliases=EXCLUDED.aliases, updated_at=EXCLUDED.updated_at",
                                    (
                                        canon,
                                        base_bio or raw_bio,
                                        base_birth or raw_birth,
                                        _merge_alias_values(base_aliases, raw_aliases, _core=_core),
                                        raw_updated_at,
                                    ),
                                )
                                bios_migrated += 1
                        conn.execute("DELETE FROM performer_bios WHERE lower(name) = lower(%s)", (alias,))

            # 4. Add entries to performer_aliases
            aliases_added = 0
            if "performer_aliases" in tables:
                for alias, canon in resolved_merges.items():
                    if not _core["is_safe_alias"](alias, canon):
                        continue
                    conn.execute("DELETE FROM performer_aliases WHERE lower(alias) = lower(%s)", (canon,))
                    conn.execute("UPDATE performer_aliases SET canonical = %s WHERE lower(canonical) = lower(%s)", (canon, alias))
                    conn.execute(
                        "INSERT INTO performer_aliases (alias, canonical) VALUES (%s, %s) "
                        "ON CONFLICT(alias) DO UPDATE SET canonical = excluded.canonical",
                        (alias.lower(), canon),
                    )
                    aliases_added += 1

            # 5. Delete obsolete entries from jellyfin_person_sync
            if "jellyfin_person_sync" in tables:
                for alias in resolved_merges.keys():
                    conn.execute(
                        "DELETE FROM jellyfin_person_sync WHERE lower(name) = lower(%s)",
                        (alias,),
                    )

        # 6. Update NFO files
        nfos_updated = 0
        import sync as S
        # Rebuild all files containing a canonical name from this operation,
        # including rows already committed by an earlier attempt. This makes
        # replay after a process stop between DB commit and NFO writes repairable.
        canonical_names = set(resolved_merges.values())
        nfo_targets = {}
        for vid_id, raw_perfs in conn.execute(
                "SELECT id, performers FROM videos WHERE performers <> '[]'::jsonb"):
            try:
                performers = _core["_json_list"](raw_perfs)
            except (TypeError, ValueError):
                continue
            if (isinstance(performers, list)
                    and canonical_names.intersection(p for p in performers if isinstance(p, str))):
                nfo_targets[vid_id] = performers

        nfo_errors = []
        if nfo_targets:
            from common.video_metadata import load_metadata_states

            target_items = list(nfo_targets.items())
            for offset in range(0, len(target_items), 128):
                batch = target_items[offset:offset + 128]
                batch_ids = [vid_id for vid_id, _ in batch]
                metadata_states = load_metadata_states(conn, batch_ids)
                batch_performer_names = {
                    performer
                    for _, names in batch
                    for performer in names
                    if isinstance(performer, str) and performer.strip()
                }
                performer_thumbs = S.load_performer_thumbs(conn, batch_performer_names)

                for vid_id, new_perfs in batch:
                    state = metadata_states.get(str(vid_id))
                    if state is None:
                        nfo_errors.append(f"{vid_id}: video disappeared before NFO regeneration")
                        continue
                    source = state["source"]
                    metadata = state["metadata"]
                    effective_perfs = metadata.get("performers")
                    if not isinstance(effective_perfs, list):
                        effective_perfs = new_perfs

                    try:
                        tag_set = set(_core["_json_list"](source.get("tags")))
                    except Exception:
                        tag_set = set()
                    try:
                        genres = set(_core["_json_list"](source.get("categories")))
                    except Exception:
                        genres = set()

                    wanted_performers = {
                        performer.strip().casefold()
                        for performer in effective_perfs
                        if isinstance(performer, str) and performer.strip()
                    }
                    video_dict = {
                        "id": vid_id,
                        "provider": source.get("provider"),
                        "provider_video_id": str(vid_id).split(":", 1)[-1],
                        "title": metadata.get("title"),
                        "plot": metadata.get("plot"),
                        "date_released": metadata.get("date_released"),
                        "tags": metadata.get("tags"),
                        "metadata_fields": metadata.get("metadata_fields"),
                        "slug": source.get("slug") or "",
                        "duration": source.get("duration") or 0,
                        "thumbnail": source.get("thumbnail") or "",
                        "performers": effective_perfs,
                        "performer_thumbs": {
                            name: thumb for name, thumb in performer_thumbs.items()
                            if name.strip().casefold() in wanted_performers
                        },
                        "studio": metadata.get("studio") or "",
                        "dateadded": S.format_dateadded(
                            source.get("first_seen") or _core["time"].time()
                        ),
                    }
                    nfo_path = media_dir / f"{S.stable_media_basename(vid_id)}.nfo"
                    try:
                        S.create_nfo_file(video_dict, genres, tag_set, nfo_path, conn=conn)
                        nfos_updated += 1
                    except Exception as e:
                        nfo_errors.append(f"{nfo_path}: {type(e).__name__}: {e}")
        if nfo_errors:
            raise RuntimeError("NFO regeneration incomplete: " + "; ".join(nfo_errors[:5]))

        # 7. Refresh Jellyfin
        jf_ok = _core["trigger_jellyfin_refresh"]()

        return {
            "ok": True,
            "backup": backup_path,
            "videos_updated": len(video_updates),
            "photos_migrated": photos_migrated,
            "bios_migrated": bios_migrated,
            "aliases_added": aliases_added,
            "nfos_updated": nfos_updated,
            "jellyfin_refreshed": jf_ok,
            "message": (
                f"Merged {len(resolved_merges)} performers "
                f"({len(video_updates)} videos updated, {nfos_updated} NFO files)."
            ),
        }

    finally:
        conn.close()

