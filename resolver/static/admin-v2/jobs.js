(() => {
  'use strict';

  const V2_API = '/admin/api/v2';
  const LIMIT = 50;
  const $ = (selector, root = document) => root.querySelector(selector);

  const state = {
    entered: false, activeTab: '', offset: 0, total: null, nextOffset: null, truncated: false,
    list: [], filter: { queue: 'all', state: '', action: '' }, directJobId: '', directJobQueue: 'jobs', seq: 0, abort: null,
    workers: {},
    access: { checked: false, ready: false, actionsEnabled: false, tokenRequired: false }, accessPromise: null,
    pollTimer: null, operationOffset: 0, operationTotal: 0, operationSeq: 0,
    operationDetailSeq: 0, openOperationId: null
  };
  const POLL_INTERVAL_MS = 15_000;
  const OPERATION_LIMIT = 20;

  const ACTION_LABELS = {
    phash_generation: 'pHash and thumbnail',
    hide: 'Hide duplicates', dismiss: 'Dismiss pair', undo: 'Undo hide', undismiss: 'Undo dismissal',
    performer_merge: 'Merge performers', performer_refresh: 'Refresh performer metadata', metadata_backfill: 'External metadata scan', telegram_fingerprint_scan: 'Telegram fingerprint scan', artwork_download: 'Download artwork', artwork_generate: 'Generate artwork', collections_refresh: 'Update collections', quarantine: 'Quarantine', orphan_file_operation: 'Orphan file quarantine or restore', metadata_update: 'Update metadata', availability_check: 'Check availability', catalog_full_audit: 'Catalog health audit schedule', consistency_audit: 'Consistency audit', metadata_quality_audit: 'Metadata quality audit', audit_repair_nfo: 'Repair audit files', audit_import_refresh: 'Request Jellyfin reimport', backup_create: 'Create backup', backup_restore_test: 'Test backup restore', sync: 'Sync'
  };
  const HISTORICAL_ACTION_LABELS = { lukayan_fingerprint: 'Telegram scene lookup', telegram_fingerprint: 'Telegram scene lookup' };
  const METADATA_PROGRESS_STAGES = new Set([
    'scanning_metadata', 'metadata_pbc', 'metadata_photos', 'metadata_tpdb', 'metadata_stashdb', 'metadata_scenes'
  ]);

  // Shared helpers live in admin-v2.js and are published through window.AdminV2.
  const helpers = () => window.AdminV2 || {};
  const create = (...args) => helpers().create(...args);
  const setText = (target, value) => helpers().setText(target, value);
  const safeId = value => helpers().safeId(value);
  const announce = message => helpers().announce(message);
  const scrub = value => helpers().scrub(value);
  const titleCase = value => helpers().titleCase(value);
  const formatDuration = (ms, state) => helpers().formatDuration(ms, state);
  const detailField = (label, value) => helpers().detailField(label, value);

  function actionLabel(value, auditMode) {
    const key = safeId(value).toLowerCase();
    if (key === 'consistency_audit' && auditMode === 'file_consistency') return 'File consistency audit';
    if (key === 'consistency_audit' && auditMode === 'full') return 'Full catalog audit';
    return ACTION_LABELS[key] || HISTORICAL_ACTION_LABELS[key] ||
      (key.startsWith('lukayan_') ? 'Telegram scene lookup' : (key ? titleCase(key) : 'Task'));
  }
  function statePill(value) {
    const raw = safeId(value).toLowerCase();
    if (raw === 'done') return ['Done', 'pill-good'];
    if (raw === 'failed') return ['Failed', 'pill-danger'];
    if (raw === 'processing') return ['Processing', 'pill-warning'];
    if (raw === 'queued') return ['Queued', 'pill-neutral'];
    if (raw === 'cancelled') return ['Cancelled', 'pill-neutral'];
    return [raw ? titleCase(raw) : 'Unknown', 'pill-neutral'];
  }
  function stageLabel(value) {
    const labels = {
      queued: 'Waiting for worker', processing: 'Worker processing',
      starting: 'Starting task', validating: 'Checking current data',
      preparing_files: 'Preparing recovery data', moving_files: 'Moving library files',
      restoring_files: 'Restoring library files', saving_catalog: 'Saving catalog changes',
      merging_performers: 'Merging performers', refreshing_collections: 'Refreshing collections',
      refreshing_jellyfin: 'Sending Jellyfin refresh request', jellyfin_refresh: 'Sending Jellyfin refresh request',
      writing_nfo: 'Writing performer metadata files',
      applying_metadata: 'Writing metadata and NFO files',
      downloading_artwork: 'Looking up and downloading scene artwork',
      generating_artwork: 'Extracting artwork from a video frame',
      scanning_metadata: 'Scanning external metadata providers',
      metadata_pbc: 'Enriching actor metadata from PBC',
      metadata_photos: 'Checking actor portrait providers',
      metadata_tpdb: 'Enriching actor and video metadata from ThePornDB',
      metadata_stashdb: 'Enriching actor metadata from StashDB',
      metadata_scenes: 'Enriching video metadata from studio catalogs and ThePornDB',
      checking_availability: 'Checking playback availability',
      recovery: 'Recovering operation',
      done: 'Completed', failed: 'Failed', cancelled: 'Cancelled before start'
    };
    return labels[safeId(value).toLowerCase()] || 'Progress details unavailable';
  }
  function localResultLabel(value) {
    const labels = { hidden: 'Saved locally', dismissed: 'Saved locally', restored: 'Restored locally', quarantined: 'Quarantined locally', metadata_updated: 'Metadata updated locally', artwork_downloaded: 'Artwork downloaded locally', artwork_generated: 'Artwork generated locally', artwork_not_found: 'No matching artwork', artwork_download_failed: 'Poster download failed', artwork_lookup_failed: 'Artwork lookup failed', availability_checked: 'Availability check recorded', audit_completed: 'Audit complete' };
    return labels[safeId(value).toLowerCase()] || 'Not recorded';
  }
  function jellyfinResultLabel(value) {
    const labels = {
      pending: 'Refresh request in progress', updated: 'Refresh request accepted',
      failed: 'Refresh request failed'
    };
    return labels[safeId(value).toLowerCase()] || 'Not recorded';
  }
  function jellyfinImportLabel(value) {
    const labels = {
      not_requested: 'Not checked; refresh request was not accepted',
      not_confirmed: 'Refresh request accepted; import not confirmed'
    };
    return labels[safeId(value).toLowerCase()] || 'Not recorded';
  }
  function displayTime(value) {
    if (value === null || value === undefined || value === '') return '—';
    const numeric = typeof value === 'number' && value < 100000000000 ? value * 1000 : value;
    const date = new Date(numeric);
    if (Number.isNaN(date.getTime())) return scrub(value);
    return new Intl.DateTimeFormat('en-US', { dateStyle: 'medium', timeStyle: 'short' }).format(date);
  }
  function processingActivityWarning(job) {
    if (safeId(job.state).toLowerCase() !== 'processing' || job.activity_stale !== true) return '';
    return 'No worker activity has been recorded for more than 90 seconds. The task may need attention.';
  }
  function resultSummary(job) {
    const provided = job.result_summary ?? job.resultSummary;
    if (typeof provided === 'string' && provided.trim()) return scrub(provided);
    const result = job.result;
    if (result === null || result === undefined || result === '') {
      return safeId(job.state).toLowerCase() === 'done' ? 'Completed' : '';
    }
    if (typeof result === 'string') return scrub(result);
    if (typeof result !== 'object') return scrub(result);
    const parts = [];
    if (result.message) parts.push(scrub(result.message));
    if (result.local_state) parts.push(scrub(result.local_state));
    if (result.jellyfin_refresh === 'failed') parts.push('Local result saved; Jellyfin refresh failed');
    else if (result.jellyfin_refresh) parts.push(`Jellyfin refresh ${scrub(result.jellyfin_refresh)}`);
    if (result.jellyfin_import === 'not_confirmed') parts.push('Jellyfin import not confirmed');
    else if (result.jellyfin_import === 'not_requested') parts.push('Jellyfin import not checked');
    else if (result.jellyfin_import === 'confirmed') parts.push('Jellyfin import confirmed');
    if (result.recovery) parts.push(`Recovery ${scrub(result.recovery)}`);
    if (result.error) parts.push(`Failed: ${scrub(result.error)}`);
    if (parts.length) return parts.join(' · ');
    return safeId(job.state).toLowerCase() === 'done' ? 'Completed' : '';
  }
  function jobError(job) { return scrub(job.error || job.error_message); }
  function isJobsRoute() {
    return location.hash.replace(/^#/, '').split(/[?&]/, 1)[0] === 'jobs';
  }
  function jobsTab() {
    const hashQuery = location.hash.includes('?') ? location.hash.slice(location.hash.indexOf('?') + 1) : '';
    const requested = new URLSearchParams(hashQuery).get('tab');
    return ['tasks', 'bulk', 'schedules'].includes(requested) ? requested : 'tasks';
  }
  function readFiltersFromUrl() {
    const params = new URLSearchParams(location.search);
    const requestedJobId = params.get('job_id') || '';
    state.directJobId = /^[A-Za-z0-9_-]{1,128}$/.test(requestedJobId) ? requestedJobId : '';
    state.directJobQueue = params.get('job_queue') === 'phash' ? 'phash' : 'jobs';
    if (state.directJobId) {
      params.delete('job_id');
      const url = new URL(location.href);
      url.search = params.toString();
      history.replaceState({}, '', url);
    }
    const requestedState = params.get('job_state') || '';
    const requestedAction = params.get('job_action') || '';
    state.filter = {
      queue: ['all', 'jobs', 'phash'].includes(params.get('job_queue')) ? params.get('job_queue') : 'all',
      state: ['active', 'queued', 'processing', 'done', 'failed', 'cancelled'].includes(requestedState) ? requestedState : '',
      action: Object.hasOwn(ACTION_LABELS, requestedAction) ? requestedAction : ''
    };
    const requestedOffset = Number(params.get('job_offset'));
    state.offset = Number.isSafeInteger(requestedOffset) && requestedOffset > 0 ? requestedOffset : 0;
    const form = $('#jobsFilters');
    form.elements.namedItem('state').value = state.filter.state;
    form.elements.namedItem('action').value = state.filter.action;
    form.elements.namedItem('queue').value = state.filter.queue;
  }
  function writeFiltersToUrl(replace = true) {
    const url = new URL(location.href);
    for (const [key, value] of [['job_queue', state.filter.queue === 'all' ? '' : state.filter.queue], ['job_state', state.filter.state], ['job_action', state.filter.action], ['job_offset', state.offset ? String(state.offset) : '']]) {
      if (value) url.searchParams.set(key, value);
      else url.searchParams.delete(key);
    }
    if (url.href === location.href) return;
    history[replace ? 'replaceState' : 'pushState']({}, '', url);
  }
  function commitFiltersToUrl() {
    writeFiltersToUrl(false);
  }
  function restoreFiltersFromHistory() {
    if (!isJobsRoute() || !state.entered) {
      helpers().scheduleSync(sync);
      return;
    }
    readFiltersFromUrl();
    setNotice('', '');
    loadJobs();
  }

  const getJson = (url, signal) => window.AdminV2.getJson(url, signal);
  async function postJson(url, action = 'Retry', payload) {
    if (!state.access.checked) await loadAccess();
    if (!state.access.ready) throw new Error(`Write access could not be verified. ${action} is disabled.`);
    if (!state.access.actionsEnabled) throw new Error(`This panel is read-only. ${action} is unavailable.`);
    return window.AdminV2.postJson(url, payload);
  }

  function updateRetryControls() {
    const enabled = state.access.ready && state.access.actionsEnabled;
    document.querySelectorAll('[data-job-retry], [data-job-cancel], [data-job-undo-dismiss]').forEach(button => {
      button.disabled = !enabled || Boolean(button.dataset.busy);
      button.title = enabled ? '' : !state.access.ready ? 'Checking write access…' : 'This action is unavailable in read-only mode.';
    });
  }
  async function loadAccess() {
    if (state.accessPromise) return state.accessPromise;
    state.access.checked = false;
    state.access.ready = false;
    updateRetryControls();
    state.accessPromise = (async () => {
      try {
        const [capabilities, activeJobs] = await Promise.all([
          getJson(`${V2_API}/capabilities`),
          getJson(`${V2_API}/jobs?state=active&limit=1&offset=0`)
        ]);
        state.access.actionsEnabled = capabilities.actions_enabled === true;
        state.access.tokenRequired = capabilities.token_required === true;
        state.access.ready = Boolean(activeJobs.data && activeJobs.data.available === true);
        state.access.checked = true;
        if (!state.access.ready) setNotice('warning', 'Retry is disabled because active jobs could not be checked.');
        else if (!state.access.actionsEnabled) setNotice('warning', 'This panel is read-only. Failed jobs cannot be retried.');
        else setNotice('', '');
      } catch {
        state.access.ready = false;
        state.access.actionsEnabled = false;
        state.access.checked = true;
        setNotice('warning', 'Retry is disabled because write access could not be verified.');
      } finally {
        state.accessPromise = null;
        updateRetryControls();
      }
    })();
    return state.accessPromise;
  }

  function setFeedback(text) { setText($('#jobsFeedback'), text); }
  function setNotice(kind, text) {
    const node = $('#jobsNotice');
    if (!node) return;
    if (!text) { node.hidden = true; node.replaceChildren(); node.setAttribute('role', 'status'); node.setAttribute('aria-live', 'polite'); return; }
    node.hidden = false;
    node.setAttribute('role', kind === 'error' ? 'alert' : 'status');
    node.setAttribute('aria-live', kind === 'error' ? 'assertive' : 'polite');
    node.setAttribute('aria-atomic', 'true');
    node.className = `state-banner jobs-notice state-${kind}`;
    node.replaceChildren(create('span', '', text));
  }
  function captureActionFocus(controls) {
    return controls.includes(document.activeElement) ? document.activeElement : null;
  }
  function restoreActionFocus(original, action, id, queue = 'jobs') {
    if (!original || (document.activeElement !== document.body && document.activeElement !== original)) return;
    const detail = [...document.querySelectorAll('[data-job-detail]')]
      .find(button => button.dataset.jobDetail === id && (button.dataset.jobQueue || 'jobs') === queue);
    const retry = [...document.querySelectorAll('[data-job-retry]')]
      .find(button => button.dataset.jobRetry === id && (button.dataset.jobQueue || 'jobs') === queue);
    const cancel = [...document.querySelectorAll('[data-job-cancel]')]
      .find(button => button.dataset.jobCancel === id && (button.dataset.jobQueue || 'jobs') === queue);
    const undo = [...document.querySelectorAll('[data-job-undo-dismiss]')]
      .find(button => button.dataset.jobUndoDismiss === id);
    const target = action === 'detail' ? detail : action === 'retry' ? retry : action === 'cancel' ? cancel : undo;
    if (target && !target.disabled) {
      target.focus({ preventScroll: true });
      return;
    }
    const notice = $('#jobsNotice');
    if (notice && !notice.hidden) {
      notice.setAttribute('tabindex', '-1');
      notice.focus({ preventScroll: true });
    }
  }
  function setJobsState(kind, title, description) {
    const node = $('#jobsState');
    node.hidden = false;
    node.className = `catalog-state${kind === 'error' ? ' state-error' : ''}`;
    node.replaceChildren(
      create('span', 'empty-icon', kind === 'loading' ? '◌' : kind === 'error' ? '!' : '◷'),
      create('strong', '', title),
      create('span', '', description)
    );
    $('#jobsTableWrap').hidden = true;
    $('#jobsPagination').hidden = true;
  }
  let detailCounter = 0;
  function appendJobRow(tbody, job) {
    const id = safeId(job.id);
    const queue = job.queue === 'phash' ? 'phash' : 'jobs';
    const row = create('tr', 'job-row');
    row.dataset.jobQueue = queue;
    const jobCell = create('td');
    if (queue === 'phash') {
      jobCell.append(create('strong', 'job-action', safeId(job.title) || 'Video fingerprint and thumbnail'), create('small', 'job-id', `pHash · ${scrub(safeId(job.video_id)) || 'Video ID unavailable'}`));
    } else {
      jobCell.append(create('strong', 'job-action', actionLabel(job.action, job.audit_mode)), create('code', 'job-id', scrub(id) || '—'));
    }
    row.append(jobCell);

    const [stateLabel, stateClass] = statePill(job.state);
    const stateCell = create('td');
    stateCell.append(create('span', `status-pill ${stateClass}`, stateLabel));
    row.append(stateCell);

    row.append(create('td', '', displayTime(job.created_at)));
    row.append(create('td', '', displayTime(job.updated_at)));

    const resultCell = create('td', 'job-result-cell');
    const jobState = safeId(job.state).toLowerCase();
    const failure = jobError(job);
    const phashResult = queue === 'phash'
      ? jobState === 'failed' ? `Failed · ${safeId(job.error_code) || 'details unavailable'}`
        : jobState === 'done' ? `Fingerprint complete · thumbnail ${job.thumbnail_ready ? 'ready' : 'missing'}`
          : `${Number(job.attempts) || 0} attempts · ${formatDuration(job.processing_time_ms, jobState)} processed`
      : '';
    const resultText = failure || phashResult || resultSummary(job)
      || (jobState === 'failed' ? 'Failure details unavailable; open details.' : '—');
    resultCell.append(create('span', `job-result${failure ? ' job-result-failure' : ''}`, resultText));
    row.append(resultCell);

    const actionCell = create('td', 'job-actions');
    const controlId = `job-detail-${++detailCounter}`;
    const detailButton = create('button', 'button button-quiet', 'Details');
    detailButton.type = 'button';
    detailButton.setAttribute('aria-expanded', 'false');
    detailButton.setAttribute('aria-controls', controlId);
    detailButton.dataset.jobDetail = id;
    detailButton.dataset.jobQueue = queue;
    actionCell.append(detailButton);
    if (jobState === 'failed' || jobState === 'cancelled') {
      const retry = create('button', 'button button-quiet job-retry', 'Retry');
      retry.type = 'button';
      retry.dataset.jobRetry = id;
      retry.dataset.jobQueue = queue;
      retry.dataset.jobQueue = queue;
      actionCell.append(retry);
    }
    if (jobState === 'queued' || (queue === 'phash' && jobState === 'processing')) {
      const cancel = create('button', 'button button-quiet job-retry', 'Cancel');
      cancel.type = 'button';
      cancel.dataset.jobCancel = id;
      cancel.dataset.jobQueue = queue;
      cancel.dataset.jobQueue = queue;
      actionCell.append(cancel);
    }
    row.append(actionCell);
    tbody.append(row);

    const detailRow = create('tr', 'job-detail-row');
    detailRow.id = controlId;
    detailRow.dataset.jobId = id;
    detailRow.dataset.jobQueue = queue;
    detailRow.dataset.loaded = 'false';
    detailRow.hidden = true;
    const detailCell = create('td', 'job-detail-cell');
    detailCell.colSpan = 6;
    detailRow.append(detailCell);
    tbody.append(detailRow);
  }

  function renderJobDetail(cell, job) {
    cell.replaceChildren();
    const wrap = create('div', 'job-detail');
    const fields = [
      ['Job ID', scrub(safeId(job.id)) || '—'],
      ['Action', actionLabel(job.action, job.audit_mode)],
      ['State', statePill(job.state)[0]],
      ['Current stage', stageLabel(job.stage)],
      ['Created', displayTime(job.created_at)],
      ['Last activity', displayTime(job.updated_at)],
      ['Undo available', job.undo_available === true ? 'Yes' : job.undo_available === false ? 'No' : 'Unknown']
    ];
    const dl = create('dl', 'detail-fields job-detail-fields');
    fields.forEach(([label, value]) => dl.append(detailField(label, value)));
    const meta = create('section', 'detail-section');
    meta.append(create('h3', '', 'Job details'), dl);
    if (safeId(job.action).toLowerCase() === 'sync') {
      const sourceId = /^telegram:[A-Za-z0-9@+_.-]{1,250}$/.test(safeId(job.source_id)) ? job.source_id : '';
      const sourceLink = create('a', 'button button-quiet', sourceId ? 'Open this source’s sync history' : 'Open Sources sync history');
      sourceLink.href = sourceId
        ? `/admin/v2/?source_id=${encodeURIComponent(sourceId)}#sources`
        : '#sources';
      meta.append(sourceLink);
    }
    const activityWarning = processingActivityWarning(job);
    if (activityWarning) meta.append(create('p', 'detail-description job-stale-warning', activityWarning));
    wrap.append(meta);

    if (job.local_state || job.jellyfin_refresh || job.jellyfin_import || safeId(job.state).toLowerCase() === 'processing') {
      const progress = create('section', 'detail-section');
      const progressFields = create('dl', 'detail-fields job-detail-fields');
      progressFields.append(
        detailField('Local result', localResultLabel(job.local_state)),
        detailField('Jellyfin refresh', jellyfinResultLabel(job.jellyfin_refresh)),
        detailField('Jellyfin import', jellyfinImportLabel(job.jellyfin_import))
      );
      progress.append(create('h3', '', 'Progress and outcome'), progressFields);
      if (safeId(job.state).toLowerCase() === 'processing' && !job.local_state && !job.jellyfin_refresh) {
        progress.append(create('p', 'detail-description', 'This task predates detailed stage tracking or has not reached a recorded checkpoint yet.'));
      }
      const counts = job.progress || {};
      if (Number.isInteger(counts.completed) && Number.isInteger(counts.total) && counts.total > 0) {
        const stage = safeId(counts.stage || job.stage).toLowerCase();
        const unit = METADATA_PROGRESS_STAGES.has(stage) ? 'provider steps completed' : 'files handled';
        progress.append(create('p', 'detail-progress-count', `${counts.completed.toLocaleString('en-US')} of ${counts.total.toLocaleString('en-US')} ${unit}`));
      }
      if (job.jellyfin_refresh) {
        progress.append(create('p', 'detail-description', 'A refresh request being accepted does not confirm that Jellyfin has finished importing the change.'));
      }
      wrap.append(progress);
    }

    const events = Array.isArray(job.events) ? job.events : [];
    if (events.length) {
      const section = create('section', 'detail-section');
      const list = create('ol', 'job-events');
      for (const event of events.slice(-20).reverse()) {
        const item = create('li', 'job-event');
        const count = Number.isInteger(event.completed) && Number.isInteger(event.total) && event.total > 0
          ? ` · ${event.completed.toLocaleString('en-US')} of ${event.total.toLocaleString('en-US')}`
          : '';
        item.append(create('span', 'job-event-stage', stageLabel(event.stage) + count));
        item.append(create('time', 'job-event-time', displayTime(event.updated_at)));
        list.append(item);
      }
      section.append(create('h3', '', 'Recent activity'), list);
      wrap.append(section);
    }

    const error = jobError(job);
    if (error || safeId(job.state).toLowerCase() === 'failed') {
      const section = create('section', 'detail-section');
      section.append(create('h3', '', 'Failure'));
      section.append(create('p', 'job-detail-error', error || 'This job failed. Inspect local worker logs for details.'));
      wrap.append(section);
    }
    const summary = resultSummary(job);
    if (summary) {
      const section = create('section', 'detail-section');
      section.append(create('h3', '', 'Result'), create('p', 'detail-description', summary));
      wrap.append(section);
    }
    const jobState = safeId(job.state).toLowerCase();
    if (safeId(job.action).toLowerCase() === 'dismiss' && jobState === 'done' && job.undo_available === true) {
      const actions = create('div', 'job-detail-actions');
      const undo = create('button', 'button button-quiet', 'Undo dismissal');
      undo.type = 'button';
      undo.dataset.jobUndoDismiss = safeId(job.id);
      actions.append(undo);
      wrap.append(actions);
    }
    if (jobState === 'failed' || jobState === 'cancelled' || jobState === 'queued') {
      const actions = create('div', 'job-detail-actions');
      if (jobState === 'failed' || jobState === 'cancelled') {
        const retry = create('button', 'button button-quiet', 'Retry job');
        retry.type = 'button';
        retry.dataset.jobRetry = safeId(job.id);
        retry.dataset.jobQueue = 'jobs';
        actions.append(retry);
      }
      if (jobState === 'queued') {
        const cancel = create('button', 'button button-quiet', 'Cancel job');
        cancel.type = 'button';
        cancel.dataset.jobCancel = safeId(job.id);
        cancel.dataset.jobQueue = 'jobs';
        actions.append(cancel);
      }
      wrap.append(actions);
    }
    cell.append(wrap);
    updateRetryControls();
  }

  function renderPhashJobDetail(cell, job) {
    cell.replaceChildren();
    const wrap = create('div', 'job-detail');
    const fields = [
      ['Task ID', scrub(job.id) || '—'], ['Queue', 'pHash and thumbnails'],
      ['State', statePill(job.state)[0]], ['Attempts', String(Number(job.attempts) || 0)],
      ['Processing time', formatDuration(job.processing_time_ms, job.state)],
      ['Thumbnail', job.thumbnail_ready ? 'Ready' : 'Missing'],
      ['Worker', state.workers.phash?.available ? 'Available' : 'Unavailable'],
      ['Created', displayTime(job.created_at)], ['Last activity', displayTime(job.updated_at)]
    ];
    const dl = create('dl', 'detail-fields job-detail-fields');
    fields.forEach(([label, value]) => dl.append(detailField(label, value)));
    const section = create('section', 'detail-section');
    section.append(create('h3', '', 'pHash task details'), dl);
    if (job.error_code) section.append(create('p', 'job-detail-error', `Worker status: ${scrub(job.error_code)}`));
    if (job.error_detail) section.append(create('pre', 'job-detail-error-detail', scrub(job.error_detail)));
    const videoId = safeId(job.video_id);
    if (videoId) {
      const link = create('a', 'button button-quiet', 'Open video');
      link.href = `#film/${encodeURIComponent(videoId)}`;
      section.append(link);
    }
    wrap.append(section);
    const taskState = safeId(job.state).toLowerCase();
    if (taskState === 'failed' || taskState === 'cancelled' || taskState === 'queued' || taskState === 'processing') {
      const actions = create('div', 'job-detail-actions');
      if (taskState === 'failed' || taskState === 'cancelled') {
        const retry = create('button', 'button button-quiet', 'Retry task');
        retry.type = 'button'; retry.dataset.jobRetry = safeId(job.id); retry.dataset.jobQueue = 'phash';
        actions.append(retry);
      }
      if (taskState === 'queued' || taskState === 'processing') {
        const cancel = create('button', 'button button-quiet', 'Cancel task');
        cancel.type = 'button'; cancel.dataset.jobCancel = safeId(job.id); cancel.dataset.jobQueue = 'phash';
        actions.append(cancel);
      }
      wrap.append(actions);
    }
    cell.append(wrap);
    updateRetryControls();
  }

  function renderPagination() {
    const pagination = $('#jobsPagination');
    pagination.hidden = false;
    $('#jobsPrev').disabled = state.offset <= 0;
    $('#jobsNext').disabled = state.nextOffset === null;
    const page = Math.floor(state.offset / LIMIT) + 1;
    setText($('#jobsPageInfo'), state.total === null ? `Page ${page}` : `Page ${page} of ${Math.max(1, Math.ceil(state.total / LIMIT))}`);
  }

  function renderJobs(payload) {
    const jobs = Array.isArray(payload.tasks) ? payload.tasks : Array.isArray(payload.jobs) ? payload.jobs : [];
    state.workers = payload.workers || {};
    const phashStatus = $('#phashWorkerStatus');
    if (phashStatus) {
      phashStatus.hidden = state.filter.queue === 'jobs';
      const worker = state.workers.phash || {};
      phashStatus.textContent = worker.available === true
        ? `pHash worker online · ${safeId(worker.worker_version) || 'version unknown'} · heartbeat ${Number.isFinite(worker.age_seconds) ? `${worker.age_seconds}s ago` : 'age unavailable'}`
        : 'pHash worker unavailable. pHash tasks remain queued until it returns.';
    }
    state.list = jobs;
    const total = Number(payload.total);
    state.total = Number.isFinite(total) ? total : null;
    state.truncated = Boolean(payload.truncated);
    const available = !payload.data || payload.data.available !== false;
    const incomplete = Boolean(payload.data && payload.data.incomplete);

    const tbody = $('#jobsRows');
    tbody.replaceChildren();
    detailCounter = 0;

    const hasMore = jobs.length > 0 && (state.truncated || (state.total !== null && state.offset + jobs.length < state.total));
    state.nextOffset = hasMore ? state.offset + jobs.length : null;

    if (!jobs.length) {
      const stateKind = !available ? 'error' : 'empty';
      const title = !available ? 'Job data unavailable' : incomplete ? 'Job history is incomplete' : 'No matching jobs';
      const description = !available
        ? 'The queue could not be loaded. Try refreshing in a moment.'
        : incomplete
          ? 'Some job records could not be loaded. Check the service status and try again.'
          : state.offset > 0 ? 'There are no more jobs on this page.' : 'Try a different state or action filter.';
      setJobsState(stateKind, title, description);
      setFeedback(available ? 'No matching jobs.' : 'Job data may be unavailable.');
      if (state.offset > 0) renderPagination();
      return;
    }

    jobs.forEach(job => appendJobRow(tbody, job));
    $('#jobsState').hidden = true;
    $('#jobsTableWrap').hidden = false;
    updateRetryControls();
    renderPagination();

    const notes = [];
    if (!available) notes.push('Data may be unavailable');
    if (incomplete) notes.push('partial list');
    const totalText = state.total === null ? `${jobs.length} jobs on this page` : `${state.total.toLocaleString('en-US')} jobs`;
    const prefix = notes.length ? `${notes.join(' · ')} · ` : '';
    setFeedback(`${prefix}${totalText}${state.truncated ? ' · more results available' : ''}`);
    if (!available) announce('Showing the last available job data.');
  }

  function expandedJobIds() {
    return [...document.querySelectorAll('.job-detail-row')]
      .filter(row => !row.hidden)
      .map(row => `${row.dataset.jobQueue || 'jobs'}:${row.dataset.jobId}`)
      .filter(Boolean);
  }

  async function restoreExpandedJobs(ids) {
    if (!ids.length) return;
    const wanted = new Set(ids);
    const buttons = [...document.querySelectorAll('[data-job-detail]')]
      .filter(button => wanted.has(`${button.dataset.jobQueue || 'jobs'}:${button.dataset.jobDetail}`));
    await Promise.all(buttons.map(button => toggleDetail(button)));
  }

  function scheduleJobsPoll(delay = POLL_INTERVAL_MS) {
    clearTimeout(state.pollTimer);
    state.pollTimer = null;
    if (!state.entered || state.activeTab !== 'tasks' || !isJobsRoute() || document.hidden) return;
    state.pollTimer = setTimeout(() => {
      state.pollTimer = null;
      loadJobs({ background: true });
    }, delay);
  }

  async function loadJobs({ background = false } = {}) {
    if (!isJobsRoute() || (background && document.hidden)) return;
    writeFiltersToUrl();
    const expandedIds = expandedJobIds();
    if (state.abort) state.abort.abort();
    const controller = new AbortController();
    state.abort = controller;
    const sequence = ++state.seq;
    const params = new URLSearchParams();
    params.set('queue', state.filter.queue);
    if (state.filter.state) params.set('state', state.filter.state);
    if (state.filter.action) params.set('action', state.filter.action);
    params.set('offset', String(state.offset));
    params.set('limit', String(LIMIT));
    if (!background && !state.list.length) setJobsState('loading', 'Loading jobs', 'Fetching the task queue and history…');
    if (!background) setFeedback('Loading jobs…');
    try {
      let payload = await getJson(`${V2_API}/tasks?${params.toString()}`, controller.signal);
      if (sequence !== state.seq) return;
      const linkedJobId = state.directJobId;
      if (linkedJobId) {
        try {
          const detailPayload = await getJson(`${V2_API}/tasks/${state.directJobQueue}/${encodeURIComponent(linkedJobId)}`, controller.signal);
          if (sequence !== state.seq) return;
          const linkedJob = detailPayload.task;
          if (linkedJob && safeId(linkedJob.id) === linkedJobId) {
            const tasks = Array.isArray(payload.tasks) ? [...payload.tasks] : [];
            if (!tasks.some(job => safeId(job.id) === linkedJobId && job.queue === linkedJob.queue)) {
              tasks.unshift(linkedJob);
              tasks.length = Math.min(tasks.length, LIMIT);
            }
            payload = { ...payload, tasks };
          } else {
            setNotice('warning', 'The linked task is no longer available. Showing the task list.');
          }
        } catch {
          if (sequence !== state.seq) return;
          setNotice('warning', 'The linked task could not be loaded. Showing the task list.');
        }
        // Consume the one-shot deep link only after the current request has
        // resolved it. If a refresh supersedes this request, the replacement
        // load must still fetch the linked job.
        if (sequence === state.seq) state.directJobId = '';
      }
      renderJobs(payload);
      if (linkedJobId && state.list.some(job => safeId(job.id) === linkedJobId && job.queue === state.directJobQueue)) {
        const button = [...document.querySelectorAll('[data-job-detail]')]
          .find(item => item.dataset.jobDetail === linkedJobId && (item.dataset.jobQueue || 'jobs') === state.directJobQueue);
        if (button) await toggleDetail(button);
      }
      if (background) {
        setFeedback(`${$('#jobsFeedback').textContent} · Updated ${displayTime(Date.now())}`);
      }
      await restoreExpandedJobs(expandedIds);
      scheduleJobsPoll();
    } catch (error) {
      if (error.name === 'AbortError' || sequence !== state.seq) return;
      if (background && state.list.length) {
        setFeedback('Live refresh failed. Showing the last loaded data.');
        scheduleJobsPoll(30_000);
        return;
      }
      setJobsState('error', 'Could not load jobs', `${error.message} Please try again.`);
      setFeedback('Job list unavailable.');
      scheduleJobsPoll(30_000);
    }
  }

  function operationStateLabel(operation) {
    if (operation.state === 'previewed') return 'Preview ready';
    if (operation.state === 'expired') return 'Preview expired';
    const parts = Object.entries(operation.task_states || {}).map(([state, count]) => `${count} ${titleCase(state).toLowerCase()}`);
    return parts.length ? parts.join(' · ') : 'Queued';
  }

  function setOperationHistoryState(title, description, kind = 'empty') {
    const node = $('#operationHistoryState');
    node.hidden = false;
    node.className = `catalog-state${kind === 'error' ? ' state-error' : ''}`;
    node.replaceChildren(create('span', 'empty-icon', kind === 'error' ? '!' : '◷'), create('strong', '', title), create('span', '', description));
  }

  function renderOperationDetails(row, operation) {
    const detailRow = document.createElement('tr'); detailRow.className = 'operation-history-detail-row';
    const cell = document.createElement('td'); cell.colSpan = 5;
    const list = document.createElement('ul'); list.className = 'operation-history-items';
    for (const item of operation.items || []) {
      const task = operation.jobs.find(entry => entry.video_id === item.video_id);
      const line = document.createElement('li');
      const label = create('strong', '', item.title || 'Untitled');
      const id = create('code', '', item.video_id);
      const status = create('span', '', task ? `${titleCase(task.state)} · Task ${task.job_id}` : operation.state === 'expired' ? 'Preview expired' : 'Previewed');
      line.append(label, id, status); list.append(line);
    }
    cell.append(list); detailRow.append(cell); row.after(detailRow);
  }

  async function toggleOperationDetails(button) {
    const row = button.closest('tr');
    const existing = row.nextElementSibling?.classList.contains('operation-history-detail-row') ? row.nextElementSibling : null;
    if (existing) { existing.remove(); state.openOperationId = null; button.setAttribute('aria-expanded', 'false'); button.textContent = 'Details'; return; }
    button.disabled = true; button.textContent = 'Loading…';
    const detailSequence = ++state.operationDetailSeq;
    try {
      const operation = await getJson(`${V2_API}/operations/${encodeURIComponent(button.dataset.operationId)}`);
      if (detailSequence !== state.operationDetailSeq || !button.isConnected || !row.isConnected) return;
      renderOperationDetails(row, operation.operation);
      state.openOperationId = button.dataset.operationId;
      button.setAttribute('aria-expanded', 'true'); button.textContent = 'Hide details';
    } catch (error) {
      if (detailSequence !== state.operationDetailSeq || !button.isConnected || !row.isConnected) return;
      setText($('#operationHistoryFeedback'), `Could not load operation details: ${error.message}`);
      button.textContent = 'Details';
    } finally {
      button.disabled = false;
      if (button.isConnected && row.isConnected && detailSequence !== state.operationDetailSeq) {
        button.textContent = button.getAttribute('aria-expanded') === 'true' ? 'Hide details' : 'Details';
      }
    }
  }

  async function loadOperationHistory({ background = false } = {}) {
    const sequence = ++state.operationSeq;
    const params = new URLSearchParams({ offset: String(state.operationOffset), limit: String(OPERATION_LIMIT) });
    if (!background) setText($('#operationHistoryFeedback'), 'Loading operation history…');
    try {
      const payload = await getJson(`${V2_API}/operations?${params}`);
      if (sequence !== state.operationSeq) return;
      const rows = $('#operationHistoryRows');
      state.operationDetailSeq += 1;
      rows.replaceChildren();
      state.operationTotal = Number(payload.total) || 0;
      for (const operation of payload.items || []) {
        const row = document.createElement('tr');
        const operationLabels = { metadata_update_batch: 'Bulk metadata update' };
        const type = create('td', 'job-action', operationLabels[operation.operation_type] || titleCase(operation.operation_type));
        type.append(create('code', 'job-id', operation.plan_id));
        const created = create('td', '', displayTime(operation.created_at));
        const count = create('td', '', `${operation.item_count} item${operation.item_count === 1 ? '' : 's'}`);
        const stateCell = create('td', '', operationStateLabel(operation));
        const actions = document.createElement('td');
        const details = create('button', 'button button-quiet', 'Details'); details.type = 'button';
        details.dataset.operationId = operation.plan_id; details.setAttribute('aria-expanded', 'false');
        actions.append(details); row.append(type, created, count, stateCell, actions); rows.append(row);
      }
      $('#operationHistoryState').hidden = state.operationTotal > 0;
      if (!state.operationTotal) setOperationHistoryState('No bulk operations yet', 'Previewed plans and their per-item results will appear here.');
      $('#operationHistoryTableWrap').hidden = state.operationTotal === 0;
      const pagination = $('#operationHistoryPagination'); pagination.hidden = state.operationTotal <= OPERATION_LIMIT;
      $('#operationHistoryPrev').disabled = state.operationOffset <= 0;
      $('#operationHistoryNext').disabled = state.operationOffset + (payload.items || []).length >= state.operationTotal;
      setText($('#operationHistoryPageInfo'), `Page ${Math.floor(state.operationOffset / OPERATION_LIMIT) + 1} of ${Math.max(1, Math.ceil(state.operationTotal / OPERATION_LIMIT))}`);
      setText($('#operationHistoryFeedback'), `${state.operationTotal.toLocaleString('en-US')} saved operation${state.operationTotal === 1 ? '' : 's'}${background ? ` · Updated ${displayTime(Date.now())}` : ''}`);
      if (state.openOperationId) {
        const button = [...rows.querySelectorAll('[data-operation-id]')].find(item => item.dataset.operationId === state.openOperationId);
        if (button) await toggleOperationDetails(button);
        else state.openOperationId = null;
      }
    } catch (error) {
      if (sequence !== state.operationSeq) return;
      setOperationHistoryState('Operation history unavailable', 'Try refreshing the operation history in a moment.', 'error');
      $('#operationHistoryTableWrap').hidden = true;
      setText($('#operationHistoryFeedback'), `Could not load operation history: ${error.message}`);
    }
  }

  async function toggleDetail(button) {
    const row = document.getElementById(button.getAttribute('aria-controls'));
    if (!row) return;
    const expanded = button.getAttribute('aria-expanded') === 'true';
    if (expanded) {
      row.dataset.detailRequestSeq = String((Number(row.dataset.detailRequestSeq) || 0) + 1);
      row.hidden = true;
      button.setAttribute('aria-expanded', 'false');
      button.textContent = 'Details';
      return;
    }
    row.hidden = false;
    button.setAttribute('aria-expanded', 'true');
    button.textContent = 'Hide details';
    if (row.dataset.loaded === 'true') return;
    const requestSeq = (Number(row.dataset.detailRequestSeq) || 0) + 1;
    row.dataset.detailRequestSeq = String(requestSeq);
    const cell = row.firstElementChild;
    cell.replaceChildren(create('p', 'job-detail-loading', 'Loading job details…'));
    const queue = row.dataset.jobQueue === 'phash' ? 'phash' : 'jobs';
    try {
      const payload = await getJson(`${V2_API}/tasks/${queue}/${encodeURIComponent(row.dataset.jobId)}`);
      if (requestSeq !== Number(row.dataset.detailRequestSeq)) return;
      const job = payload.task || payload.job || payload.item || payload.data || payload;
      if (queue === 'phash') renderPhashJobDetail(cell, job);
      else renderJobDetail(cell, job);
      row.dataset.loaded = 'true';
    } catch (error) {
      if (requestSeq !== Number(row.dataset.detailRequestSeq)) return;
      const fallback = state.list.find(item => safeId(item.id) === row.dataset.jobId && (item.queue || 'jobs') === queue);
      if (fallback) {
        if (queue === 'phash') renderPhashJobDetail(cell, fallback);
        else renderJobDetail(cell, fallback);
        cell.append(create('p', 'job-detail-note', `Some details could not be refreshed: ${scrub(error.message)}`));
        row.dataset.loaded = 'true';
      } else {
        cell.replaceChildren(create('p', 'job-detail-error', scrub(error.message)));
      }
    }
  }

  async function retryJob(id, queue = 'jobs') {
    if (!id) return;
    const isPhash = queue === 'phash';
    const controls = [...document.querySelectorAll('[data-job-retry]')].filter(button => button.dataset.jobRetry === id && (button.dataset.jobQueue || 'jobs') === queue);
    const focusedControl = captureActionFocus(controls);
    const labels = controls.map(button => button.textContent);
    let focusAfter = { action: 'retry', id };
    controls.forEach(button => { button.dataset.busy = 'true'; button.disabled = true; button.textContent = 'Retrying…'; });
    setNotice('', '');
    try {
      const path = isPhash ? 'phash-jobs' : 'jobs';
      const payload = await postJson(`${V2_API}/${path}/${encodeURIComponent(id)}/retry`, 'Retry');
      const message = scrub(payload.message) || 'Task re-queued.';
      const newId = scrub(safeId(payload.job_id));
      setNotice('success', `${message}${newId ? ` New job: ${newId}.` : ''}`);
      announce(message);
      focusAfter = { action: 'detail', id: newId || id };
      await loadJobs();
    } catch (error) {
      setNotice('error', error.message);
      announce(error.message);
    } finally {
      controls.forEach((button, index) => { delete button.dataset.busy; if (button.isConnected) button.textContent = labels[index]; });
      updateRetryControls();
      restoreActionFocus(focusedControl, focusAfter.action, focusAfter.id, queue);
    }
  }

  async function cancelJob(id, queue = 'jobs') {
    if (!id || !window.confirm('Cancel this queued job? It can be retried later.')) return;
    const isPhash = queue === 'phash';
    const controls = [...document.querySelectorAll('[data-job-cancel]')].filter(button => button.dataset.jobCancel === id && (button.dataset.jobQueue || 'jobs') === queue);
    const focusedControl = captureActionFocus(controls);
    const labels = controls.map(button => button.textContent);
    let focusAfter = { action: 'cancel', id };
    controls.forEach(button => { button.dataset.busy = 'true'; button.disabled = true; button.textContent = 'Cancelling…'; });
    setNotice('', '');
    try {
      const path = isPhash ? 'phash-jobs' : 'jobs';
      const payload = await postJson(`${V2_API}/${path}/${encodeURIComponent(id)}/cancel`, 'Cancel');
      const message = scrub(payload.message) || 'Task cancelled.';
      setNotice('success', message);
      announce(message);
      focusAfter = { action: 'retry', id };
      await loadJobs();
    } catch (error) {
      setNotice('error', error.message);
      announce(error.message);
    } finally {
      controls.forEach((button, index) => { delete button.dataset.busy; if (button.isConnected) button.textContent = labels[index]; });
      updateRetryControls();
      restoreActionFocus(focusedControl, focusAfter.action, focusAfter.id, queue);
    }
  }

  async function undoDismissal(id) {
    if (!id || !window.confirm('Undo this dismissed suggestion? It will appear in Organize again.')) return;
    const controls = [...document.querySelectorAll('[data-job-undo-dismiss]')]
      .filter(button => button.dataset.jobUndoDismiss === id);
    const focusedControl = captureActionFocus(controls);
    const labels = controls.map(button => button.textContent);
    let focusAfter = { action: 'undo', id };
    controls.forEach(button => { button.dataset.busy = 'true'; button.disabled = true; button.textContent = 'Undoing…'; });
    setNotice('', '');
    try {
      const payload = await postJson(`${V2_API}/duplicates/actions`, 'Undo dismissal', {
        action: 'undismiss', operation_id: id
      });
      const jobId = scrub(safeId(payload.job_id));
      const message = `Dismissal undo queued.${jobId ? ` New task: ${jobId}.` : ''}`;
      setNotice('success', message);
      announce(message);
      focusAfter = { action: 'detail', id: jobId || id };
      await loadJobs();
    } catch (error) {
      setNotice('error', `Could not undo dismissal. ${error.message}`);
      announce(error.message);
    } finally {
      controls.forEach((button, index) => { delete button.dataset.busy; if (button.isConnected) button.textContent = labels[index]; });
      updateRetryControls();
      restoreActionFocus(focusedControl, focusAfter.action, focusAfter.id);
    }
  }

  function sync() {
    const view = $('#jobsView');
    if (!view) return;
    const active = isJobsRoute();
    const tab = active ? jobsTab() : '';
    view.hidden = !active;
    document.querySelectorAll('[data-jobs-pane]').forEach(pane => { pane.hidden = !active || pane.dataset.jobsPane !== tab; });
    document.querySelectorAll('[data-jobs-tab]').forEach(link => {
      if (link.dataset.jobsTab === tab) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    });
    if (!active) {
      state.entered = false;
      state.activeTab = '';
      clearTimeout(state.pollTimer);
      state.pollTimer = null;
      if (state.abort) state.abort.abort();
      return;
    }
    setText($('#crumbCurrent'), 'Jobs & Automation');
    setText($('#operationalEyebrow'), 'OPERATIONS');
    setText($('#operationalTitle'), tab === 'bulk' ? 'Bulk Operations' : tab === 'schedules' ? 'Schedules' : 'Tasks');
    setText($('#operationalIntro'), tab === 'bulk'
      ? 'Review saved bulk plans and per-item outcomes.'
      : tab === 'schedules' ? 'Manage recurring checks and maintenance tasks.'
        : 'Monitor queued, running, and completed work. Updates every 15 seconds while this tab is open.');
    clearTimeout(state.pollTimer);
    state.pollTimer = null;
    const changedTab = state.activeTab !== tab;
    state.entered = true;
    state.activeTab = tab;
    if (tab !== 'tasks' && state.abort) state.abort.abort();
    if (!changedTab) return;
    if (tab === 'tasks') {
      readFiltersFromUrl();
      loadAccess();
      loadJobs();
    } else if (tab === 'bulk') {
      state.operationOffset = 0;
      loadOperationHistory();
    }
  }

  function setupEvents() {
    const form = $('#jobsFilters');
    form.addEventListener('change', () => {
      state.filter.queue = form.elements.namedItem('queue').value;
      state.filter.state = form.elements.namedItem('state').value;
      state.filter.action = form.elements.namedItem('action').value;
      state.offset = 0;
      setNotice('', '');
      commitFiltersToUrl();
      loadJobs();
    });
    $('#jobsClearFilters').addEventListener('click', () => {
      form.reset();
      state.filter = { queue: 'all', state: '', action: '' };
      state.offset = 0;
      setNotice('', '');
      commitFiltersToUrl();
      loadJobs();
    });
    $('#jobsRefresh').addEventListener('click', () => { setNotice('', ''); loadJobs(); });
    $('#jobsPrev').addEventListener('click', () => {
      if (state.offset > 0) { state.offset = Math.max(0, state.offset - LIMIT); commitFiltersToUrl(); loadJobs(); }
    });
    $('#jobsNext').addEventListener('click', () => {
      if (state.nextOffset !== null) { state.offset = state.nextOffset; commitFiltersToUrl(); loadJobs(); }
    });
    $('#jobsRows').addEventListener('click', event => {
      const detail = event.target.closest('[data-job-detail]');
      if (detail) { toggleDetail(detail); return; }
      const retry = event.target.closest('[data-job-retry]');
      if (retry) retryJob(retry.dataset.jobRetry, retry.dataset.jobQueue || 'jobs');
      const cancel = event.target.closest('[data-job-cancel]');
      if (cancel) cancelJob(cancel.dataset.jobCancel, cancel.dataset.jobQueue || 'jobs');
      const undoDismiss = event.target.closest('[data-job-undo-dismiss]');
      if (undoDismiss) undoDismissal(undoDismiss.dataset.jobUndoDismiss);
    });
    $('#operationHistoryRefresh').addEventListener('click', () => loadOperationHistory());
    $('#operationHistoryPrev').addEventListener('click', () => {
      if (state.operationOffset > 0) { state.operationOffset = Math.max(0, state.operationOffset - OPERATION_LIMIT); loadOperationHistory(); }
    });
    $('#operationHistoryNext').addEventListener('click', () => {
      if (state.operationOffset + OPERATION_LIMIT < state.operationTotal) { state.operationOffset += OPERATION_LIMIT; loadOperationHistory(); }
    });
    $('#operationHistoryRows').addEventListener('click', event => {
      const details = event.target.closest('[data-operation-id]');
      if (details) toggleOperationDetails(details);
    });

    document.addEventListener('adminv2:navigate', () => helpers().scheduleSync(sync));
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden && state.entered && state.activeTab === 'tasks') loadJobs({ background: true });
      else scheduleJobsPoll();
    });
    addEventListener('hashchange', () => helpers().scheduleSync(sync));
    addEventListener('popstate', restoreFiltersFromHistory);
  }

  setupEvents();
  sync();
})();
