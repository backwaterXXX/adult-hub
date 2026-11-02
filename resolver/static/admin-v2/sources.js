(() => {
  'use strict';

  const V2_API = '/admin/api/v2';
  const SOURCES_URL = `${V2_API}/sources/list`;
  const SOURCE_SETTINGS_URL = `${V2_API}/sources/settings`;
  const SOURCE_PREVIEW_URL = `${V2_API}/sources/preview`;
  const SOURCE_SAVE_URL = `${V2_API}/sources/save`;
  const SOURCE_CREATE_URL = `${V2_API}/sources`;
  const SOURCE_REMOVE_URL = `${V2_API}/sources/remove`;
  const SOURCE_CHECK_URL = `${V2_API}/sources/check`;
  const HISTORY_URL = `${V2_API}/sources/sync-history?limit=50`;
  const TRIGGER_URL = `${V2_API}/maintenance/sync`;
  const SOURCE_PAGE_SIZE = 50;
  const $ = (selector, root = document) => root.querySelector(selector);

  const state = {
    entered: false, activeTab: '', pendingSync: new Set(), loading: false, seq: 0, abort: null, items: [], page: 0, historyPage: 0, historySeq: 0,
    configAvailable: true, historyAvailable: true, settingsAvailable: false, auditPending: false, overrides: {},
    configRevision: null,
    editorCandidate: null,
    filter: { q: '', status: '', sourceKey: '' },
    sort: { key: 'channel', direction: 'asc' },
    channelHistory: new Map(), historyTruncated: false, accessChecks: new Map(), accessFeedback: '',
    detailItem: null, urlTimer: null, urlEditActive: false
  };

  // Shared helpers live in admin-v2.js and are published through window.AdminV2.
  const helpers = () => window.AdminV2 || {};
  const create = (...args) => helpers().create(...args);
  const setText = (target, value) => helpers().setText(target, value);
  const safeId = value => helpers().safeId(value);
  function itemKey(item) { return `${safeId(item && item.provider)}::${safeId(item && item.id)}`; }
  const announce = message => helpers().announce(message);
  const scrub = value => helpers().scrub(value);
  const titleCase = value => helpers().titleCase(value);
  function isSourcesRoute() {
    const [route, query = ''] = location.hash.replace(/^#/, '').split('?', 2);
    const tab = new URLSearchParams(query).get('tab') || '';
    return route === 'sources' || (route === 'system' && ['sync', 'sync-activity', 'sync_activity', 'history'].includes(tab));
  }
  function sourcesTab() {
    const [route, query = ''] = location.hash.replace(/^#/, '').split('?', 2);
    return route === 'system' || new URLSearchParams(query).get('tab') === 'history' ? 'history' : 'channels';
  }
  function displayTime(value) {
    if (value === null || value === undefined || value === '') return '—';
    const numeric = typeof value === 'number' && value < 100000000000 ? value * 1000 : value;
    const date = new Date(numeric);
    if (Number.isNaN(date.getTime())) return '—';
    const dateLabel = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
    return `${dateLabel} ${new Intl.DateTimeFormat('en-US', { timeStyle: 'short' }).format(date)}`;
  }

  function timeStamp(value) {
    const numeric = typeof value === 'number' && value < 100000000000 ? value * 1000 : value;
    const parsed = new Date(numeric).getTime();
    return Number.isNaN(parsed) ? 0 : parsed;
  }
  function runStamp(run) { return timeStamp(run && (run.ended_at ?? run.started_at)); }

  function buildHistoryIndex(runs) {
    const index = new Map();
    const orderedRuns = [...runs].sort((left, right) => runStamp(right) - runStamp(left));
    for (const run of orderedRuns) {
      if (!run || typeof run !== 'object') continue;
      const when = run.ended_at ?? run.started_at ?? null;
      const entries = Array.isArray(run.sources) ? run.sources : [];
      for (const entry of entries) {
        if (!entry || typeof entry !== 'object') continue;
        const key = safeId(entry.source);
        if (!key || index.has(key)) continue;
        index.set(key, {
          success: entry.success, when,
          added: entry.added, removed: entry.removed, videos: entry.videos
        });
      }
    }
    return index;
  }
  function sourceCandidates(provider, id) {
    const list = [];
    const raw = safeId(id);
    if (raw) {
      list.push(`telegram:${raw}`, raw);
      if (raw.startsWith('-100')) list.push(`telegram:${raw.slice(4)}`, raw.slice(4));
    }
    if (provider) list.push(`${provider}:${raw}`, provider);
    return list;
  }
  function exactChannelCandidates(provider, id) {
    const list = new Set();
    const raw = safeId(id);
    if (raw) {
      if (provider) list.add(`${provider}:${raw}`);
      if (provider === 'telegram') list.add(raw);
      if (provider === 'telegram' && raw.startsWith('-100')) {
        list.add(`telegram:${raw.slice(4)}`);
        list.add(raw.slice(4));
      }
    }
    return [...list];
  }
  function latestForChannel(index, provider, id) {
    for (const key of sourceCandidates(provider, id)) {
      if (index.has(key)) return index.get(key);
    }
    return null;
  }
  function buildChannelHistory(runs) {
    const index = new Map();
    const orderedRuns = [...runs].sort((left, right) => runStamp(right) - runStamp(left));
    for (const run of orderedRuns) {
      if (!run || typeof run !== 'object') continue;
      const when = run.ended_at ?? run.started_at ?? null;
      const entries = Array.isArray(run.sources) ? run.sources : [];
      for (const entry of entries) {
        if (!entry || typeof entry !== 'object') continue;
        const key = safeId(entry.source);
        if (!key) continue;
        if (!index.has(key)) index.set(key, []);
        index.get(key).push({
          when, success: entry.success,
          added: entry.added, removed: entry.removed, videos: entry.videos,
          error: entry.error, detail: entry.detail
        });
      }
    }
    return index;
  }
  function historyFor(item) {
    const seen = new Set();
    const rows = [];
    for (const key of exactChannelCandidates(item.provider, item.id)) {
      const list = state.channelHistory.get(key);
      if (!list) continue;
      for (const row of list) {
        const marker = `${row.when}|${row.success}|${row.videos}|${row.added}|${row.removed}`;
        if (seen.has(marker)) continue;
        seen.add(marker);
        rows.push(row);
      }
    }
    return rows.sort((left, right) => timeStamp(right.when) - timeStamp(left.when));
  }
  function lastSuccessfulSync(item) {
    if (!state.historyAvailable) return 'Unknown';
    const success = historyFor(item).find(row => row.success === true);
    if (success) return displayTime(success.when);
    return state.historyTruncated ? 'Not found in recent history' : 'No successful sync recorded';
  }
  function buildItems(sources, index) {
    const items = [];
    sources.forEach(group => {
      if (!group || typeof group !== 'object') return;
      const provider = safeId(group.provider);
      const sourceEnabled = group.enabled !== false;
      const channels = Array.isArray(group.channels) ? group.channels : [];
      channels.forEach(channel => {
        if (!channel || typeof channel !== 'object') return;
        const id = safeId(channel.id);
        if (!id) return;
        const channelEnabled = channel.enabled !== false;
        items.push({
          provider,
          providerLabel: titleCase(provider || 'source'),
          studio: scrub(channel.studio) || 'Unnamed channel',
          id,
          channelEnabled,
          sourceEnabled,
          enabled: channelEnabled && sourceEnabled,
          effective: readEffectiveSettings(channel, provider),
          sourceDefaults: readProviderDefaults(group, provider),
          latest: state.historyAvailable ? latestForChannel(index, provider, id) : null
        });
      });
      const configuration = group.configuration && typeof group.configuration === 'object' ? group.configuration : null;
      if (!channels.length && provider !== 'telegram' && configuration) {
        const providerLabel = titleCase(provider || 'source');
        items.push({
          provider,
          providerLabel,
          studio: `${providerLabel} configuration`,
          id: provider,
          channelEnabled: sourceEnabled,
          sourceEnabled,
          enabled: sourceEnabled,
          isProviderConfig: true,
          providerConfig: configuration,
          effective: null,
          sourceDefaults: null,
          latest: null
        });
      }
    });
    return items;
  }

  function statusFor(item) {
    if (item.enabled) return ['Enabled', 'pill-good'];
    if (item.channelEnabled && !item.sourceEnabled) return ['Source disabled', 'pill-warning'];
    return ['Disabled', 'pill-neutral'];
  }
  function latestFor(item) {
    if (!state.historyAvailable) return { label: 'History unavailable', cls: 'pill-neutral', when: null, history: false };
    if (!item.latest) return { label: 'Never synced', cls: 'pill-neutral', when: null, never: true };
    if (item.latest.success === true) return { label: 'Succeeded', cls: 'pill-good', when: item.latest.when };
    if (item.latest.success === false) return { label: 'Failed', cls: 'pill-danger', when: item.latest.when };
    return { label: 'Unknown', cls: 'pill-warning', when: item.latest.when };
  }
  function countSummary(latest) {
    if (!latest) return '';
    const parts = [];
    if (latest.added !== null && latest.added !== undefined && Number.isFinite(Number(latest.added)) && Number(latest.added) !== 0) parts.push(`+${Number(latest.added)}`);
    if (latest.removed !== null && latest.removed !== undefined && Number.isFinite(Number(latest.removed)) && Number(latest.removed) !== 0) parts.push(`-${Number(latest.removed)}`);
    return parts.join(' · ');
  }

  function sortValue(item, key) {
    if (key === 'channel') return item.studio || item.id || '';
    if (key === 'provider') return item.providerLabel || item.provider || '';
    if (key === 'status') return statusFor(item)[0];
    if (key === 'videos') {
      const rawVideos = item.latest && item.latest.videos;
      const videos = rawVideos === null || rawVideos === undefined ? Number.NaN : Number(rawVideos);
      return Number.isFinite(videos) ? videos : -1;
    }
    if (key === 'latest') return latestFor(item).label;
    if (key === 'last-sync') return timeStamp(latestFor(item).when);
    return '';
  }
  function compareSourceItems(left, right) {
    const leftValue = sortValue(left, state.sort.key);
    const rightValue = sortValue(right, state.sort.key);
    let result;
    if (typeof leftValue === 'number' && typeof rightValue === 'number') {
      result = leftValue - rightValue;
    } else {
      result = String(leftValue).localeCompare(String(rightValue), undefined,
        { numeric: true, sensitivity: 'base' });
    }
    if (result === 0) {
      result = `${left.provider}::${left.id}`.localeCompare(`${right.provider}::${right.id}`, undefined,
        { numeric: true, sensitivity: 'base' });
    }
    return state.sort.direction === 'desc' ? -result : result;
  }
  function updateSortHeaders() {
    document.querySelectorAll('[data-source-sort]').forEach(button => {
      const active = button.dataset.sourceSort === state.sort.key;
      const header = button.closest('th');
      const indicator = button.querySelector('.table-sort-indicator');
      if (header) header.setAttribute('aria-sort', active
        ? (state.sort.direction === 'asc' ? 'ascending' : 'descending') : 'none');
      if (indicator) indicator.textContent = active
        ? (state.sort.direction === 'asc' ? '↑' : '↓') : '↕';
      const label = button.textContent.replace(/[↑↓↕]/g, '').trim();
      button.setAttribute('aria-label', active
        ? `Sort by ${label}; currently ${state.sort.direction === 'asc' ? 'ascending' : 'descending'}`
        : `Sort by ${label}`);
    });
  }

  function matchesFilter(item) {
    if (state.filter.sourceKey && itemKey(item) !== state.filter.sourceKey) return false;
    const query = state.filter.q.trim().toLowerCase();
    if (query) {
      const haystack = `${item.studio} ${scrub(item.id)} ${item.provider}`.toLowerCase();
      if (!haystack.includes(query)) return false;
    }
    const status = state.filter.status;
    if (!status) return true;
    if (status === 'enabled') return item.enabled;
    if (status === 'disabled') return !item.enabled;
    if (!state.historyAvailable) return false;
    if (status === 'never') return !item.latest;
    if (status === 'succeeded') return Boolean(item.latest && item.latest.success === true);
    if (status === 'failed') return Boolean(item.latest && item.latest.success === false);
    return true;
  }

  function setFeedback(text) {
    const feedback = $('#sourcesFeedback');
    setText(feedback, text);
    if (feedback) feedback.title = text || '';
  }
  function setNotice(kind, message, link) {
    const node = $('#sourcesNotice');
    if (!node) return;
    if (!message && !link) { node.hidden = true; node.replaceChildren(); return; }
    node.hidden = false;
    node.className = `state-banner sources-notice state-${kind}`;
    const children = [];
    if (message) children.push(create('span', '', message));
    if (link) {
      const anchor = create('a', 'notice-link', link.label);
      anchor.href = link.href;
      children.push(anchor);
    }
    node.replaceChildren(...children);
  }
  function setSourcesState(kind, title, description) {
    const node = $('#sourcesState');
    if (!node) return;
    node.hidden = false;
    node.className = `catalog-state${kind === 'error' ? ' state-error' : ''}`;
    node.replaceChildren(
      create('span', 'empty-icon', kind === 'loading' ? '◌' : kind === 'error' ? '!' : '◉'),
      create('strong', '', title),
      create('span', '', description)
    );
    $('#sourcesTableWrap').hidden = true;
    renderPagination(0);
  }

  function appendRow(tbody, item) {
    const latest = latestFor(item);
    const row = create('tr', 'source-row');
    const sourceLabel = scrub(item.studio || item.providerLabel || item.id || 'source');

    const channelCell = create('td');
    channelCell.append(create('strong', 'source-name', item.studio));
    row.append(channelCell);


    const [statusLabel, statusClass] = statusFor(item);
    const statusCell = create('td');
    statusCell.append(create('span', `status-pill ${statusClass}`, statusLabel));
    row.append(statusCell);

    const rawVideos = item.latest && item.latest.videos;
    const videos = rawVideos === null || rawVideos === undefined ? Number.NaN : Number(rawVideos);
    row.append(create('td', 'source-muted', Number.isFinite(videos) ? videos.toLocaleString('en-US') : '—'));

    const resultCell = create('td', 'source-result-cell');
    const result = create('span', 'source-result');
    result.append(create('span', `status-pill ${latest.cls}`, latest.label));
    const summary = countSummary(item.latest);
    if (summary && !latest.never && latest.history !== false) result.append(create('small', '', summary));
    resultCell.append(result);
    row.append(resultCell);

    row.append(create('td', 'source-muted', displayTime(latest.when)));

    const actionCell = create('td', 'source-actions');
    const actionGroup = create('div', 'source-action-group');
    const detailButton = create('button', 'button button-quiet source-detail-button', 'Details');
    detailButton.type = 'button';
    detailButton.dataset.sourceDetail = itemKey(item);
    detailButton.setAttribute('aria-label', `Details for ${sourceLabel}`);
    detailButton.setAttribute('aria-haspopup', 'dialog');
      actionGroup.append(detailButton);
    if (item.enabled && !item.isProviderConfig) {
      const scheduled = create('button', 'button button-quiet source-sync-button', 'Run sync');
      scheduled.type = 'button';
      scheduled.dataset.syncSource = item.id;
      scheduled.dataset.syncMode = 'scheduled';
      scheduled.setAttribute('aria-label', `Run sync for ${sourceLabel}`);
      scheduled.title = 'Run the normal synchronization process for this source.';
      const newest = create('button', 'button button-primary source-sync-button', 'Sync new');
      newest.type = 'button';
      newest.dataset.syncSource = item.id;
      newest.dataset.syncMode = 'new';
      newest.setAttribute('aria-label', `Sync new for ${sourceLabel}`);
      newest.title = 'Fetch the latest channel materials without moving the history cursor.';
      const backfill = create('button', 'button button-quiet source-sync-button', 'Backfill');
      backfill.type = 'button';
      backfill.dataset.syncSource = item.id;
      backfill.dataset.syncMode = 'backfill';
      backfill.setAttribute('aria-label', `Backfill ${sourceLabel}`);
      backfill.title = 'Fetch the next bounded history batch and resume from its saved cursor.';
      actionGroup.append(scheduled, newest, backfill);
    }
    if (item.provider === 'telegram') {
      const check = create('button', 'button button-quiet source-access-button', 'Check access');
      check.type = 'button';
      check.dataset.checkSource = item.id;
      check.setAttribute('aria-label', `Check access for ${sourceLabel}`);
      check.title = 'Checks read access with one message metadata request; no media is downloaded.';
      const previousCheck = state.accessChecks.get(itemKey(item));
      if (previousCheck) applyAccessButtonOutcome(check, item, previousCheck);
      actionGroup.append(check);
    }
    actionCell.append(actionGroup);
    row.append(actionCell);

    tbody.append(row);
  }

  const detailField = (label, value) => helpers().detailField(label, value);
  function isCount(value) {
    return value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value));
  }
  function validNumber(value) {
    if (value === null || value === undefined || value === '' || typeof value === 'boolean') return null;
    const numeric = Number(value);
    return Number.isFinite(numeric) ? numeric : null;
  }
  function numberText(value) {
    const numeric = validNumber(value);
    return numeric === null ? 'Unavailable' : numeric.toLocaleString('en-US');
  }
  function durationText(value) {
    const numeric = validNumber(value);
    if (numeric === null || numeric < 0) return 'Unavailable';
    const total = Math.round(numeric);
    if (total === 0) return '0 s';
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const seconds = total % 60;
    const parts = [];
    if (hours) parts.push(`${hours} h`);
    if (minutes) parts.push(`${minutes} min`);
    if (seconds) parts.push(`${seconds} s`);
    return parts.join(' ');
  }
  function streamModeText(value) {
    const mode = safeId(value).toLowerCase();
    if (mode === 'proxy') return 'Proxy';
    if (mode === 'redirect') return 'Redirect';
    return 'Unknown';
  }
  const SETTING_KEYS = ['stream_mode', 'max_pages', 'min_duration_seconds', 'history_batch_size'];
  function hasTagArrays(source) {
    return Array.isArray(source.tags_whitelist) && Array.isArray(source.tags_blacklist);
  }
  function readEffectiveSettings(channel, provider) {
    if (provider !== 'telegram' || !channel || typeof channel !== 'object') return null;
    if (!SETTING_KEYS.every(key => Object.prototype.hasOwnProperty.call(channel, key))) return null;
    if (!hasTagArrays(channel)) return null;
    return {
      streamMode: channel.stream_mode,
      maxPages: channel.max_pages,
      minDurationSeconds: channel.min_duration_seconds,
      historyBatchSize: channel.history_batch_size,
      tagsWhitelist: channel.tags_whitelist,
      tagsBlacklist: channel.tags_blacklist
    };
  }
  function readProviderDefaults(group, provider) {
    const defaults = group && typeof group.defaults === 'object' ? group.defaults : null;
    if (provider !== 'telegram' || !defaults) return null;
    if (!SETTING_KEYS.every(key => Object.prototype.hasOwnProperty.call(defaults, key))) return null;
    if (!hasTagArrays(defaults)) return null;
    return {
      streamMode: defaults.stream_mode,
      maxPages: defaults.max_pages,
      minDurationSeconds: defaults.min_duration_seconds,
      historyBatchSize: defaults.history_batch_size,
      tagsWhitelist: defaults.tags_whitelist,
      tagsBlacklist: defaults.tags_blacklist
    };
  }
  function tagsNode(value) {
    if (!Array.isArray(value)) return create('span', 'detail-muted', 'Unavailable');
    const cleaned = value
      .filter(tag => typeof tag === 'string' && tag.trim() !== '')
      .map(tag => scrub(tag))
      .filter(tag => tag !== '');
    if (!cleaned.length) return create('span', 'detail-muted', 'None');
    const limit = 20;
    const wrap = create('div', 'detail-tags');
    cleaned.slice(0, limit).forEach(tag => wrap.append(create('span', 'tag-chip', tag)));
    const extra = cleaned.length - limit;
    if (extra > 0) wrap.append(create('span', 'tag-chip tag-chip-more', `+${extra} more`));
    return wrap;
  }
  function detailTagField(label, value) {
    const wrapper = create('div', 'detail-field detail-field-wide');
    wrapper.append(create('dt', '', label));
    const body = create('dd');
    body.append(tagsNode(value));
    wrapper.append(body);
    return wrapper;
  }
  function appendSettingFields(fields, settings) {
    fields.append(detailField('Stream mode', streamModeText(settings.streamMode)));
    fields.append(detailField('Max pages', numberText(settings.maxPages)));
    fields.append(detailField('Minimum duration', durationText(settings.minDurationSeconds)));
    fields.append(detailField('History batch size', numberText(settings.historyBatchSize)));
    fields.append(detailTagField('Tags whitelist', settings.tagsWhitelist));
    fields.append(detailTagField('Tags blacklist', settings.tagsBlacklist));
    return fields;
  }
  function effectiveSettingsSection(item) {
    const section = create('section', 'detail-section');
    section.append(create('h3', '', item.isProviderConfig ? 'Provider configuration' : 'Effective sync settings'));
    if (item.isProviderConfig) {
      const fields = create('dl', 'detail-fields');
      fields.append(detailField('Address configured', item.providerConfig.address_configured === true ? 'Yes' : 'No'));
      fields.append(detailField('Stream mode', streamModeText(item.providerConfig.stream_mode)));
      fields.append(detailField('Max pages', numberText(item.providerConfig.max_pages)));
      fields.append(detailTagField('Sections', item.providerConfig.sections));
      section.append(fields);
      return section;
    }
    if (!item.effective) {
      section.append(create('p', 'detail-description', 'Effective sync settings are not available for this provider.'));
      return section;
    }
    section.append(create('p', 'detail-note', 'Values applied to this channel after source defaults are inherited.'));
    section.append(appendSettingFields(create('dl', 'detail-fields'), item.effective));
    return section;
  }
  function providerDefaultsSection(item) {
    if (!item.sourceDefaults) return null;
    const section = create('section', 'detail-section');
    section.append(create('h3', '', 'Provider defaults'));
    section.append(create('p', 'detail-note', 'Source defaults used when a channel does not override a setting.'));
    section.append(appendSettingFields(create('dl', 'detail-fields'), item.sourceDefaults));
    return section;
  }
  function historyStatus(success) {
    if (success === true) return ['Succeeded', 'pill-good'];
    if (success === false) return ['Failed', 'pill-danger'];
    return ['Unknown', 'pill-warning'];
  }
  function countText(entry) {
    const parts = [];
    if (isCount(entry.videos)) parts.push(`${Number(entry.videos).toLocaleString('en-US')} videos`);
    if (isCount(entry.added)) parts.push(`${Number(entry.added).toLocaleString('en-US')} added`);
    if (isCount(entry.removed)) parts.push(`${Number(entry.removed).toLocaleString('en-US')} removed`);
    return parts.join(' · ');
  }
  function historyItem(entry) {
    const item = create('li', 'source-history-item');
    const head = create('div', 'source-history-head');
    head.append(create('span', 'source-history-time', displayTime(entry.when)));
    const [label, cls] = historyStatus(entry.success);
    head.append(create('span', `status-pill ${cls}`, label));
    item.append(head);
    item.append(create('span', 'source-history-counts', countText(entry) || 'No counts recorded'));
    if (entry.success === false) {
      const error = scrub(entry.error);
      const detail = scrub(entry.detail);
      const explanation = [error, detail].filter(Boolean).join(' — ');
      if (explanation) item.append(create('span', 'source-history-failure', `Failure: ${explanation}`));
    }
    return item;
  }
  function renderSyncRuns(payload) {
    const feedback = $('#sourcesHistoryFeedback');
    const host = $('#sourcesHistoryRows');
    const pagination = $('#sourcesHistoryPagination');
    if (pagination) { pagination.replaceChildren(); pagination.hidden = true; }
    host.replaceChildren();
    if (!payload || payload.available === false || payload.error) {
      feedback.textContent = 'Sync history is unavailable. Recent run details are unknown.';
      return;
    }
    const runs = Array.isArray(payload.runs) ? payload.runs : [];
    if (!runs.length) {
      feedback.textContent = 'No sync runs have been recorded yet.';
      return;
    }
    const total = Number.isInteger(payload.total) && payload.total >= runs.length ? payload.total : runs.length;
    const offset = Number(payload.offset) || 0;
    feedback.textContent = `Showing ${offset + 1}–${offset + runs.length} of ${total} sync runs.`;
    if (pagination && total > 10) {
      pagination.hidden = false;
      const prev = create('button', 'button button-quiet', '← Previous'); prev.type = 'button'; prev.disabled = offset === 0;
      const next = create('button', 'button button-quiet', 'Next →'); next.type = 'button'; next.disabled = offset + runs.length >= total;
      prev.addEventListener('click', () => { state.historyPage--; loadHistoryPage(); });
      next.addEventListener('click', () => { state.historyPage++; loadHistoryPage(); });
      pagination.append(prev, create('span', '', `Page ${state.historyPage + 1} of ${Math.ceil(total / 10)}`), next);
    }
    for (const run of runs) {
      const row = create('article', 'operation-history-row source-sync-run');
      const copy = create('div', 'source-sync-run-copy');
      const started = run.started_at;
      const outcome = run.interrupted === true ? 'Interrupted' : run.success === true ? 'Succeeded' : run.success === false ? 'Partial / failed' : 'Unknown';
      const pill = run.success === true ? 'pill-good' : run.success === false ? 'pill-danger' : 'pill-warning';
      copy.append(create('strong', '', `${outcome} · ${displayTime(started)}`));
      const sourceFilter = scrub(run.source_filter);
      const mode = ['scheduled', 'new', 'backfill'].includes(run.sync_mode) ? run.sync_mode : null;
      const channels = Array.isArray(run.sources) ? run.sources : [];
      const added = channels.reduce((sum, source) => sum + (isCount(source.added) ? Number(source.added) : 0), 0);
      const removed = channels.reduce((sum, source) => sum + (isCount(source.removed) ? Number(source.removed) : 0), 0);
      const failed = channels.filter(source => source.success === false && !source.history_deferred);
      const deferred = channels.filter(source => source.history_deferred === true);
      const facts = [
        `${channels.length} source${channels.length === 1 ? '' : 's'}`,
        `${added.toLocaleString('en-US')} added · ${removed.toLocaleString('en-US')} removed`,
        isCount(run.duration_seconds) ? `${Number(run.duration_seconds).toFixed(1)} s` : null,
      ].filter(Boolean);
      copy.append(create('p', 'source-sync-run-facts', facts.join(' · ')));
      const technical = create('details', 'source-run-details');
      const technicalCopy = create('div');
      technical.append(create('summary', '', 'Channels and HTTP retries'), technicalCopy);
      technicalCopy.append(create('p', 'source-sync-run-details', [mode ? `${mode} mode` : '', sourceFilter ? `Filter: ${sourceFilter}` : '', Number.isInteger(run.managed_config_revision) ? `Config revision ${run.managed_config_revision}` : ''].filter(Boolean).join(' · ')));
      for (const channel of channels) technicalCopy.append(historyItem({ ...channel, when: run.started_at }));
      if (failed.length) {
        const failureText = failed.map(source => {
          const label = scrub(source.label) || scrub(source.source) || 'Source';
          const error = scrub(source.error);
          const detail = scrub(source.detail);
          return `${label}${error ? ` · ${error}` : ''}${detail ? ` — ${detail}` : ''}`;
        }).join('; ');
        technicalCopy.append(create('p', 'source-sync-run-failures', `Failed sources: ${failureText}`));
      }
      if (deferred.length) {
        const labels = deferred.map(source => scrub(source.label) || scrub(source.source) || 'Source').join(', ');
        technicalCopy.append(create('p', 'source-sync-run-details', `History deferred; will resume: ${labels}. Cleanup skipped for these sources.`));
      }
      const stages = run.stages && typeof run.stages === 'object'
        ? Object.entries(run.stages).filter(([, value]) => isCount(value && value.calls) || isCount(value && value.total_seconds))
          .sort((left, right) => Number(right[1].total_seconds || 0) - Number(left[1].total_seconds || 0)).slice(0, 3)
        : [];
      if (stages.length) {
        technicalCopy.append(create('p', 'source-sync-run-details', `Slowest stages: ${stages.map(([name, value]) =>
          `${name.replaceAll('_', ' ')} ${Number(value.total_seconds || 0).toFixed(1)} s (${Number(value.calls || 0)} calls)`
        ).join(' · ')}`));
      }
      const http = run.http && typeof run.http === 'object' ? run.http : {};
      const httpFacts = [
        isCount(http.failed_attempts) ? `${Number(http.failed_attempts)} failed attempts` : null,
        isCount(http.retries) ? `${Number(http.retries)} retries` : null,
        isCount(http.rate_limited) ? `${Number(http.rate_limited)} rate limits` : null,
      ].filter(Boolean);
      if (httpFacts.length) technicalCopy.append(create('p', 'source-sync-run-details', `HTTP: ${httpFacts.join(' · ')}`));
      const jellyfinPoll = run.jellyfin_refresh_poll && typeof run.jellyfin_refresh_poll === 'object'
        ? run.jellyfin_refresh_poll : {};
      const pollFacts = [
        isCount(jellyfinPoll.calls) ? `${Number(jellyfinPoll.calls)} polls` : null,
        isCount(jellyfinPoll.timeouts) ? `${Number(jellyfinPoll.timeouts)} timeouts` : null,
        isCount(jellyfinPoll.total_seconds) ? `${Number(jellyfinPoll.total_seconds).toFixed(1)} s total` : null,
        isCount(jellyfinPoll.max_seconds) ? `${Number(jellyfinPoll.max_seconds).toFixed(1)} s max` : null,
      ].filter(Boolean);
      technicalCopy.append(create('p', 'source-sync-run-details', pollFacts.length
        ? `Jellyfin refresh polling: ${pollFacts.join(' · ')}`
        : 'Jellyfin refresh polling: not recorded for this run'));
      copy.append(technical);
      row.append(copy, create('span', `status-pill ${pill}`, outcome));
      host.append(row);
    }
  }
  function renderSourceDetail(item) {
    const host = $('#sourceDetailContent');
    if (!host) return;
    host.replaceChildren();
    host.dataset.sourceId = item.id;
    host.dataset.sourceProvider = item.provider;
    const [statusLabel, statusClass] = statusFor(item);
    host.append(create('p', 'detail-kicker', 'CHANNEL DETAILS'));
    const title = create('h2', 'detail-title', item.studio);
    title.id = 'sourceDetailTitle';
    host.append(title);
    host.append(create('span', `status-pill detail-state ${statusClass}`, statusLabel));
    const contextualActions = create('div', 'source-actions source-detail-actions');
    if (item.enabled && !item.isProviderConfig) {
      const syncNew = create('button', 'button button-primary source-sync-button', 'Sync new');
      syncNew.type = 'button'; syncNew.id = 'sourceDetailSyncNew';
      syncNew.dataset.syncSource = item.id; syncNew.dataset.syncMode = 'new';
      const backfill = create('button', 'button button-quiet source-sync-button', 'Backfill');
      backfill.type = 'button'; backfill.id = 'sourceDetailBackfill';
      backfill.dataset.syncSource = item.id; backfill.dataset.syncMode = 'backfill';
      contextualActions.append(syncNew, backfill);
    }
    if (item.provider === 'telegram' && !item.isProviderConfig) {
      const check = create('button', 'button button-quiet source-access-button', 'Check access');
      check.type = 'button'; check.id = 'sourceDetailCheckAccess'; check.dataset.checkSource = item.id;
      const outcome = create('span', 'source-access-outcome'); outcome.dataset.accessOutcome = item.id;
      const previousCheck = state.accessChecks.get(itemKey(item));
      if (previousCheck) renderAccessOutcome(outcome, previousCheck); else outcome.hidden = true;
      contextualActions.append(check, outcome);
    }
    if (contextualActions.childElementCount) host.append(contextualActions);
    if (item.provider === 'telegram' && state.settingsAvailable) {
      const edit = create('button', 'button button-primary source-editor-open', 'Edit settings');
      edit.type = 'button';
      edit.dataset.sourceEdit = itemKey(item);
      host.append(edit);
      const remove = create('button', 'button button-quiet source-remove-button', 'Remove channel');
      remove.type = 'button';
      remove.dataset.sourceRemove = item.id;
      remove.disabled = state.auditPending;
      if (state.auditPending) remove.title = 'A pending source audit must finish before source changes.';
      host.append(remove);
    }

    const channelSection = create('section', 'detail-section');
    channelSection.append(create('h3', '', item.isProviderConfig ? 'Provider' : 'Channel'));
    const fields = create('dl', 'detail-fields');
    fields.append(detailField(item.isProviderConfig ? 'Provider key' : 'Channel ID', scrub(item.id) || '—'));
    fields.append(detailField('Provider', item.providerLabel));
    if (!item.isProviderConfig) fields.append(detailField('Channel enabled', item.channelEnabled ? 'Yes' : 'No'));
    fields.append(detailField(item.isProviderConfig ? 'Provider enabled' : 'Source enabled', item.sourceEnabled ? 'Yes' : 'No'));
    fields.append(detailField('Overall status', statusLabel));
    if (!item.isProviderConfig) fields.append(detailField('Last successful sync', lastSuccessfulSync(item)));
    channelSection.append(fields);
    host.append(channelSection);

    host.append(effectiveSettingsSection(item));
    const defaultsSection = providerDefaultsSection(item);
    if (defaultsSection) host.append(defaultsSection);

    const historySection = create('section', 'detail-section');
    historySection.append(create('h3', '', 'Recent sync history'));
    if (!state.historyAvailable) {
      historySection.append(create('p', 'detail-description', 'Sync history is unavailable, so recent results are unknown.'));
    } else {
      const rows = historyFor(item).slice(0, 5);
      if (!rows.length) {
        historySection.append(create('p', 'detail-description', 'No recent syncs were found for this channel in the cached history.'));
      } else {
        historySection.append(create('p', 'detail-description', 'Showing the latest entries found in the 50 most recent available sync runs.'));
        const list = create('ul', 'source-history-list');
        rows.forEach(entry => list.append(historyItem(entry)));
        historySection.append(list);
      }
    }
    host.append(historySection);
  }
  function hasOwnPath(object, path) {
    let current = object;
    for (const key of path) {
      if (!current || typeof current !== 'object' || !Object.prototype.hasOwnProperty.call(current, key)) return false;
      current = current[key];
    }
    return true;
  }
  function setPath(object, path, value) {
    let current = object;
    for (const key of path.slice(0, -1)) current = current[key] ||= {};
    current[path.at(-1)] = value;
  }
  function deletePath(object, path) {
    let current = object;
    const parents = [];
    for (const key of path.slice(0, -1)) {
      if (!current || typeof current !== 'object' || !Object.prototype.hasOwnProperty.call(current, key)) return;
      parents.push([current, key]);
      current = current[key];
    }
    if (!current || typeof current !== 'object') return;
    delete current[path.at(-1)];
    for (const [parent, key] of parents.reverse()) {
      if (Object.keys(parent[key] || {}).length) break;
      delete parent[key];
    }
  }
  function addEditorField(form, labelText, path, value, type = 'text', options = []) {
    const row = create('div', 'source-editor-field');
    const inputId = `sourceEditor_${form.querySelectorAll('.source-editor-input').length}`;
    let input;
    if (type === 'textarea') {
      input = document.createElement('textarea');
      input.rows = 3;
      input.value = Array.isArray(value) ? value.join('\n') : '';
    } else if (type === 'select') {
      input = document.createElement('select');
      options.forEach(([optionValue, optionLabel]) => {
        const option = create('option', '', optionLabel);
        option.value = optionValue;
        input.append(option);
      });
      input.value = safeId(value);
    } else {
      input = document.createElement('input');
      input.type = type;
      if (type === 'checkbox') input.checked = value === true;
      else input.value = value === null || value === undefined ? '' : String(value);
      if (type === 'number') { input.min = path.at(-1) === 'min_duration_seconds' ? '0' : '1'; input.step = '1'; }
    }
    input.className = 'source-editor-input';
    input.id = inputId;
    input.dataset.overlayPath = JSON.stringify(path);
    input.disabled = !hasOwnPath(state.overrides, path);
    const fieldLabel = create('label', 'source-editor-label', labelText);
    fieldLabel.htmlFor = inputId;
    row.append(fieldLabel, input);
    const inherit = document.createElement('input');
    inherit.type = 'checkbox';
    inherit.className = 'source-editor-reset';
    inherit.dataset.resetPath = JSON.stringify(path);
    inherit.checked = !hasOwnPath(state.overrides, path);
    const inheritLabel = create('label', 'source-editor-inherit');
    inheritLabel.append(inherit, create('span', '', 'Use base setting'));
    inherit.addEventListener('change', () => { input.disabled = inherit.checked; invalidateSourcePreview(); });
    row.append(inheritLabel);
    input.addEventListener('input', invalidateSourcePreview);
    input.addEventListener('change', invalidateSourcePreview);
    form.append(row);
  }
  function invalidateSourcePreview() {
    state.editorCandidate = null;
    const save = $('#sourceEditorSave');
    if (save) save.disabled = true;
    const preview = $('#sourceEditorPreview');
    if (preview) preview.replaceChildren(create('p', 'detail-muted', 'Review the updated settings before saving.'));
  }
  function sourceEditorCandidate(form) {
    const candidate = JSON.parse(JSON.stringify(state.overrides || {}));
    const resetFields = [...form.querySelectorAll('.source-editor-reset')];
    for (const input of form.querySelectorAll('.source-editor-input')) {
      const path = JSON.parse(input.dataset.overlayPath);
      const inherit = resetFields.find(field => field.dataset.resetPath === input.dataset.overlayPath);
      if (inherit && inherit.checked) { deletePath(candidate, path); continue; }
      let value;
      if (input.type === 'checkbox') value = input.checked;
      else if (input.type === 'number') value = Number(input.value);
      else if (input.tagName === 'TEXTAREA') value = input.value.split(/[\n,]/).map(tag => tag.trim()).filter(Boolean);
      else value = input.value.trim();
      setPath(candidate, path, value);
    }
    return candidate;
  }
  function renderSourceEditor(item) {
    const host = $('#sourceDetailContent');
    const sourcePath = [item.provider];
    const channelPath = [...sourcePath, 'channels', item.id];
    const channel = item.effective || {};
    const defaults = item.sourceDefaults || {};
    host.replaceChildren();
    host.append(create('p', 'detail-kicker', 'EDIT SOURCE SETTINGS'));
    host.append(create('h2', 'detail-title', item.studio));
    host.append(create('p', 'detail-note', 'Changes are stored as overrides. Clearing an override restores the read-only base value.'));
    const form = document.createElement('form');
    form.id = 'sourceEditorForm';
    form.className = 'source-editor-form';
    const sourceHeading = create('h3', 'detail-section-title', 'Source');
    form.append(sourceHeading);
    addEditorField(form, 'Source enabled', [...sourcePath, 'enabled'], item.sourceEnabled, 'checkbox');
    form.append(create('h3', 'detail-section-title', 'Source defaults'));
    addEditorField(form, 'Default stream mode', [...sourcePath, 'defaults', 'stream_mode'], defaults.streamMode, 'select', [['proxy', 'Proxy'], ['redirect', 'Redirect']]);
    addEditorField(form, 'Default max pages', [...sourcePath, 'defaults', 'max_pages'], defaults.maxPages, 'number');
    addEditorField(form, 'Default minimum duration (seconds)', [...sourcePath, 'defaults', 'min_duration_seconds'], defaults.minDurationSeconds, 'number');
    addEditorField(form, 'Default history batch size', [...sourcePath, 'defaults', 'history_batch_size'], defaults.historyBatchSize, 'number');
    addEditorField(form, 'Default tags whitelist (one per line)', [...sourcePath, 'defaults', 'tags_whitelist'], defaults.tagsWhitelist, 'textarea');
    addEditorField(form, 'Default tags blacklist (one per line)', [...sourcePath, 'defaults', 'tags_blacklist'], defaults.tagsBlacklist, 'textarea');
    form.append(create('h3', 'detail-section-title', 'Channel settings'));
    addEditorField(form, 'Channel enabled', [...channelPath, 'enabled'], item.channelEnabled, 'checkbox');
    addEditorField(form, 'Studio label', [...channelPath, 'studio'], item.studio, 'text');
    addEditorField(form, 'Stream mode', [...channelPath, 'stream_mode'], channel.streamMode, 'select', [['proxy', 'Proxy'], ['redirect', 'Redirect']]);
    addEditorField(form, 'Max pages', [...channelPath, 'max_pages'], channel.maxPages, 'number');
    addEditorField(form, 'Minimum duration (seconds)', [...channelPath, 'min_duration_seconds'], channel.minDurationSeconds, 'number');
    addEditorField(form, 'History batch size', [...channelPath, 'history_batch_size'], channel.historyBatchSize, 'number');
    addEditorField(form, 'Tags whitelist (one per line)', [...channelPath, 'tags_whitelist'], channel.tagsWhitelist, 'textarea');
    addEditorField(form, 'Tags blacklist (one per line)', [...channelPath, 'tags_blacklist'], channel.tagsBlacklist, 'textarea');
    const preview = create('div', 'source-editor-preview');
    preview.id = 'sourceEditorPreview';
    preview.setAttribute('aria-live', 'polite');
    preview.append(create('p', 'detail-muted', 'Preview changes before saving.'));
    const actions = create('div', 'source-editor-actions');
    const back = create('button', 'button button-quiet', 'Back to details');
    back.type = 'button';
    back.dataset.sourceEditorBack = 'true';
    const previewButton = create('button', 'button button-quiet', 'Preview changes');
    previewButton.type = 'button';
    previewButton.dataset.sourceEditorPreview = 'true';
    const save = create('button', 'button button-primary', 'Save settings');
    save.type = 'button';
    save.id = 'sourceEditorSave';
    save.disabled = true;
    actions.append(back, previewButton, save);
    form.append(preview, actions);
    form.addEventListener('submit', event => event.preventDefault());
    host.append(form);
    const panel = panelFor($('#sourceDetailDialog'));
    if (panel) panel.scrollTop = 0;
    previewButton.focus({ preventScroll: true });
  }
  function openAddChannel(opener) {
    if (!state.settingsAvailable || state.configRevision === null) return;
    state.detailItem = null;
    const host = $('#sourceDetailContent');
    host.replaceChildren(
      create('p', 'detail-kicker', 'TELEGRAM SOURCE'),
      create('h2', 'detail-title', 'Add a channel'),
      create('p', 'detail-note', 'Add the Telegram channel ID and a studio label. The ID is immutable after creation; verify it before saving.')
    );
    const form = document.createElement('form');
    form.id = 'sourceCreateForm';
    form.className = 'source-editor-form';
    const id = document.createElement('input'); id.name = 'id'; id.required = true; id.maxLength = 256; id.autocomplete = 'off';
    const studio = document.createElement('input'); studio.name = 'studio'; studio.required = true; studio.maxLength = 160; studio.autocomplete = 'off';
    for (const [labelText, input] of [['Telegram channel ID', id], ['Studio label', studio]]) {
      const label = document.createElement('label'); label.className = 'source-editor-field'; input.className = 'source-editor-input';
      label.append(create('span', '', labelText), input); form.append(label);
    }
    const preview = create('div', 'source-editor-preview'); preview.id = 'sourceCreatePreview'; preview.setAttribute('aria-live', 'polite');
    preview.append(create('p', 'detail-muted', 'Preview the validated configuration change before adding the channel.'));
    const actions = create('div', 'source-editor-actions');
    const back = create('button', 'button button-quiet', 'Cancel'); back.type = 'button'; back.dataset.sourceCreateCancel = 'true';
    const previewButton = create('button', 'button button-quiet', 'Preview'); previewButton.type = 'button'; previewButton.dataset.sourceCreatePreview = 'true';
    const save = create('button', 'button button-primary', 'Add channel'); save.type = 'button'; save.id = 'sourceCreateSave'; save.disabled = true;
    actions.append(back, previewButton, save); form.append(preview, actions); form.addEventListener('submit', event => event.preventDefault()); host.append(form);
    form.addEventListener('input', () => { state.editorCandidate = null; $('#sourceCreateSave').disabled = true; preview.replaceChildren(create('p', 'detail-muted', 'Preview the updated channel details before adding it.')); });
    const dialog = $('#sourceDetailDialog'); window.AdminV2.openDialog(dialog, opener || document.activeElement);
    const panel = panelFor(dialog); if (panel) { panel.focus({ preventScroll: true }); panel.scrollTop = 0; }
    id.focus({ preventScroll: true });
  }
  async function previewNewChannel(button) {
    const form = $('#sourceCreateForm'); if (!form || !form.reportValidity()) return;
    const id = form.elements.id.value.trim(); const studio = form.elements.studio.value.trim();
    state.editorCandidate = null;
    $('#sourceCreateSave').disabled = true;
    const preview = $('#sourceCreatePreview');
    if (state.items.some(item => item.provider === 'telegram' && item.id === id)) {
      preview.replaceChildren(create('p', 'detail-error', 'This Telegram channel ID is already configured. Use its settings instead.'));
      return;
    }
    const candidate = JSON.parse(JSON.stringify(state.overrides || {}));
    candidate.telegram ||= {}; candidate.telegram.channels ||= {};
    const previous = candidate.telegram.channels[id] || {};
    candidate.telegram.channels[id] = { ...previous, enabled: true, studio };
    if (Array.isArray(candidate.telegram.removed_channels)) {
      candidate.telegram.removed_channels = candidate.telegram.removed_channels.filter(channelId => channelId !== id);
      if (!candidate.telegram.removed_channels.length) delete candidate.telegram.removed_channels;
    }
    button.disabled = true;
    preview.replaceChildren(create('p', 'detail-muted', 'Validating the complete configuration…'));
    try {
      const result = await window.AdminV2.postJson(SOURCE_PREVIEW_URL, { expected_revision: state.configRevision, overrides: candidate });
      preview.replaceChildren(create('p', 'detail-muted', result.changes.map(change => `${change.path.join(' / ')}: ${change.after ?? 'None'}`).join(' · ') || 'Configuration is valid.'));
      state.editorCandidate = { id, studio, revision: state.configRevision };
      $('#sourceCreateSave').disabled = false;
    } catch (error) {
      state.editorCandidate = null; $('#sourceCreateSave').disabled = true;
      preview.replaceChildren(create('p', 'detail-error', scrub(error.message) || 'Could not validate this channel.'));
    } finally { button.disabled = false; }
  }
  async function saveNewChannel(button) {
    const candidate = state.editorCandidate; if (!candidate) return;
    button.disabled = true;
    try {
      const result = await window.AdminV2.postJson(SOURCE_CREATE_URL, { expected_revision: candidate.revision, channel: { id: candidate.id, studio: candidate.studio } });
      state.editorCandidate = null; state.configRevision = result.revision; closeSourceDetail();
      setNotice('success', result.audit_pending
        ? 'Telegram channel added. Its audit record is queued until the database is available.'
        : 'Telegram channel added. The next sync will apply it.'); announce('Telegram channel added.'); await loadSources();
      if (state.settingsAvailable && !state.auditPending && state.historyAvailable
          && state.configRevision === result.revision) {
        setNotice('success', 'Telegram channel added. The next sync will apply it.');
      }
    } catch (error) {
      const staleRevision = error.status === 409;
      if (staleRevision) state.editorCandidate = null;
      button.disabled = staleRevision;
      const preview = $('#sourceCreatePreview'); if (preview) preview.replaceChildren(create('p', 'detail-error', scrub(error.message) || 'Could not add this channel. Reload and try again.'));
    }
  }
  async function removeSourceChannel(button) {
    const item = state.detailItem;
    if (!item || item.provider !== 'telegram' || !state.settingsAvailable || !button) return;
    const prompt = `Permanently remove “${item.studio}” from Sources? This deletes its channel-only catalog videos, managed video files and channel-specific sync history. Shared catalog items remain if another active source still provides them. This cannot be undone.`;
    if (!window.confirm(prompt)) return;
    button.disabled = true;
    button.dataset.busy = 'true';
    try {
      const result = await window.AdminV2.postJson(SOURCE_REMOVE_URL, {
        expected_revision: state.configRevision,
        channel_id: item.id,
      });
      state.configRevision = result.revision;
      closeSourceDetail(false);
      await loadSources();
      helpers().showQueued(result, 'Channel deletion', $('#sourcesNotice'));
      announce('Channel removed from future syncs. Video and sync-history deletion is queued.');
    } catch (error) {
      const staleRevision = error.status === 409;
      if (staleRevision) await loadSources();
      button.disabled = staleRevision;
      delete button.dataset.busy;
      setNotice('error', scrub(error.message) || 'Could not remove this channel. Reload and try again.');
      announce('Could not remove this channel.');
    }
  }
  async function previewSourceEditor(button) {
    const form = $('#sourceEditorForm');
    if (!form || !form.reportValidity()) return;
    const candidate = sourceEditorCandidate(form);
    button.disabled = true;
    const preview = $('#sourceEditorPreview');
    preview.replaceChildren(create('p', 'detail-muted', 'Checking the complete source configuration…'));
    try {
      const result = await window.AdminV2.postJson(SOURCE_PREVIEW_URL, {
        expected_revision: state.configRevision,
        overrides: candidate
      });
      preview.replaceChildren();
      if (!result.changes.length) preview.append(create('p', 'detail-muted', result.has_override_changes
        ? 'Effective values are unchanged; this will change which values inherit the base configuration.'
        : 'No effective settings will change.'));
      else {
        const list = create('ul', 'source-editor-diff');
        result.changes.forEach(change => {
          const before = change.before === null || change.before === undefined ? 'None' : JSON.stringify(change.before);
          const after = change.after === null || change.after === undefined ? 'None' : JSON.stringify(change.after);
          list.append(create('li', '', `${change.path.join(' / ')}: ${before} → ${after}`));
        });
        preview.append(list);
      }
      state.editorCandidate = result.has_override_changes ? candidate : null;
      const save = $('#sourceEditorSave');
      if (save) save.disabled = !state.editorCandidate;
    } catch (error) {
      state.editorCandidate = null;
      preview.replaceChildren(create('p', 'detail-error', scrub(error.message) || 'Could not preview settings.'));
    } finally { button.disabled = false; }
  }
  async function saveSourceEditor(button) {
    if (!state.editorCandidate) return;
    button.disabled = true;
    try {
      const result = await window.AdminV2.postJson(SOURCE_SAVE_URL, {
        expected_revision: state.configRevision,
        overrides: state.editorCandidate
      });
      state.overrides = state.editorCandidate;
      state.configRevision = result.revision;
      state.editorCandidate = null;
      closeSourceDetail(false);
      setNotice('success', result.audit_pending
        ? 'Source settings saved. Their audit record is queued until the database is available.'
        : 'Source settings saved. The next sync will apply them.');
      announce(result.audit_pending ? 'Source settings saved; audit record queued.' : 'Source settings saved.');
      await loadSources();
    } catch (error) {
      const staleRevision = error.status === 409;
      if (staleRevision) state.editorCandidate = null;
      const save = $('#sourceEditorSave');
      if (save) save.disabled = staleRevision;
      const preview = $('#sourceEditorPreview');
      if (preview) preview.replaceChildren(create('p', 'detail-error', scrub(error.message) || 'Could not save settings. Reload and try again.'));
    }
  }
  function openSourceDetail(item, opener) {
    const dialog = $('#sourceDetailDialog');
    if (!dialog || !item) return;
    state.detailItem = item;
    renderSourceDetail(item);
    window.AdminV2.openDialog(dialog, opener || document.activeElement);
    const panel = panelFor(dialog);
    if (panel) panel.focus({ preventScroll: true });
    announce(`Channel details for ${item.studio}.`);
  }
  function closeSourceDetail(restoreFocus = true) {
    const dialog = $('#sourceDetailDialog');
    if (!dialog) return;
    window.AdminV2.closeDialog(dialog, restoreFocus);
    state.detailItem = null;
  }
  function panelFor(dialog) {
    return dialog.querySelector('.source-detail-panel');
  }

  function render() {
    const tbody = $('#sourcesRows');
    if (!tbody) return;
    if (state.loading && !state.items.length) return;
    const matching = state.items.filter(matchesFilter).sort(compareSourceItems);
    updateSortHeaders();
    const pageCount = Math.max(1, Math.ceil(matching.length / SOURCE_PAGE_SIZE));
    const requestedPage = state.page;
    state.page = Math.min(state.page, pageCount - 1);
    if (state.page !== requestedPage) writeSourceUrl(true);
    const startIndex = state.page * SOURCE_PAGE_SIZE;
    const visible = matching.slice(startIndex, startIndex + SOURCE_PAGE_SIZE);
    const addChannel = $('#sourcesAddChannel');
    if (addChannel) addChannel.disabled = !state.settingsAvailable || state.configRevision === null;
    tbody.replaceChildren();

    if (!state.items.length) {
      const unavailable = !state.configAvailable;
      setSourcesState(unavailable ? 'error' : 'empty',
        unavailable ? 'Sources unavailable' : 'No sources configured',
        unavailable ? 'The source configuration could not be loaded. Try refreshing in a moment.'
                    : 'No channels are configured. Add channels in configuration to use this screen.');
      setFeedback(unavailable ? 'Source configuration unavailable.' : 'No channels configured.');
      return;
    }
    if (!visible.length) {
      setSourcesState('empty', 'No matching channels', 'Try a different search or clear the status filter.');
      renderPagination(0);
      setFeedback(`No channels match the current filters (${state.items.length} total).`);
      return;
    }

    visible.forEach(item => appendRow(tbody, item));
    $('#sourcesState').hidden = true;
    $('#sourcesTableWrap').hidden = false;
    renderPagination(matching.length);
    setFeedback(state.accessFeedback);
  }

  function renderPagination(total) {
    const host = $('#sourcesPagination');
    if (!host) return;
    host.replaceChildren();
    const pageCount = Math.max(1, Math.ceil(total / SOURCE_PAGE_SIZE));
    host.hidden = pageCount <= 1;
    if (host.hidden) return;
    const previous = create('button', 'button button-quiet', '← Previous');
    previous.type = 'button'; previous.disabled = state.page === 0; previous.dataset.sourcePage = 'previous';
    const info = create('span', '', `Page ${state.page + 1} of ${pageCount}`);
    info.id = 'sourcesPageInfo';
    info.tabIndex = -1;
    const next = create('button', 'button button-quiet', 'Next →');
    next.type = 'button'; next.disabled = state.page >= pageCount - 1; next.dataset.sourcePage = 'next';
    host.append(previous, info, next);
  }

  async function loadHistoryPage() {
    const sequence = ++state.historySeq;
    try {
      const payload = await window.AdminV2.getJson(`${V2_API}/sources/sync-history?limit=10&offset=${state.historyPage * 10}`);
      if (sequence !== state.historySeq || !isSourcesRoute() || sourcesTab() !== 'history') return;
      if (!payload.runs?.length && state.historyPage > 0 && payload.total >= 0) {
        state.historyPage = Math.max(0, Math.ceil(payload.total / 10) - 1); return loadHistoryPage();
      }
      renderSyncRuns(payload);
    } catch (error) { if (sequence === state.historySeq && isSourcesRoute() && sourcesTab() === 'history') renderSyncRuns(null); }
  }

  async function loadSources() {
    if (!isSourcesRoute()) return;
    if (state.abort) state.abort.abort();
    const controller = new AbortController();
    state.abort = controller;
    const sequence = ++state.seq;
    state.loading = true;
    setNotice('', '');
    if (!state.items.length) setSourcesState('loading', 'Loading sources', 'Fetching configured channels and sync history…');
    setFeedback('Loading sources…');
    try {
      const [sourcesResult, historyResult, settingsResult] = await Promise.allSettled([
        window.AdminV2.getJson(SOURCES_URL, controller.signal),
        window.AdminV2.getJson(HISTORY_URL, controller.signal),
        window.AdminV2.getJson(SOURCE_SETTINGS_URL, controller.signal)
      ]);
      if (sequence !== state.seq || !isSourcesRoute() || sourcesTab() !== 'channels') return;
      const historyPayload = historyResult.status === 'fulfilled' ? historyResult.value : null;
      if (sourcesTab() === 'history') loadHistoryPage();
      if (sourcesResult.status === 'rejected') throw sourcesResult.reason;
      const sourcesPayload = sourcesResult.value;
      const settingsPayload = settingsResult.status === 'fulfilled' ? settingsResult.value : null;
      state.configAvailable = !(sourcesPayload && sourcesPayload.available === false);
      state.configRevision = Number.isInteger(sourcesPayload && sourcesPayload.managed_revision)
        ? sourcesPayload.managed_revision : null;
      state.settingsAvailable = Boolean(settingsPayload
        && settingsPayload.revision === state.configRevision
        && settingsPayload.overrides && typeof settingsPayload.overrides === 'object');
      state.auditPending = settingsPayload?.audit_pending === true;
      state.overrides = state.settingsAvailable ? settingsPayload.overrides : {};
      state.historyAvailable = historyResult.status === 'fulfilled' && !(historyPayload && historyPayload.error);
      state.historyTruncated = state.historyAvailable && historyPayload?.truncated === true;
      const runs = Array.isArray(historyPayload && historyPayload.runs) ? historyPayload.runs : [];
      const sources = Array.isArray(sourcesPayload && sourcesPayload.sources) ? sourcesPayload.sources : [];
      state.channelHistory = state.historyAvailable ? buildChannelHistory(runs) : new Map();
      state.items = buildItems(sources, buildHistoryIndex(runs));
      if (!state.configAvailable) setNotice('warning', 'Source configuration could not be loaded. Showing configured channels where available.');
      else if (!state.settingsAvailable) setNotice('warning', 'Source editing is unavailable until settings and revision can be loaded together.');
      else if (state.auditPending) setNotice('warning', 'A source change audit is waiting for the database. It will be retried automatically; new source changes are paused until it is recorded.');
      else if (!state.historyAvailable) setNotice('warning', 'Sync history is unavailable, so the latest results are unknown.');
      render();
    } catch (error) {
      if (error.name === 'AbortError' || sequence !== state.seq || !isSourcesRoute() || sourcesTab() !== 'channels') return;
      setSourcesState('error', 'Could not load sources', `${scrub(error.message) || 'The request failed.'} Please try again.`);
      setFeedback('Source list unavailable.');
    } finally {
      if (sequence === state.seq) state.loading = false;
    }
  }

  async function syncNow(id, mode, button) {
    const item = state.items.find(candidate => candidate.id === id);
    if (!item || !item.enabled || state.pendingSync.has(id)) return;
    state.pendingSync.add(id);
    const sequence = state.seq;
    const original = button.textContent;
    button.disabled = true;
    button.dataset.busy = 'true';
    button.textContent = 'Queueing…';
    setNotice('', '');
    try {
      const payload = await window.AdminV2.postJson(TRIGGER_URL, { source: id, sync_mode: mode });
      if (sequence !== state.seq || !isSourcesRoute()) return;
      if (!helpers().taskLink(payload?.job_id)) throw new Error('The server did not return a task reference.');
      helpers().showQueued(payload, 'Synchronization', $('#sourcesNotice'));
      // Keep the receipt visible when the originating dialog closes.
    } catch (error) {
      if (sequence !== state.seq || !isSourcesRoute()) return;
      setNotice('error', scrub(error.message) || 'Could not queue the sync.');
      announce('Could not queue the sync.');
    } finally {
      state.pendingSync.delete(id);
      delete button.dataset.busy;
      if (button.isConnected) { button.disabled = false; button.textContent = original; }
    }
  }

  async function checkSourceAccess(id, button) {
    const item = state.items.find(candidate => candidate.provider === 'telegram' && candidate.id === id);
    if (!item || !button) return;
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    state.accessFeedback = `${item.studio}: Checking Telegram read access…`;
    setFeedback(state.accessFeedback);
    const resultNode = button.parentElement && button.parentElement.querySelector('.source-access-outcome');
    if (resultNode) {
      resultNode.hidden = false;
      resultNode.dataset.accessStatus = 'checking';
      resultNode.textContent = 'Checking Telegram read access…';
      resultNode.removeAttribute('title');
    }
    try {
      const result = await window.AdminV2.postJson(SOURCE_CHECK_URL, { source_id: id });
      const status = ['accessible', 'inaccessible', 'unknown'].includes(result.status) ? result.status : 'unknown';
      const outcome = {
        status,
        message: scrub(result.message) || 'Telegram could not confirm read access to this channel.',
        checkedAt: result.checked_at,
      };
      state.accessChecks.set(itemKey(item), outcome);
      if (resultNode) renderAccessOutcome(resultNode, outcome);
      applyAccessButtonOutcome(button, item, outcome);
      state.accessFeedback = accessFeedbackText(item, outcome);
      setFeedback(state.accessFeedback);
      announce(`${item.studio}: ${outcome.message} Checked ${displayTime(outcome.checkedAt)}.`);
    } catch (error) {
      const outcome = {
        status: 'error',
        message: scrub(error.message) || 'Could not check Telegram access.',
        checkedAt: null,
      };
      state.accessChecks.set(itemKey(item), outcome);
      if (resultNode) renderAccessOutcome(resultNode, outcome);
      applyAccessButtonOutcome(button, item, outcome);
      state.accessFeedback = accessFeedbackText(item, outcome);
      setFeedback(state.accessFeedback);
      announce(`${item.studio}: could not check Telegram access. ${outcome.message}`);
    } finally {
      button.disabled = false;
      button.removeAttribute('aria-busy');
    }
  }

  function accessStatusLabel(outcome) {
    return ({ accessible: 'Accessible', inaccessible: 'No access', unknown: 'Unknown', error: 'Check failed' })[outcome.status] || 'Unknown';
  }

  function accessFeedbackText(item, outcome) {
    return `${item.studio}: ${accessStatusLabel(outcome)} · ${outcome.message}`;
  }

  function applyAccessButtonOutcome(button, item, outcome) {
    const label = accessStatusLabel(outcome);
    const checked = displayTime(outcome.checkedAt);
    button.dataset.accessStatus = outcome.status;
    button.title = `${label} · ${outcome.message}${checked === '—' ? '' : ` · Checked ${checked}`}`;
    button.setAttribute('aria-label', `Check access for ${item.studio}. Last result: ${label}. ${outcome.message}`);
  }

  function renderAccessOutcome(node, outcome) {
    const labels = {
      accessible: 'Accessible', inaccessible: 'No access', unknown: 'Unknown', error: 'Check failed',
      checking: 'Checking',
    };
    const status = labels[outcome.status] ? outcome.status : 'unknown';
    const label = labels[status];
    const checked = displayTime(outcome.checkedAt);
    node.hidden = false;
    node.dataset.accessStatus = status;
    node.textContent = `${label} · ${outcome.message}${checked === '—' ? '' : ` · Checked ${checked}`}`;
    node.title = outcome.message;
  }

  function restoreSourceUrl() {
    const url = new URL(location.href);
    const params = url.searchParams;
    const deepLink = params.get('source_id');
    if (deepLink) {
      const separator = deepLink.indexOf(':');
      const query = separator >= 0 ? deepLink.slice(separator + 1) : deepLink;
      if (separator > 0) params.set('source_key', `${deepLink.slice(0, separator)}::${query}`);
      else params.delete('source_key');
      if (query) params.set('source_q', query);
      params.delete('source_id');
      url.search = params.toString();
      history.replaceState({}, '', url);
    }

    const current = new URLSearchParams(location.search || '');
    state.filter.q = current.get('source_q') || '';
    const status = current.get('source_status') || '';
    state.filter.status = ['enabled', 'disabled', 'succeeded', 'failed', 'never'].includes(status) ? status : '';
    state.filter.sourceKey = current.get('source_key') || '';
    const rawPage = Number(current.get('source_page'));
    state.page = Number.isSafeInteger(rawPage) && rawPage >= 0 ? rawPage : 0;
    const sort = current.get('source_sort') || 'channel';
    state.sort.key = ['channel', 'provider', 'status', 'videos', 'latest', 'last-sync'].includes(sort)
      ? sort : 'channel';
    state.sort.direction = current.get('source_dir') === 'desc' ? 'desc' : 'asc';
    const queryInput = $('#sourcesQuery');
    const statusInput = $('#sourcesStatusFilter');
    if (queryInput) queryInput.value = state.filter.q;
    if (statusInput) statusInput.value = state.filter.status;
  }

  function writeSourceUrl(replace = false) {
    const url = new URL(location.href);
    const params = url.searchParams;
    for (const key of ['source_id', 'source_q', 'source_status', 'source_key', 'source_page', 'source_sort', 'source_dir']) {
      params.delete(key);
    }
    if (state.filter.q.trim()) params.set('source_q', state.filter.q.trim());
    if (state.filter.status) params.set('source_status', state.filter.status);
    if (state.filter.sourceKey) params.set('source_key', state.filter.sourceKey);
    if (state.page > 0) params.set('source_page', String(state.page));
    if (state.sort.key !== 'channel') params.set('source_sort', state.sort.key);
    if (state.sort.direction === 'desc') params.set('source_dir', 'desc');
    url.search = params.toString();
    if (url.href === location.href) return;
    history[replace ? 'replaceState' : 'pushState']({}, '', url);
  }

  function scheduleSourceUrlUpdate() {
    if (!state.urlEditActive) {
      history.pushState({}, '', location.href);
      state.urlEditActive = true;
    }
    clearTimeout(state.urlTimer);
    state.urlTimer = setTimeout(() => {
      state.urlTimer = null;
      state.urlEditActive = false;
    }, 300);
    writeSourceUrl(true);
  }

  function commitSourceUrl() {
    clearTimeout(state.urlTimer);
    state.urlTimer = null;
    state.urlEditActive = false;
    writeSourceUrl();
  }

  function restoreSourceStateFromHistory() {
    if (!isSourcesRoute()) return;
    clearTimeout(state.urlTimer);
    state.urlTimer = null;
    state.urlEditActive = false;
    restoreSourceUrl();
    render();
  }

  function sync() {
    const view = $('#sourcesView');
    if (!view) return;
    const active = isSourcesRoute();
    const tab = active ? sourcesTab() : '';
    const changed = state.activeTab !== tab;
    view.hidden = !active;
    if (changed) {
      state.seq += 1; state.historySeq += 1;
      state.abort?.abort();
      const dialog = $('#sourceDetailDialog');
      if (dialog?.open) closeSourceDetail(false);
    }
    state.activeTab = tab;
    const channels = $('#sourceChannelsPane'), history = $('#sourceHistoryPane');
    if (channels) channels.hidden = !active || tab !== 'channels';
    if (history) history.hidden = !active || tab !== 'history';
    document.querySelectorAll('[data-sources-tab]').forEach(link => {
      if (link.dataset.sourcesTab === tab) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    });
    if (!active) { state.entered = false; return; }
    setText($('#crumbCurrent'), 'Sources');
    setText($('#operationalEyebrow'), 'SOURCES');
    setText($('#operationalTitle'), 'Sources');
    setText($('#operationalIntro'), tab === 'history' ? 'Synchronization outcomes and details.' : 'Manage channels and request synchronization.');
    if (!state.entered || changed) {
      state.entered = true;
      restoreSourceUrl();
      if (tab === 'history') loadHistoryPage();
      else loadSources();
    }
  }

  function setupEvents() {
    const form = $('#sourcesFilters');
    form.addEventListener('submit', event => event.preventDefault());
    form.addEventListener('input', event => {
      if (event.target.name === 'q') {
        state.filter.q = event.target.value;
        state.filter.sourceKey = '';
        state.page = 0;
        state.accessFeedback = '';
        render();
        scheduleSourceUrlUpdate();
      }
    });
    form.addEventListener('change', event => {
      if (event.target.name === 'status') { state.filter.status = event.target.value; state.page = 0; render(); commitSourceUrl(); }
    });
    $('#sourcesClearFilters').addEventListener('click', () => {
      form.reset();
      state.filter = { q: '', status: '', sourceKey: '' };
      state.page = 0;
      render();
      commitSourceUrl();
    });
    $('#sourcesRefresh').addEventListener('click', () => { setNotice('', ''); return loadSources(); });
    $('#sourcesPagination').addEventListener('click', event => {
      const button = event.target.closest('[data-source-page]');
      if (!button || button.disabled) return;
      const direction = button.dataset.sourcePage;
      state.page += direction === 'next' ? 1 : -1;
      render();
      commitSourceUrl();
      const preferred = $(`[data-source-page="${direction}"]`);
      const fallback = $(`[data-source-page="${direction === 'next' ? 'previous' : 'next'}"]`);
      (preferred && !preferred.disabled ? preferred : fallback && !fallback.disabled ? fallback : $('#sourcesPageInfo'))
        ?.focus({ preventScroll: true });
    });
    $('#sourcesTableWrap').addEventListener('click', event => {
      const button = event.target.closest('[data-source-sort]');
      if (!button) return;
      const key = button.dataset.sourceSort;
      if (state.sort.key === key) {
        state.sort.direction = state.sort.direction === 'asc' ? 'desc' : 'asc';
      } else {
        state.sort = { key, direction: 'asc' };
      }
      state.page = 0;
      render();
      commitSourceUrl();
      button.focus({ preventScroll: true });
    });
    window.addEventListener('popstate', restoreSourceStateFromHistory);
    $('#sourcesAddChannel').addEventListener('click', event => openAddChannel(event.currentTarget));
    $('#sourcesRows').addEventListener('click', event => {
      const detail = event.target.closest('[data-source-detail]');
      if (detail) {
        const item = state.items.find(candidate => itemKey(candidate) === detail.dataset.sourceDetail);
        if (item) openSourceDetail(item, detail);
        return;
      }
      const button = event.target.closest('[data-sync-source]');
      if (button) syncNow(button.dataset.syncSource, button.dataset.syncMode || 'scheduled', button);
      const check = event.target.closest('[data-check-source]');
      if (check) checkSourceAccess(check.dataset.checkSource, check);
    });
    const dialog = $('#sourceDetailDialog');
    if (dialog) {
      dialog.addEventListener('click', event => {
        const sync = event.target.closest('[data-sync-source]');
        if (sync) { syncNow(sync.dataset.syncSource, sync.dataset.syncMode || 'scheduled', sync); return; }
        const check = event.target.closest('[data-check-source]');
        if (check) { checkSourceAccess(check.dataset.checkSource, check); return; }
        if (event.target.closest('[data-source-create-cancel]')) { state.editorCandidate = null; closeSourceDetail(); return; }
        const createPreview = event.target.closest('[data-source-create-preview]'); if (createPreview) { previewNewChannel(createPreview); return; }
        const createSave = event.target.closest('#sourceCreateSave'); if (createSave) { saveNewChannel(createSave); return; }
        const remove = event.target.closest('[data-source-remove]'); if (remove) { removeSourceChannel(remove); return; }
        const edit = event.target.closest('[data-source-edit]');
        if (edit && state.detailItem) { renderSourceEditor(state.detailItem); return; }
        if (event.target.closest('[data-source-editor-back]')) {
          state.editorCandidate = null;
          if (state.detailItem) renderSourceDetail(state.detailItem);
          return;
        }
        const preview = event.target.closest('[data-source-editor-preview]');
        if (preview) { previewSourceEditor(preview); return; }
        const save = event.target.closest('#sourceEditorSave');
        if (save) saveSourceEditor(save);
      });
      dialog.querySelectorAll('[data-source-detail-close]').forEach(button => {
        button.addEventListener('click', () => closeSourceDetail());
      });
      dialog.addEventListener('cancel', event => { event.preventDefault(); closeSourceDetail(); });
      dialog.addEventListener('click', event => { if (event.target === dialog) closeSourceDetail(); });
    }
    document.addEventListener('adminv2:navigate', sync);
  }

  setupEvents();
  sync();
})();
