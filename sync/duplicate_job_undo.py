"""Undo and restore steps for durable duplicate decision jobs.

Moved verbatim from duplicate_job_worker.py. Names are read through the
duplicate_jobs compatibility facade (``_core``), as they were before the move.
"""
from __future__ import annotations
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

from sync import duplicate_jobs as _core  # noqa: E402


def _restore_list(value):
    if isinstance(value, list):
        return value
    try:
        parsed = _core.json.loads(value or '[]')
        return parsed if isinstance(parsed, list) else []
    except (TypeError, ValueError):
        return []


def _check_undo_move_paths(snapshot: dict, media_root, quarantine_base) -> None:
    for move in snapshot.get('files', []):
        try:
            source = _core.Path(move['source'])
            quarantined = _core.Path(move['quarantine'])
            if not source.is_absolute() or not quarantined.is_absolute():
                raise ValueError
            source.resolve(strict=False).relative_to(media_root)
            quarantined.resolve(strict=False).relative_to(quarantine_base)
        except (KeyError, OSError, ValueError, TypeError):
            raise ValueError('Unsafe restore path detected; manual review is required.') from None


def _check_undo_collisions(conn, snapshot: dict) -> None:
    for saved_file in snapshot['synced_files']:
        collision = conn.execute('SELECT video_id FROM synced_files WHERE file_path=%s', (saved_file['file_path'],)).fetchone()
        if collision:
            raise ValueError('File path is currently used by another entry.')


def _is_valid_fallback_source_url(surl: str | None, video_id: str) -> bool:
    surl_str = str(surl or '').rstrip('/')
    if not surl_str:
        return True
    l_channel = video_id.split(':')[1] if ':' in video_id and len(video_id.split(':')) >= 2 else ''
    channel_num = (l_channel[4:] if l_channel.startswith('-100') else l_channel.lstrip('-')) if l_channel else ''
    default_url = f"https://t.me/c/{channel_num}" if channel_num else ''
    if default_url:
        return surl_str == default_url or surl_str.startswith(f"{default_url}/")
    return surl_str.startswith("https://t.me/")


def _is_synthetic_fallback_source(row, video_id: str, k_id: str | None) -> bool:
    if row is None:
        return False
    if isinstance(row, dict):
        v_id, s_id = row.get('video_id'), row.get('source_id')
        prov, pref = row.get('provider'), row.get('provider_ref')
        surl, cid = row.get('source_url'), row.get('channel_id')
        prio, act = row.get('priority'), row.get('is_active')
    elif len(row) >= 8:
        v_id, s_id, prov, pref, surl, cid, prio, act = row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7]
    else:
        v_id, prov, pref, surl, cid, prio, act = row[0], row[1], row[2], row[3], row[4], row[5], row[6]
        s_id = None

    if str(v_id) != str(k_id) or (s_id is not None and str(s_id) != str(video_id)):
        return False
    if str(prov) != 'telegram':
        return False
    if not pref or not (str(pref) == str(video_id) or str(video_id).endswith(str(pref))):
        return False
    if int(prio or 0) < 2 or int(act or 0) != 1:
        return False
    l_channel = video_id.split(':')[1] if ':' in video_id and len(video_id.split(':')) >= 2 else ''
    cid_str = str(cid or '')
    if l_channel and cid_str and cid_str != l_channel:
        return False
    return _is_valid_fallback_source_url(surl, video_id)


def _is_exact_primary_source(row, video_id: str) -> bool:
    if row is None:
        return False
    if isinstance(row, dict):
        v_id, s_id = row.get('video_id'), row.get('source_id')
        prov, pref = row.get('provider'), row.get('provider_ref')
    elif len(row) >= 4:
        v_id, s_id, prov, pref = row[0], row[1], row[2], row[3]
    else:
        return False

    if str(v_id) != str(video_id) or str(s_id) != str(video_id):
        return False
    if str(prov) != 'telegram' or not pref:
        return False
    return str(pref) == str(video_id) or str(video_id).endswith(str(pref))


