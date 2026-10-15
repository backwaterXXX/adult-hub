"""Telegram NFO reconciliation against Jellyfin's supported item API."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import xml.etree.ElementTree as ET


def sync_jellyfin_telegram_metadata(url, api_key, media_dir, *, state_path=None,
                                    limit=200, item_ids=None, audit_only=False,
                                    poll_timeout=120, logger=None, session=None,
                                    workers=1, adopt_matching=False, _core):
    """Import changed Telegram NFO through Jellyfin, retaining IDs and user data.

    An NFO lock prevents online matching, but Jellyfin also skips local imports for
    already locked items. Unlock a complete API DTO only for a changed sidecar,
    then refresh it; the NFO restores the lock. Remember successful content hashes
    so later performer/tag-only NFO changes are imported too. No Jellyfin SQL writes.
    """
    if not url or not api_key:
        return {'checked': 0, 'needed': 0, 'updated': 0, 'failed': 0}
    import requests
    logger = logger or _core["_LOGGER"]
    session = session or requests.Session()
    workers = max(1, min(int(workers), 4))
    local = threading.local()
    local.session = session
    state_lock = threading.Lock()
    base = url.rstrip('/')
    headers = {
        'X-Emby-Token': api_key,
        'X-Emby-Authorization': f'MediaBrowser Client="adult-hub", Device="sync", DeviceId="adult-hub-sync", Version="1.0.0", Token="{api_key}"',
        'Authorization': f'MediaBrowser Client="adult-hub", Device="sync", DeviceId="adult-hub-sync", Version="1.0.0", Token="{api_key}"',
    }
    media_dir = Path(media_dir)
    state_path = Path(state_path or '/data/telegram-metadata-state.json')
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        state = {}

    def call(method, endpoint, **kwargs):
        if not hasattr(local, 'session'):
            local.session = requests.Session()
        response = local.session.request(method, base + endpoint, headers=headers,
                                   timeout=20, **kwargs)
        # Do not log HTTP exceptions: credential-bearing URLs may be included.
        response.raise_for_status()
        return response.json() if response.content else None

    def matches(dto, expected):
        for key, value in expected.items():
            if key == 'ProviderIds':
                actual_ids = {name.lower(): identifier for name, identifier in (dto.get(key) or {}).items()}
                expected_ids = {name.lower(): identifier for name, identifier in value.items()}
                if actual_ids != expected_ids:
                    return False
            elif (dto.get(key) or '') != (value or ''):
                return False
        return True

    def checkpoint():
        state_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = state_path.with_suffix('.tmp')
        temp_path.write_text(json.dumps(state, sort_keys=True))
        temp_path.replace(state_path)

    result = {'checked': 0, 'needed': 0, 'updated': 0, 'failed': 0, 'adopted': 0,
              'refresh_poll': {'calls': 0, 'timeouts': 0, 'total_seconds': 0.0,
                               'max_seconds': 0.0},
              '_confirmed_file_hashes': {}}
    candidates = []
    seen_ids = set()
    start = 0
    while True:
        page = call('GET', '/Items', params={
            'Recursive': 'true', 'IncludeItemTypes': 'Movie',
            'Fields': 'Path,Overview,OriginalTitle,Settings,Tags,Genres,Studios,People,ProviderIds',
            'StartIndex': start, 'Limit': 500,
        })
        items = page.get('Items', [])
        for dto in items:
            if dto['Id'] in seen_ids:
                continue
            seen_ids.add(dto['Id'])
            item_path = dto.get('Path') or ''
            if not item_path.startswith('/media/adult/'):
                continue
            filename = Path(item_path).name
            if not filename.startswith('telegram ['):
                continue
            if item_ids is not None and dto['Id'] not in item_ids:
                continue
            result['checked'] += 1
            ids_only = False
            try:
                nfo_path = media_dir / Path(filename).with_suffix('.nfo')
                digest, expected = _core["_telegram_nfo"](nfo_path)
                if (state.get(dto['Id']) == digest and dto.get('LockData')
                        and matches(dto, expected) and _core["_matches_complete_nfo"](dto, nfo_path)):
                    with state_lock:
                        result['_confirmed_file_hashes'][filename] = digest
                if (adopt_matching and dto['Id'] not in state and dto.get('LockData')
                        and matches(dto, expected) and _core["_matches_complete_nfo"](dto, nfo_path)):
                    result['adopted'] += 1
                    if not audit_only:
                        state[dto['Id']] = digest
                    continue
                if (adopt_matching and state.get(dto['Id']) in (None, digest)
                        and dto.get('LockData') and not matches(dto, {'ProviderIds': expected['ProviderIds']})
                        and matches(dto, {key: value for key, value in expected.items() if key != 'ProviderIds'})):
                    corrected = dict(dto, ProviderIds=expected['ProviderIds'])
                    ids_only = _core["_matches_complete_nfo"](corrected, nfo_path)
            except (OSError, ET.ParseError, ValueError):
                result['failed'] += 1
                continue
            if state.get(dto['Id']) != digest or not matches(dto, expected) or not dto.get('LockData'):
                result['needed'] += 1
                candidates.append((dto['Id'], digest, expected, ids_only, filename))
        start += len(items)
        if not items or start >= page.get('TotalRecordCount', start):
            break
    if not audit_only and result['adopted']:
        checkpoint()
    if audit_only or not candidates:
        return result
    libraries = call('GET', '/Library/VirtualFolders')
    adult = next((folder for folder in libraries
                  if '/media/adult' in folder.get('Locations', [])), None)
    if adult is None or adult.get('LibraryOptions', {}).get('MetadataSavers') != []:
        raise ValueError('Telegram NFO import requires Adult LibraryOptions.MetadataSavers=[]; '
                         'enabled savers cause replace-all refresh to skip local NFO')
    users = call('GET', '/Users')
    admin = next((u for u in users if u.get('Policy', {}).get('IsAdministrator')), None)
    if admin is None:
        raise ValueError('Jellyfin requires an administrator for complete item DTOs')
    def import_one(candidate):
        item_id, digest, expected, ids_only, filename = candidate
        dto = None
        last_current = None
        unlocked = False
        try:
            endpoint = f"/Users/{admin['Id']}/Items/{item_id}"
            dto = call('GET', endpoint)
            # Ensure a round trip cannot clear collection fields omitted by GET.
            if any(key not in dto for key in ('Name', 'Tags', 'Genres', 'ProviderIds')):
                raise ValueError('Jellyfin returned an incomplete update DTO')
            nfo_path = media_dir / Path(dto['Path']).with_suffix('.nfo').name
            if (ids_only and dto.get('LockData')
                    and matches(dto, {key: value for key, value in expected.items() if key != 'ProviderIds'})
                    and _core["_telegram_nfo"](nfo_path)[0] == digest
                    and _core["_matches_complete_nfo"](dict(dto, ProviderIds=expected['ProviderIds']), nfo_path)):
                # The movie has already imported all NFO metadata. Correct only
                # leftover foreign provider IDs, keeping protection throughout.
                dto['ProviderIds'] = expected['ProviderIds']
                call('POST', f'/Items/{item_id}', json=dto)
                deadline = time.monotonic() + 5
                while True:
                    current = call('GET', endpoint)
                    last_current = current
                    if (current.get('LockData') and matches(current, expected)
                            and _core["_matches_complete_nfo"](current, nfo_path)):
                        with state_lock:
                            state[item_id] = digest
                            result['updated'] += 1
                            result['_confirmed_file_hashes'][filename] = digest
                            if result['updated'] % 20 == 0:
                                checkpoint()
                        return
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Telegram provider ID correction did not become visible')
                    time.sleep(0.1)
            if dto.get('LockData') or not matches(dto, {'ProviderIds': expected['ProviderIds']}):
                dto['LockData'] = False
                # Telegram NFO is authoritative: discard foreign IMDb/TMDb IDs
                # left by a prior online match, preserving the Jellyfin item ID.
                dto['ProviderIds'] = expected['ProviderIds']
                call('POST', f'/Items/{item_id}', json=dto)
                unlocked = True
            # Item update completion and refresh scheduling can observe different
            # cached instances. Confirm the unlock through a fresh GET before
            # queueing a refresh, otherwise Jellyfin can skip its local readers.
            unlock_deadline = time.monotonic() + 5
            while True:
                visible = call('GET', endpoint)
                if visible.get('LockData') is False:
                    break
                if time.monotonic() >= unlock_deadline:
                    raise TimeoutError('Telegram metadata unlock did not become visible')
                time.sleep(0.1)
            call('POST', f'/Items/{item_id}/Refresh', params={
                'metadataRefreshMode': 'FullRefresh', 'imageRefreshMode': 'None',
                'replaceAllMetadata': 'true', 'replaceAllImages': 'false',
            })
            deadline = time.monotonic() + poll_timeout
            poll_started = time.monotonic()
            poll_timed_out = False
            try:
                while True:
                    current = call('GET', endpoint)
                    last_current = current
                    if current.get('LockData') and matches(current, expected):
                        with state_lock:
                            state[item_id] = digest
                            result['updated'] += 1
                            result['_confirmed_file_hashes'][filename] = digest
                            if result['updated'] % 20 == 0:
                                checkpoint()
                        break
                    if time.monotonic() >= deadline:
                        poll_timed_out = True
                        raise TimeoutError('Telegram metadata import did not complete')
                    time.sleep(0.1)
            finally:
                poll_elapsed = max(0.0, time.monotonic() - poll_started)
                with state_lock:
                    poll_stats = result['refresh_poll']
                    poll_stats['calls'] += 1
                    poll_stats['timeouts'] += int(poll_timed_out)
                    poll_stats['total_seconds'] = round(poll_stats['total_seconds'] + poll_elapsed, 3)
                    poll_stats['max_seconds'] = round(max(poll_stats['max_seconds'], poll_elapsed), 3)
        except Exception as exc:
            with state_lock:
                result['failed'] += 1
            logger.warning('Telegram metadata import failed for %s (%s)', item_id, type(exc).__name__)
            if isinstance(exc, TimeoutError):
                # Report why verification stalled without exposing any movie
                # titles, descriptions, URLs, credentials or DTO field values.
                lock_state = last_current.get('LockData') if last_current is not None else None
                if not isinstance(lock_state, bool):
                    lock_state = None
                mismatched = [key for key in ('Name', 'OriginalTitle', 'Overview', 'ProviderIds')
                              if key in expected and not matches(last_current or {}, {key: expected[key]})]
                logger.warning('Telegram metadata verification timeout for %s: '
                               'last_poll_available=%s LockData=%s mismatched_keys=%s',
                               item_id, last_current is not None, lock_state, mismatched)
            if unlocked and dto is not None:
                try:
                    # Restore protection using the latest complete DTO, avoiding
                    # overwriting any metadata the queued import already saved.
                    latest = call('GET', endpoint)
                    latest['LockData'] = True
                    call('POST', f'/Items/{item_id}', json=latest)
                except Exception:
                    logger.error('Could not restore Telegram metadata lock for %s', item_id)
    selected = candidates[:limit] if limit else candidates
    if workers == 1:
        for candidate in selected:
            import_one(candidate)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(import_one, selected))
    if result['updated']:
        checkpoint()
    poll_stats = result['refresh_poll']
    logger.info(
        'Telegram metadata API reconciliation: checked=%s needed=%s updated=%s failed=%s adopted=%s '
        'refresh_poll_calls=%s refresh_poll_timeouts=%s refresh_poll_total_s=%.3f refresh_poll_max_s=%.3f',
        result['checked'], result['needed'], result['updated'], result['failed'], result['adopted'],
        poll_stats['calls'], poll_stats['timeouts'], poll_stats['total_seconds'], poll_stats['max_seconds'],
    )
    return result
