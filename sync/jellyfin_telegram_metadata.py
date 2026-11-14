"""Telegram NFO reconciliation against Jellyfin's supported item API."""

import base64
import json
import hashlib
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import xml.etree.ElementTree as ET


def _matches_expected(dto, expected):
    for key, value in expected.items():
        if key == 'ProviderIds':
            actual_ids = {name.lower(): identifier for name, identifier in (dto.get(key) or {}).items()}
            expected_ids = {name.lower(): identifier for name, identifier in value.items()}
            if actual_ids != expected_ids:
                return False
        elif (dto.get(key) or '') != (value or ''):
            return False
    return True


def _linked_scene_nfo(nfo_path):
    try:
        root = ET.parse(nfo_path).getroot()
    except (OSError, ET.ParseError):
        return False
    for uniqueid in root.findall('uniqueid'):
        if (str(uniqueid.get('type') or '').casefold() in {'stashdb', 'theporndb', 'tpdb'}
                and str(uniqueid.text or '').strip()):
            return True
    website = str(root.findtext('website') or '').casefold()
    return 'stashdb.org/' in website or 'theporndb.net/' in website


class _ItemImport:
    """Progress of one item's import attempt, read when that attempt fails."""

    def __init__(self):
        self.dto = None
        self.last_current = None
        self.unlocked = False