def _check_legacy_undo_sources(conn, snapshot: dict, video_id: str, k_id: str | None) -> None:
    existing_sources = conn.execute(
        'SELECT video_id, source_id, provider, provider_ref, source_url, channel_id, priority, is_active '
        'FROM video_sources WHERE video_id=%s OR source_id=%s',
        (video_id, video_id),
    ).fetchall()
    is_quarantine = (k_id == video_id) or (
        not snapshot.get('keeper_before')
        and not snapshot.get('keeper_after')
        and not snapshot.get('_decision_keeper_id')
    )
    if not is_quarantine and not existing_sources:
        raise ValueError(
            f"Cannot safely reconstruct source inventory for '{video_id}' from legacy snapshot; "
            "undo cannot proceed."
        )
    for s_row in existing_sources:
        s_vid = s_row[0] if not isinstance(s_row, dict) else s_row.get('video_id')
        if is_quarantine:
            if str(s_vid) != str(video_id):
                raise ValueError(
                    f"Source row for '{video_id}' is now owned by another video ('{s_vid}'); "
                    "undo cannot proceed."
                )
        else:
            if str(s_vid) not in (str(k_id), str(video_id)):
                raise ValueError(
                    f"Source row for '{video_id}' is now owned by another video ('{s_vid}'); "
                    "undo cannot proceed."
                )
            if not _is_exact_primary_source(s_row, video_id) and not _is_synthetic_fallback_source(s_row, video_id, k_id):
                raise ValueError(
                    f"Cannot safely reconstruct source inventory for '{video_id}' from legacy snapshot; "
                    "undo cannot proceed."
                )


def _check_undo_sources(conn, snapshot: dict, video_id: str, k_id: str | None) -> None:
    if 'sources' in snapshot and snapshot.get('sources') is not None:
        for record in snapshot.get('sources') or []:
            source_id = record.get('source_id')
            if not source_id:
                continue
            row = conn.execute('SELECT video_id FROM video_sources WHERE source_id=%s', (source_id,)).fetchone()
            if row and row[0] not in (k_id, video_id):
                raise ValueError(
                    f"Source row '{source_id}' is now owned by another video ('{row[0]}'); "
                    "undo cannot proceed."
                )
        return

    _check_legacy_undo_sources(conn, snapshot, video_id, k_id)


def _check_undo_snapshots(conn, snapshots: list, media_dir, quarantine_root, resuming_undo: bool) -> None:
    for snapshot in snapshots:
        video_id = snapshot['video']['id']
        if conn.execute('SELECT 1 FROM videos WHERE id=%s', (video_id,)).fetchone():
            raise ValueError('Item was added again; undo requires manual review.')
        media_root = media_dir.resolve()
        quarantine_base = quarantine_root.resolve()
        _check_undo_move_paths(snapshot, media_root, quarantine_base)
        _check_undo_collisions(conn, snapshot)
        if not resuming_undo and any((_core.Path(move['source']).exists() for move in snapshot.get('files', []))):
            raise ValueError('Target path exists; not overwritten.')
        k_id = (
            snapshot.get('_decision_keeper_id')
            or snapshot.get('keeper_before', {}).get('video', {}).get('id')
            or snapshot.get('keeper_after', {}).get('video', {}).get('id')
        )
        _check_undo_sources(conn, snapshot, video_id, k_id)


def _restore_undo_files(conn, job_id, snapshots: list, services, run, resuming_undo: bool) -> None:
    file_total = sum((len(snapshot.get('files', [])) for snapshot in snapshots))
    restored_count = 0
    _core._record_job_progress(conn, job_id, 'restoring_files', completed=0, total=file_total)
    for snapshot in snapshots:
        for move in snapshot.get('files', []):
            src, dst = (_core.Path(move['quarantine']), _core.Path(move['source']))
            if src.exists():
                if dst.exists():
                    if not services.duplicate_file_matches(src, dst):
                        raise FileExistsError('Target path has different contents; not overwritten.')
                    if not resuming_undo:
                        raise FileExistsError('Target path already exists.')
                    src.unlink()
                else:
                    services.duplicate_move_file(src, dst)
                run.moved.append((src, dst))
            elif _core.Path(move['source']).suffix == '.strm' and (not dst.exists()):
                raise FileNotFoundError('Missing STRM file in quarantine.')
            elif dst.exists():
                run.moved.append((src, dst))
            restored_count += 1
            _core._record_job_progress(conn, job_id, 'restoring_files', completed=restored_count, total=file_total)


def _restore_fallback_source(conn, video_id: str, k_id: str | None) -> None:
    if not k_id:
        return
    row = conn.execute(
        'SELECT video_id, source_id, provider, provider_ref, source_url, channel_id, priority, is_active '
        'FROM video_sources WHERE source_id=%s',
        (video_id,),
    ).fetchone()
    if not row:
        raise RuntimeError(
            f"Cannot safely restore source row for '{video_id}' from legacy snapshot: "
            "matched fallback source is missing or invalid."
        )
    v_id = row[0] if not isinstance(row, dict) else row.get('video_id')
    if str(v_id) not in (str(k_id), str(video_id)):
        raise RuntimeError(
            f"Source row '{video_id}' was not restored (possibly reassigned to another video in a race)."
        )
    if _is_exact_primary_source(row, video_id):
        return
    if not _is_synthetic_fallback_source(row, video_id, k_id):
        raise RuntimeError(
            f"Cannot safely restore source row for '{video_id}' from legacy snapshot: "
            "matched fallback source is missing or invalid."
        )
    cur = conn.execute(
        'UPDATE video_sources SET video_id=%s, priority=1 WHERE source_id=%s AND video_id=%s',
        (video_id, video_id, k_id),
    )
    if cur.rowcount == 0:
        raise RuntimeError(
            f"Source row '{video_id}' was not restored (possibly reassigned to another video in a race)."
        )


def _restore_single_source_record(conn, record: dict, video_id: str, k_id: str | None, valid_cols: set) -> None:
    source_id = record.get('source_id')
    s_rec = {k: v for k, v in record.items() if k in valid_cols and k != 'id'}
    s_rec['video_id'] = video_id
    s_rec['source_id'] = source_id
    if 'provider' not in s_rec:
        s_rec['provider'] = source_id.split(':', 1)[0] if ':' in source_id else 'telegram'
    created_at = s_rec.get('created_at')
    if created_at is None:
        s_rec['created_at'] = _core.time.time()
    elif isinstance(created_at, (int, float)):
        s_rec['created_at'] = created_at
    elif hasattr(created_at, 'timestamp'):
        s_rec['created_at'] = created_at.timestamp()
    elif isinstance(created_at, str):
        try:
            s_rec['created_at'] = float(created_at)
        except ValueError:
            try:
                s_rec['created_at'] = datetime.fromisoformat(created_at).timestamp()
            except ValueError:
                s_rec['created_at'] = _core.time.time()
    s_names = [c for c in s_rec if c in valid_cols]
    s_updates = [name for name in s_names if name != 'source_id']
    if s_updates:
        s_assignments = ', '.join(f'{name}=EXCLUDED.{name}' for name in s_updates)
        where_clause = f" WHERE video_sources.video_id IN ({','.join(['%s'] * (2 if k_id else 1))})"
        where_params = [k_id, video_id] if k_id else [video_id]
        cur = conn.execute(
            f"INSERT INTO video_sources ({','.join(s_names)}) "
            f"VALUES ({','.join('%s' for _ in s_names)}) "
            f"ON CONFLICT (source_id) DO UPDATE SET {s_assignments}{where_clause}",
            [*[s_rec[n] for n in s_names], *where_params],
        )
    else:
        cur = conn.execute(
            f"INSERT INTO video_sources ({','.join(s_names)}) "
            f"VALUES ({','.join('%s' for _ in s_names)}) "
            f"ON CONFLICT (source_id) DO NOTHING",
            [s_rec[n] for n in s_names],
        )
    if cur.rowcount == 0:
        raise RuntimeError(
            f"Source row '{source_id}' was not restored (possibly reassigned to another video in a race)."
        )