class _TelegramNfoReconciler:
    """One reconciliation pass: inventory, artwork uploads, then metadata imports."""

    def __init__(self, *, requests, base, headers, local, session, media_dir, state,
                 state_path, image_state, image_state_path, state_lock, logger, limit,
                 item_ids, audit_only, poll_timeout, adopt_matching, workers, core,
                 valid_thumbnail_bytes):
        self.requests = requests
        self.base = base
        self.headers = headers
        self.local = local
        self.media_dir = media_dir
        self.state = state
        self.state_path = state_path
        self.image_state = image_state
        self.image_state_path = image_state_path
        self.state_lock = state_lock
        self.logger = logger
        self.limit = limit
        self.item_ids = item_ids
        self.audit_only = audit_only
        self.poll_timeout = poll_timeout
        self.adopt_matching = adopt_matching
        self.workers = workers
        self.core = core
        self.valid_thumbnail_bytes = valid_thumbnail_bytes
        self.local.session = session
        self.result = {'checked': 0, 'needed': 0, 'updated': 0, 'failed': 0, 'adopted': 0,
                       'images_uploaded': 0, 'images_failed': 0,
                       'phash_thumbnails_uploaded': 0, 'phash_thumbnails_failed': 0,
                       'refresh_poll': {'calls': 0, 'timeouts': 0, 'total_seconds': 0.0,
                                        'max_seconds': 0.0},
                       '_confirmed_file_hashes': {}}
        self.candidates = []
        self.image_candidates = []
        self.phash_fallback_candidates = []
        self.admin = None

    def call(self, method, endpoint, **kwargs):
        if not hasattr(self.local, 'session'):
            self.local.session = self.requests.Session()
        request_headers = dict(self.headers)
        request_headers.update(kwargs.pop('headers', {}) or {})
        response = self.local.session.request(method, self.base + endpoint, headers=request_headers,
                                              timeout=20, **kwargs)
        # Do not log HTTP exceptions: credential-bearing URLs may be included.
        response.raise_for_status()
        return response.json() if response.content else None

    def checkpoint(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.state_path.with_suffix('.tmp')
        temp_path.write_text(json.dumps(self.state, sort_keys=True))
        temp_path.replace(self.state_path)

    def checkpoint_images(self):
        self.image_state_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.image_state_path.with_suffix('.tmp')
        temp_path.write_text(json.dumps(self.image_state, sort_keys=True))
        temp_path.replace(self.image_state_path)

    def run(self):
        self._scan_inventory()
        if not self.audit_only and self.result['adopted']:
            self.checkpoint()
        if self.audit_only:
            return self.result
        selected_images = self.image_candidates[:self.limit] if self.limit else self.image_candidates
        self._upload_scene_posters(selected_images)
        if self.result['images_uploaded'] or self.result['images_failed']:
            self.logger.info('Linked scene artwork upload: uploaded=%s failed=%s',
                             self.result['images_uploaded'], self.result['images_failed'])
        if self.phash_fallback_candidates:
            self._upload_phash_fallbacks()
        if not self.candidates:
            return self.result
        self._require_administrator()
        self._import_candidates()
        if self.result['updated']:
            self.checkpoint()
        self._log_summary()
        return self.result

    # Inventory scan.

    def _scan_inventory(self):
        start = 0
        seen_ids = set()
        adult_folder_id = None
        try:
            libraries = self.call('GET', '/Library/VirtualFolders')
            adult = next((folder for folder in (libraries or [])
                          if '/media/adult' in folder.get('Locations', [])), None)
            if adult:
                adult_folder_id = adult.get('ItemId')
        except Exception:
            pass

        params = {
            'IncludeItemTypes': 'Movie',
            'Fields': 'Path,Overview,OriginalTitle,Settings,Tags,Genres,Studios,People,ProviderIds',
            'Limit': 500,
        }
        if adult_folder_id:
            params['ParentId'] = adult_folder_id
        else:
            params['Recursive'] = 'true'

        while True:
            params['StartIndex'] = start
            page = self.call('GET', '/Items', params=params)
            items = page.get('Items', [])
            for dto in items:
                self._scan_item(dto, seen_ids)
            start += len(items)
            if not items or start >= page.get('TotalRecordCount', start):
                break

    def _scan_item(self, dto, seen_ids):
        if dto['Id'] in seen_ids:
            return
        seen_ids.add(dto['Id'])
        item_path = dto.get('Path') or ''
        if not item_path.startswith('/media/adult/'):
            return
        filename = Path(item_path).name
        if not filename.startswith('telegram ['):
            return
        if self.item_ids is not None and dto['Id'] not in self.item_ids:
            return
        self.result['checked'] += 1
        outcome, digest, expected, ids_only = self._inspect_item(dto, filename)
        if outcome == 'failed':
            self.result['failed'] += 1
            return
        if outcome == 'adopted':
            return
        if self.state.get(dto['Id']) != digest or not _matches_expected(dto, expected) or not dto.get('LockData'):
            self.result['needed'] += 1
            self.candidates.append((dto['Id'], digest, expected, ids_only, filename))

    def _inspect_item(self, dto, filename):
        """Return (outcome, digest, expected, ids_only); outcome is failed, adopted or ok."""
        ids_only = False
        try:
            nfo_path = self.media_dir / Path(filename).with_suffix('.nfo')
            poster_path = nfo_path.with_suffix('.jpg')
            has_external_poster, invalid_local_poster = self._check_local_poster(dto, nfo_path, poster_path)
            self._queue_phash_fallback(dto, filename, poster_path, has_external_poster, invalid_local_poster)
            digest, expected = self.core["_telegram_nfo"](nfo_path)
            if (self.state.get(dto['Id']) == digest and dto.get('LockData')
                    and _matches_expected(dto, expected) and self.core["_matches_complete_nfo"](dto, nfo_path)):
                with self.state_lock:
                    self.result['_confirmed_file_hashes'][filename] = digest
            if (self.adopt_matching and dto['Id'] not in self.state and dto.get('LockData')
                    and _matches_expected(dto, expected) and self.core["_matches_complete_nfo"](dto, nfo_path)):
                self.result['adopted'] += 1
                if not self.audit_only:
                    self.state[dto['Id']] = digest
                return 'adopted', digest, expected, ids_only
            if (self.adopt_matching and self.state.get(dto['Id']) in (None, digest)
                    and dto.get('LockData') and not _matches_expected(dto, {'ProviderIds': expected['ProviderIds']})
                    and _matches_expected(dto, {key: value for key, value in expected.items() if key != 'ProviderIds'})):
                corrected = dict(dto, ProviderIds=expected['ProviderIds'])
                ids_only = self.core["_matches_complete_nfo"](corrected, nfo_path)
        except (OSError, ET.ParseError, ValueError):
            return 'failed', None, None, False
        return 'ok', digest, expected, ids_only

    def _check_local_poster(self, dto, nfo_path, poster_path):
        """Return (has_external_poster, invalid_local_poster) and queue a linked-scene poster upload."""
        poster_marker = poster_path.with_suffix('.jpg.external')
        has_external_poster = False
        invalid_local_poster = False
        if not self.audit_only and poster_path.is_file() and not poster_path.is_symlink():
            poster_bytes = poster_path.read_bytes()
            poster_digest = hashlib.sha256(poster_bytes).hexdigest()
            usable_poster = self.valid_thumbnail_bytes(
                poster_bytes, str(poster_path), minimum_size=(180, 320)
            )
            invalid_local_poster = not usable_poster
            if (usable_poster and poster_marker.is_file() and not poster_marker.is_symlink()
                    and _linked_scene_nfo(nfo_path)):
                has_external_poster = poster_marker.read_text(encoding='ascii').strip() == poster_digest
            if has_external_poster and self.image_state.get(dto['Id']) != poster_digest:
                self.image_candidates.append((dto['Id'], poster_bytes, poster_digest))
        return has_external_poster, invalid_local_poster

    def _queue_phash_fallback(self, dto, filename, poster_path, has_external_poster, invalid_local_poster):
        image_tags = dto.get('ImageTags')
        primary_tag = image_tags.get('Primary') if isinstance(image_tags, dict) else None
        if (not self.audit_only and isinstance(image_tags, dict) and not has_external_poster
                and (invalid_local_poster or (
                    not primary_tag and not poster_path.exists() and not poster_path.is_symlink()
                ))):
            self.phash_fallback_candidates.append((dto['Id'], filename))

    # Artwork uploads.

    def _upload_scene_posters(self, selected_images):
        for item_id, poster_bytes, poster_digest in selected_images:
            try:
                self.call('POST', f'/Items/{item_id}/Images/Primary',
                          data=base64.b64encode(poster_bytes),
                          headers={'Content-Type': 'image/jpeg'})
                self.image_state[item_id] = poster_digest
                self.result['images_uploaded'] += 1
                self.checkpoint_images()
            except Exception as exc:
                self.result['images_failed'] += 1
                self.logger.warning('Linked scene image upload failed for %s (%s)', item_id, type(exc).__name__)

    def _upload_phash_fallbacks(self):
        fallback_limit = max(0, self.limit - self.result['images_uploaded']) if self.limit else None
        selected_fallbacks = (self.phash_fallback_candidates[:fallback_limit]
                              if fallback_limit is not None else self.phash_fallback_candidates)
        thumbnail_by_file = self._load_phash_thumbnails(selected_fallbacks)
        for item_id, filename in selected_fallbacks:
            self._upload_phash_thumbnail(item_id, filename, thumbnail_by_file)
        if self.result['phash_thumbnails_uploaded'] or self.result['phash_thumbnails_failed']:
            self.logger.info('Jellyfin pHash poster fallback: uploaded=%s failed=%s',
                             self.result['phash_thumbnails_uploaded'], self.result['phash_thumbnails_failed'])

    def _load_phash_thumbnails(self, selected_fallbacks):
        thumbnail_by_file = {}
        if selected_fallbacks:
            try:
                conn = self.core['db'].connect(os.getenv('DATABASE_URL') or None)
                try:
                    filenames = [filename for _item_id, filename in selected_fallbacks]
                    rows = conn.execute(
                        """SELECT sf.file_path,t.image_bytes
                           FROM synced_files sf
                           JOIN video_phash_thumbnails t ON t.video_id=sf.video_id
                           WHERE sf.file_path=ANY(%s) AND sf.video_id LIKE 'telegram:%%'""",
                        (filenames,),
                    ).fetchall()
                    thumbnail_by_file = {str(filename): bytes(image_bytes)
                                         for filename, image_bytes in rows}
                finally:
                    conn.close()
            except Exception as exc:
                self.result['phash_thumbnails_failed'] += len(selected_fallbacks)
                self.logger.warning('Could not load stored pHash thumbnails (%s)', type(exc).__name__)
        return thumbnail_by_file

    def _upload_phash_thumbnail(self, item_id, filename, thumbnail_by_file):
        thumbnail_bytes = thumbnail_by_file.get(filename)
        if not thumbnail_bytes:
            return
        if not thumbnail_bytes.startswith(b'\xff\xd8'):
            self.result['phash_thumbnails_failed'] += 1
            self.logger.warning('Stored pHash thumbnail is not a JPEG for %s', item_id)
            return
        if not self.valid_thumbnail_bytes(thumbnail_bytes, 'phash://frame', minimum_size=(180, 320)):
            self.result['phash_thumbnails_failed'] += 1
            self.logger.warning('Stored pHash thumbnail failed image quality checks for %s', item_id)
            return
        try:
            thumbnail_digest = hashlib.sha256(thumbnail_bytes).hexdigest()
            self.call('POST', f'/Items/{item_id}/Images/Primary',
                      data=base64.b64encode(thumbnail_bytes),
                      headers={'Content-Type': 'image/jpeg'})
            self.image_state[item_id] = thumbnail_digest
            self.result['phash_thumbnails_uploaded'] += 1
            self.result['images_uploaded'] += 1
            self.checkpoint_images()
        except Exception as exc:
            self.result['phash_thumbnails_failed'] += 1
            self.result['images_failed'] += 1
            self.logger.warning('pHash thumbnail upload failed for %s (%s)', item_id, type(exc).__name__)

    # Metadata imports.

    def _require_administrator(self):
        libraries = self.call('GET', '/Library/VirtualFolders')
        adult = next((folder for folder in libraries
                      if '/media/adult' in folder.get('Locations', [])), None)
        if adult is None or adult.get('LibraryOptions', {}).get('MetadataSavers') != []:
            raise ValueError('Telegram NFO import requires Adult LibraryOptions.MetadataSavers=[]; '
                             'enabled savers cause replace-all refresh to skip local NFO')
        users = self.call('GET', '/Users')
        admin = next((u for u in users if u.get('Policy', {}).get('IsAdministrator')), None)
        if admin is None:
            raise ValueError('Jellyfin requires an administrator for complete item DTOs')
        self.admin = admin

    def _import_candidates(self):
        selected = self.candidates[:self.limit] if self.limit else self.candidates
        if self.workers == 1:
            for candidate in selected:
                self._import_one(candidate)
        else:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                list(pool.map(self._import_one, selected))

    def _import_one(self, candidate):
        item_id, digest, expected, ids_only, filename = candidate
        attempt = _ItemImport()
        try:
            self._run_item_import(attempt, item_id, digest, expected, ids_only, filename)
        except Exception as exc:
            self._report_import_failure(attempt, item_id, expected, exc)

    def _run_item_import(self, attempt, item_id, digest, expected, ids_only, filename):
        endpoint = f"/Users/{self.admin['Id']}/Items/{item_id}"
        attempt.dto = self.call('GET', endpoint)
        # Ensure a round trip cannot clear collection fields omitted by GET.
        if any(key not in attempt.dto for key in ('Name', 'Tags', 'Genres', 'ProviderIds')):
            raise ValueError('Jellyfin returned an incomplete update DTO')
        nfo_path = self.media_dir / Path(attempt.dto['Path']).with_suffix('.nfo').name
        if (ids_only and attempt.dto.get('LockData')
                and _matches_expected(attempt.dto, {key: value for key, value in expected.items() if key != 'ProviderIds'})
                and self.core["_telegram_nfo"](nfo_path)[0] == digest
                and self.core["_matches_complete_nfo"](dict(attempt.dto, ProviderIds=expected['ProviderIds']), nfo_path)):
            # The movie has already imported all NFO metadata. Correct only
            # leftover foreign provider IDs, keeping protection throughout.
            self._correct_provider_ids(attempt, endpoint, item_id, digest, expected, filename, nfo_path)
            return
        if attempt.dto.get('LockData') or not _matches_expected(attempt.dto, {'ProviderIds': expected['ProviderIds']}):
            attempt.dto['LockData'] = False
            # Telegram NFO is authoritative: discard foreign IMDb/TMDb IDs
            # left by a prior online match, preserving the Jellyfin item ID.
            attempt.dto['ProviderIds'] = expected['ProviderIds']
            self.call('POST', f'/Items/{item_id}', json=attempt.dto)
            attempt.unlocked = True
        self._wait_for_unlock(endpoint)
        self.call('POST', f'/Items/{item_id}/Refresh', params={
            'metadataRefreshMode': 'FullRefresh', 'imageRefreshMode': 'None',
            'replaceAllMetadata': 'true', 'replaceAllImages': 'false',
        })
        self._poll_refresh(attempt, endpoint, item_id, digest, expected, filename)

    def _correct_provider_ids(self, attempt, endpoint, item_id, digest, expected, filename, nfo_path):
        attempt.dto['ProviderIds'] = expected['ProviderIds']
        self.call('POST', f'/Items/{item_id}', json=attempt.dto)
        deadline = time.monotonic() + 5
        while True:
            current = self.call('GET', endpoint)
            attempt.last_current = current
            if (current.get('LockData') and _matches_expected(current, expected)
                    and self.core["_matches_complete_nfo"](current, nfo_path)):
                self._record_success(item_id, digest, filename)
                return
            if time.monotonic() >= deadline:
                raise TimeoutError('Telegram provider ID correction did not become visible')
            time.sleep(0.1)

    def _wait_for_unlock(self, endpoint):
        # Item update completion and refresh scheduling can observe different
        # cached instances. Confirm the unlock through a fresh GET before
        # queueing a refresh, otherwise Jellyfin can skip its local readers.
        unlock_deadline = time.monotonic() + 5
        while True:
            visible = self.call('GET', endpoint)
            if visible.get('LockData') is False:
                break
            if time.monotonic() >= unlock_deadline:
                raise TimeoutError('Telegram metadata unlock did not become visible')
            time.sleep(0.1)

    def _poll_refresh(self, attempt, endpoint, item_id, digest, expected, filename):
        deadline = time.monotonic() + self.poll_timeout
        poll_started = time.monotonic()
        poll_timed_out = False
        try:
            while True:
                current = self.call('GET', endpoint)
                attempt.last_current = current
                if current.get('LockData') and _matches_expected(current, expected):
                    self._record_success(item_id, digest, filename)
                    break
                if time.monotonic() >= deadline:
                    poll_timed_out = True
                    raise TimeoutError('Telegram metadata import did not complete')
                time.sleep(0.1)
        finally:
            self._record_poll_stats(poll_started, poll_timed_out)

    def _record_success(self, item_id, digest, filename):
        with self.state_lock:
            self.state[item_id] = digest
            self.result['updated'] += 1
            self.result['_confirmed_file_hashes'][filename] = digest
            if self.result['updated'] % 20 == 0:
                self.checkpoint()

    def _record_poll_stats(self, poll_started, poll_timed_out):
        poll_elapsed = max(0.0, time.monotonic() - poll_started)
        with self.state_lock:
            poll_stats = self.result['refresh_poll']
            poll_stats['calls'] += 1
            poll_stats['timeouts'] += int(poll_timed_out)
            poll_stats['total_seconds'] = round(poll_stats['total_seconds'] + poll_elapsed, 3)
            poll_stats['max_seconds'] = round(max(poll_stats['max_seconds'], poll_elapsed), 3)

    def _report_import_failure(self, attempt, item_id, expected, exc):
        with self.state_lock:
            self.result['failed'] += 1
        self.logger.warning('Telegram metadata import failed for %s (%s)', item_id, type(exc).__name__)
        if isinstance(exc, TimeoutError):
            self._log_verification_timeout(attempt, item_id, expected)
        if attempt.unlocked and attempt.dto is not None:
            self._restore_lock(item_id)

    def _log_verification_timeout(self, attempt, item_id, expected):
        # Report why verification stalled without exposing any movie
        # titles, descriptions, URLs, credentials or DTO field values.
        lock_state = attempt.last_current.get('LockData') if attempt.last_current is not None else None
        if not isinstance(lock_state, bool):
            lock_state = None
        mismatched = [key for key in ('Name', 'OriginalTitle', 'Overview', 'ProviderIds')
                      if key in expected and not _matches_expected(attempt.last_current or {}, {key: expected[key]})]
        self.logger.warning('Telegram metadata verification timeout for %s: '
                            'last_poll_available=%s LockData=%s mismatched_keys=%s',
                            item_id, attempt.last_current is not None, lock_state, mismatched)

    def _restore_lock(self, item_id):
        try:
            # Restore protection using the latest complete DTO, avoiding
            # overwriting any metadata the queued import already saved.
            endpoint = f"/Users/{self.admin['Id']}/Items/{item_id}"
            latest = self.call('GET', endpoint)
            latest['LockData'] = True
            self.call('POST', f'/Items/{item_id}', json=latest)
        except Exception:
            self.logger.error('Could not restore Telegram metadata lock for %s', item_id)

    def _log_summary(self):
        poll_stats = self.result['refresh_poll']
        self.logger.info(
            'Telegram metadata API reconciliation: checked=%s needed=%s updated=%s failed=%s adopted=%s '
            'images_uploaded=%s images_failed=%s '
            'refresh_poll_calls=%s refresh_poll_timeouts=%s refresh_poll_total_s=%.3f refresh_poll_max_s=%.3f',
            self.result['checked'], self.result['needed'], self.result['updated'], self.result['failed'], self.result['adopted'],
            self.result['images_uploaded'], self.result['images_failed'],
            poll_stats['calls'], poll_stats['timeouts'], poll_stats['total_seconds'], poll_stats['max_seconds'],
        )


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
    from sync.thumbnails import _valid_thumbnail_bytes
    logger = logger or _core["_LOGGER"]
    session = session or requests.Session()
    workers = max(1, min(int(workers), 4))
    local = threading.local()
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
    image_state_path = state_path.with_name(f"{state_path.stem}-images.json")
    try:
        image_state = json.loads(image_state_path.read_text())
        if not isinstance(image_state, dict):
            image_state = {}
    except (OSError, ValueError):
        image_state = {}
    reconciler = _TelegramNfoReconciler(
        requests=requests, base=base, headers=headers, local=local, session=session,
        media_dir=media_dir, state=state, state_path=state_path, image_state=image_state,
        image_state_path=image_state_path, state_lock=state_lock, logger=logger, limit=limit,
        item_ids=item_ids, audit_only=audit_only, poll_timeout=poll_timeout,
        adopt_matching=adopt_matching, workers=workers, core=_core,
        valid_thumbnail_bytes=_valid_thumbnail_bytes,
    )
    return reconciler.run()