def _restore_sources_snapshot(conn, video_id: str, snapshot: dict, k_id: str | None) -> None:
    sources = snapshot.get('sources')
    is_quarantine = (k_id == video_id) or (not snapshot.get('keeper_before') and not snapshot.get('keeper_after') and not snapshot.get('_decision_keeper_id'))
    if sources is None:
        if not is_quarantine:
            _restore_fallback_source(conn, video_id, k_id)
        return

    restored_ids = set()
    valid_cols = set(_core.db.table_columns(conn, 'video_sources'))
    for record in sources:
        source_id = record.get('source_id')
        if not source_id:
            continue
        restored_ids.add(source_id)
        _restore_single_source_record(conn, record, video_id, k_id, valid_cols)

    if video_id not in restored_ids:
        if k_id and not is_quarantine:
            conn.execute('DELETE FROM video_sources WHERE source_id=%s AND video_id=%s', (video_id, k_id))


def _restore_snapshot_rows(conn, snapshot: dict) -> None:
    video = snapshot['video']
    names = list(video)
    updates = [name for name in names if name != 'id']
    assignments = ', '.join((f'{name}=EXCLUDED.{name}' for name in updates))
    conn.execute(f"INSERT INTO videos ({','.join(names)}) VALUES ({','.join(('%s' for _ in names))}) ON CONFLICT (id) DO UPDATE SET {assignments}", [video[n] for n in names])
    for table, records in (('synced_files', snapshot['synced_files']), ('video_categories', snapshot['categories'])):
        for record in records:
            names = list(record)
            if table == 'synced_files':
                conflict_sql = 'ON CONFLICT (file_path) DO UPDATE SET ' + ', '.join((f'{name}=EXCLUDED.{name}' for name in names if name != 'file_path'))
            else:
                conflict_sql = 'ON CONFLICT (video_id, category) DO NOTHING'
            conn.execute(f"INSERT INTO {table} ({','.join(names)}) VALUES ({','.join(('%s' for _ in names))}) {conflict_sql}", [record[n] for n in names])
    override_snapshot = snapshot.get('metadata_overrides')
    if isinstance(override_snapshot, dict):
        conn.execute(
            'INSERT INTO video_metadata_overrides (video_id,fields,revision,updated_at) '
            'VALUES (%s,%s,%s,coalesce(%s::timestamptz,CURRENT_TIMESTAMP)) '
            'ON CONFLICT (video_id) DO UPDATE SET fields=EXCLUDED.fields,'
            'revision=EXCLUDED.revision,updated_at=EXCLUDED.updated_at',
            (video['id'], _core.json.dumps(override_snapshot.get('fields') or {}, ensure_ascii=False),
             override_snapshot.get('revision', 1), override_snapshot.get('updated_at')),
        )
    conn.execute("UPDATE video_duplicate_decisions SET state='undone' WHERE loser_id=%s", (video['id'],))
    k_id = (
        snapshot.get('_decision_keeper_id')
        or snapshot.get('keeper_before', {}).get('video', {}).get('id')
        or snapshot.get('keeper_after', {}).get('video', {}).get('id')
    )
    _restore_sources_snapshot(conn, video['id'], snapshot, k_id)


def _restore_keeper_video_fields(conn, keeper_id: str, current: dict, old_video: dict, new_video: dict) -> None:
    for field in ('title', 'plot', 'studio', 'date_released', 'thumbnail', 'performers', 'categories', 'tags',
                  'scene_match_source', 'scene_match_external_id', 'scene_match_url',
                  'scene_match_confidence'):
        if current.get(field) == new_video.get(field):
            conn.execute(f'UPDATE videos SET {field}=%s WHERE id=%s', (old_video.get(field), keeper_id))
    current_categories = set(_core.json.loads(current.get('categories') or '[]')
                             if isinstance(current.get('categories'), str) else current.get('categories') or [])
    after_categories = set(_core.json.loads(new_video.get('categories') or '[]')
                           if isinstance(new_video.get('categories'), str) else new_video.get('categories') or [])
    before_categories = _core.json.loads(old_video.get('categories') or '[]') if isinstance(old_video.get('categories'), str) else old_video.get('categories') or []
    if current_categories == after_categories:
        conn.execute('DELETE FROM video_categories WHERE video_id=%s', (keeper_id,))
        for category in before_categories:
            conn.execute('INSERT INTO video_categories (video_id,category) VALUES (%s,%s) ON CONFLICT DO NOTHING', (keeper_id, category))


def _restore_keeper_overrides(conn, keeper_id: str, before: dict, after: dict) -> None:
    current_override = conn.execute('SELECT fields FROM video_metadata_overrides WHERE video_id=%s', (keeper_id,)).fetchone()
    current_fields = current_override[0] if current_override else {}
    if isinstance(current_fields, str):
        current_fields = _core.json.loads(current_fields)
    if current_fields == after.get('overrides', {}):
        old_fields = before.get('overrides', {})
        if old_fields:
            conn.execute('INSERT INTO video_metadata_overrides (video_id,fields,revision,updated_at) VALUES (%s,%s,1,CURRENT_TIMESTAMP) ON CONFLICT (video_id) DO UPDATE SET fields=EXCLUDED.fields,revision=video_metadata_overrides.revision+1,updated_at=CURRENT_TIMESTAMP', (keeper_id, _core.json.dumps(old_fields, ensure_ascii=False)))
        else:
            conn.execute('DELETE FROM video_metadata_overrides WHERE video_id=%s', (keeper_id,))


def _restore_keeper_metadata(conn, snapshots: list) -> None:
    keeper_states = [snapshot for snapshot in snapshots if snapshot.get('keeper_before') and snapshot.get('keeper_after')]
    if keeper_states:
        before = keeper_states[0]['keeper_before']
        after = keeper_states[0]['keeper_after']
        keeper_id = str(before.get('video', {}).get('id') or '')
        current_row = conn.execute('SELECT * FROM videos WHERE id=%s', (keeper_id,)).fetchone()
        if current_row:
            columns = _core.db.table_columns(conn, 'videos')
            current = dict(zip(columns, current_row))
            old_video, new_video = before.get('video', {}), after.get('video', {})
            _restore_keeper_video_fields(conn, keeper_id, current, old_video, new_video)
            _restore_keeper_overrides(conn, keeper_id, before, after)


def _run_undo(conn, job_id, payload, media_dir, services, quarantine_root, now, run) -> dict:
    """Restore the items of a previous duplicate operation from their undo snapshots."""
    operation = str(payload['operation_id'])
    rows = conn.execute(
        "SELECT loser_id,snapshot,keeper_id FROM video_duplicate_decisions WHERE operation_id=%s AND state='active'",
        (operation,),
    ).fetchall()
    if not rows:
        raise ValueError('No active decision found to undo.')
    snapshots = []
    for r in rows:
        s = _core.json.loads(r[1])
        s['_decision_keeper_id'] = r[2]
        snapshots.append(s)
    job_stage_row = conn.execute('SELECT result FROM video_duplicate_jobs WHERE id=%s', (job_id,)).fetchone()
    try:
        resuming_undo = bool(job_stage_row and job_stage_row[0] and ('undo_snapshots' in _core.json.loads(job_stage_row[0])))
    except (TypeError, ValueError):
        resuming_undo = False
    _check_undo_snapshots(conn, snapshots, media_dir, quarantine_root, resuming_undo)
    _core._store_job_result_field(conn, job_id, 'operation_id', operation)
    _core._store_job_result_field(conn, job_id, 'undo_snapshots', snapshots)
    _restore_undo_files(conn, job_id, snapshots, services, run, resuming_undo)
    _core._record_job_progress(conn, job_id, 'saving_catalog')
    with conn:
        for snapshot in snapshots:
            _restore_snapshot_rows(conn, snapshot)
        _restore_keeper_metadata(conn, snapshots)
    run.catalog_committed = True
    result = {'operation_id': operation, 'restored': [s['video']['id'] for s in snapshots], 'local_state': 'restored'}
    return result
