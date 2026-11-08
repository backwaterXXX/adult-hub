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
  const TELEGRAM_AUTH_STATUS_URL = `${V2_API}/telegram/auth/status`;
  const TELEGRAM_AUTH_START_URL = `${V2_API}/telegram/auth/start`;
  const TELEGRAM_AUTH_CODE_URL = `${V2_API}/telegram/auth/code`;
  const TELEGRAM_AUTH_PASSWORD_URL = `${V2_API}/telegram/auth/password`;
  const TELEGRAM_AUTH_RESEND_URL = `${V2_API}/telegram/auth/resend`;
  const TELEGRAM_AUTH_CANCEL_URL = `${V2_API}/telegram/auth/cancel`;
  const TELEGRAM_ACCOUNTS_URL = `${V2_API}/telegram/accounts`;
  const TELEGRAM_CHANNELS_ACCESS_URL = `${V2_API}/telegram/channels/access`;
  const TELEGRAM_ACCOUNT_ID_PATTERN = /^[a-zA-Z0-9_-]{1,64}$/;

  function isValidTelegramAccountId(id) {
    if (typeof id !== 'string') return false;
    const trimmed = id.trim();
    if (!trimmed) return false;
    if (trimmed.includes('/') || trimmed.includes('\\') || trimmed.includes('..') || trimmed.includes('\0')) return false;
    return TELEGRAM_ACCOUNT_ID_PATTERN.test(trimmed);
  }

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
    detailItem: null, urlTimer: null, urlEditActive: false, telegramAuth: null,
    activeAuthAccountId: 'default', activeWizardMode: null, addAccountDraft: null,
    addAccountPending: false, addAccountToken: null, wizardSeq: 0,
    accountActionToken: null, activeAccountActionId: null
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
  function setSourcesState(kind, title, description, action = null) {
    const node = $('#sourcesState');
    if (!node) return;
    node.hidden = false;
    node.className = `catalog-state${kind === 'error' ? ' state-error' : ''}`;
    const children = [
      create('span', 'empty-icon', kind === 'loading' ? '◌' : kind === 'error' ? '!' : '◉'),
      create('strong', '', title),
      create('span', '', description),
    ];
    if (action) children.push(action);
    node.replaceChildren(...children);
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
      const editName = create('button', 'button button-quiet source-name-edit-open', 'Edit channel name');
      editName.type = 'button';
      editName.dataset.sourceEditName = itemKey(item);
      editName.disabled = state.auditPending;
      if (state.auditPending) editName.title = 'A pending source audit must finish before source changes.';
      host.append(editName);

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
    if (!item.isProviderConfig) fields.append(detailField('Channel name', scrub(item.studio) || '—'));
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
    addEditorField(form, 'Channel name (studio label)', [...channelPath, 'studio'], item.studio, 'text');
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
    preview.append(create('p', 'detail-muted', 'Add the channel immediately, or preview changes before saving.'));
    const actions = create('div', 'source-editor-actions');
    const back = create('button', 'button button-quiet', 'Cancel'); back.type = 'button'; back.dataset.sourceCreateCancel = 'true';
    const previewButton = create('button', 'button button-quiet', 'Preview'); previewButton.type = 'button'; previewButton.dataset.sourceCreatePreview = 'true';
    const save = create('button', 'button button-primary', 'Add channel'); save.type = 'button'; save.id = 'sourceCreateSave';
    actions.append(back, previewButton, save); form.append(preview, actions); form.addEventListener('submit', event => event.preventDefault()); host.append(form);
    form.addEventListener('input', () => { state.editorCandidate = null; preview.replaceChildren(create('p', 'detail-muted', 'Add the channel immediately, or preview changes before saving.')); });
    const dialog = $('#sourceDetailDialog'); window.AdminV2.openDialog(dialog, opener || document.activeElement);
    const panel = panelFor(dialog); if (panel) { panel.focus({ preventScroll: true }); panel.scrollTop = 0; }
    id.focus({ preventScroll: true });
  }
  async function previewNewChannel(button) {
    const form = $('#sourceCreateForm'); if (!form || !form.reportValidity()) return;
    const id = form.elements.id ? form.elements.id.value.trim() : '';
    const studio = form.elements.studio ? form.elements.studio.value.trim() : '';
    state.editorCandidate = null;
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
    } catch (error) {
      state.editorCandidate = null;
      preview.replaceChildren(create('p', 'detail-error', scrub(error.message) || 'Could not validate this channel.'));
    } finally { button.disabled = false; }
  }
  async function saveNewChannel(button) {
    const form = $('#sourceCreateForm'); if (!form || !form.reportValidity()) return;
    const id = form.elements.id ? form.elements.id.value.trim() : '';
    const studio = form.elements.studio ? form.elements.studio.value.trim() : '';
    if (!id || !studio) return;
    const preview = $('#sourceCreatePreview');
    if (state.items.some(item => item.provider === 'telegram' && item.id === id)) {
      if (preview) preview.replaceChildren(create('p', 'detail-error', 'This Telegram channel ID is already configured. Use its settings instead.'));
      return;
    }
    const revision = (state.editorCandidate && state.editorCandidate.id === id && state.editorCandidate.studio === studio)
      ? state.editorCandidate.revision
      : state.configRevision;
    button.disabled = true;
    try {
      const result = await window.AdminV2.postJson(SOURCE_CREATE_URL, { expected_revision: revision, channel: { id, studio } });
      state.editorCandidate = null; state.configRevision = result.revision; closeSourceDetail();
      setNotice('success', result.queued ? result.message : result.audit_pending
        ? 'Telegram channel added. Its audit record is queued until the database is available.'
        : 'Telegram channel added. The next sync will apply it.'); announce(result.queued ? result.message : 'Telegram channel added.'); await loadSources();
      if (typeof CustomEvent === 'function') document.dispatchEvent(new CustomEvent('adminv2:sources-changed'));
      if (state.settingsAvailable && !state.auditPending && state.historyAvailable
          && state.configRevision === result.revision && !result.queued) {
        setNotice('success', 'Telegram channel added. The next sync will apply it.');
      }
    } catch (error) {
      const staleRevision = error.status === 409;
      if (staleRevision) state.editorCandidate = null;
      button.disabled = staleRevision;
      if (preview) preview.replaceChildren(create('p', 'detail-error', scrub(error.message) || 'Could not add this channel. Reload and try again.'));
    } finally {
      if (!button.disabled || !button.closest('dialog')?.open) {
        button.disabled = false;
      }
    }
  }
  function renderChannelNameEditor(item) {
    const host = $('#sourceDetailContent');
    if (!host) return;
    host.replaceChildren(
      create('p', 'detail-kicker', 'TELEGRAM CHANNEL'),
      create('h2', 'detail-title', 'Edit channel name'),
      create('p', 'detail-note', `Update the display name (studio label) for channel ${item.id}.`)
    );
    const form = document.createElement('form');
    form.id = 'sourceEditNameForm';
    form.className = 'source-editor-form';
    const nameInput = document.createElement('input');
    nameInput.name = 'studio';
    nameInput.required = true;
    nameInput.maxLength = 160;
    nameInput.autocomplete = 'off';
    nameInput.className = 'source-editor-input';
    nameInput.value = item.studio || '';
    const label = document.createElement('label');
    label.className = 'source-editor-field';
    label.append(create('span', '', 'Channel name'), nameInput);
    form.append(label);

    const preview = create('div', 'source-editor-preview');
    preview.id = 'sourceEditNamePreview';
    preview.setAttribute('aria-live', 'polite');

    const actions = create('div', 'source-editor-actions');
    const back = create('button', 'button button-quiet', 'Cancel');
    back.type = 'button';
    back.dataset.sourceEditNameCancel = 'true';
    const save = create('button', 'button button-primary', 'Save channel name');
    save.type = 'button';
    save.id = 'sourceEditNameSave';
    actions.append(back, save);
    form.append(preview, actions);
    form.addEventListener('submit', event => event.preventDefault());
    host.append(form);
    const panel = panelFor($('#sourceDetailDialog'));
    if (panel) panel.scrollTop = 0;
    nameInput.focus({ preventScroll: true });
    nameInput.select();
  }
  async function saveChannelName(button) {
    const item = state.detailItem;
    if (!item || !state.settingsAvailable) return;
    const form = $('#sourceEditNameForm');
    if (!form || !form.reportValidity()) return;
    const newStudio = form.elements.studio ? form.elements.studio.value.trim() : '';
    if (!newStudio) return;
    if (newStudio === item.studio) {
      renderSourceDetail(item);
      return;
    }
    const preview = $('#sourceEditNamePreview');
    button.disabled = true;
    try {
      const candidate = JSON.parse(JSON.stringify(state.overrides || {}));
      candidate.telegram ||= {};
      candidate.telegram.channels ||= {};
      const previous = candidate.telegram.channels[item.id] || {};
      candidate.telegram.channels[item.id] = { ...previous, studio: newStudio };
      const removedList = Array.isArray(candidate.telegram.removed_channels) ? candidate.telegram.removed_channels : [];
      const removedSet = new Set(removedList.map(String));
      for (const ch of Object.keys(candidate.telegram.channels)) {
        const clean = ch.replace(/^-100/, '').replace(/^-/, '');
        if (removedSet.has(ch) || removedSet.has(clean)) {
          delete candidate.telegram.channels[ch];
        }
      }

      const result = await window.AdminV2.postJson(SOURCE_SAVE_URL, {
        expected_revision: state.configRevision,
        overrides: candidate
      });
      state.overrides = candidate;
      state.configRevision = result.revision;
      setNotice('success', result.queued ? result.message : result.audit_pending
        ? 'Channel name updated. Its audit record is queued until the database is available.'
        : 'Channel name updated. The next sync will apply it.');
      announce(result.queued ? result.message : 'Channel name updated.');
      await loadSources();
      if (typeof CustomEvent === 'function') document.dispatchEvent(new CustomEvent('adminv2:sources-changed'));
      const updatedItem = state.items.find(candidateItem => itemKey(candidateItem) === itemKey(item));
      if (updatedItem) {
        state.detailItem = updatedItem;
        renderSourceDetail(updatedItem);
      } else {
        closeSourceDetail(false);
      }
    } catch (error) {
      const staleRevision = error.status === 409;
      if (staleRevision) await loadSources();
      button.disabled = staleRevision;
      if (preview) preview.replaceChildren(create('p', 'detail-error', scrub(error.message) || 'Could not update channel name. Reload and try again.'));
    } finally {
      if (!button.disabled || !button.closest('dialog')?.open) {
        button.disabled = false;
      }
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
      if (typeof CustomEvent === 'function') document.dispatchEvent(new CustomEvent('adminv2:sources-changed'));
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
      setNotice('success', result.queued ? result.message : result.audit_pending
        ? 'Source settings saved. Their audit record is queued until the database is available.'
        : 'Source settings saved. The next sync will apply them.');
      announce(result.queued ? result.message : result.audit_pending ? 'Source settings saved; audit record queued.' : 'Source settings saved.');
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
      let action = null;
      if (!unavailable && state.settingsAvailable && state.configRevision !== null) {
        action = create('button', 'button button-primary empty-action-button', 'Add Telegram channel');
        action.type = 'button';
        action.id = 'sourcesEmptyAddChannel';
        action.addEventListener('click', event => openAddChannel(event.currentTarget));
      }
      setSourcesState(
        unavailable ? 'error' : 'empty',
        unavailable ? 'Sources unavailable' : 'No channels configured',
        unavailable
          ? 'The source configuration could not be loaded. Try refreshing in a moment.'
          : 'No Telegram channels are configured yet. Add your first channel through Admin to start synchronizing.',
        action,
      );
      setFeedback(unavailable ? 'Source configuration unavailable.' : 'No channels configured. Add your first channel to start.');
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
      state.telegramAuth = sourcesPayload?.telegram_auth || null;
      renderTelegramAccountCard(state.telegramAuth);
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
      state.loading = false;
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
      const tgAuthDialog = $('#telegramAuthDialog');
      if (tgAuthDialog?.open) closeTelegramAuthWizard();
      const tgActionDialog = $('#telegramAccountActionDialog');
      if (tgActionDialog?.open) closeTelegramAccountActionDialog();
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

  let wizardWriteToken = '';

  function extractErrorMessage(err, fallback = '') {
    const visited = new Set();
    function resolve(value) {
      if (typeof value === 'string') {
        const trimmed = value.trim();
        return (trimmed && trimmed !== '[object Object]') ? trimmed : '';
      }
      if (!value || typeof value !== 'object' || visited.has(value)) {
        return '';
      }
      visited.add(value);
      if (Array.isArray(value)) {
        for (const item of value) {
          const nested = resolve(item);
          if (nested) return nested;
        }
        return '';
      }
      if (value.error !== undefined && value.error !== null) {
        if (typeof value.error === 'string') {
          const trimmed = value.error.trim();
          if (trimmed && trimmed !== '[object Object]') return trimmed;
        } else if (typeof value.error === 'object') {
          const nested = resolve(value.error);
          if (nested) return nested;
        }
      }
      if (typeof value.message === 'string') {
        const trimmed = value.message.trim();
        if (trimmed && trimmed !== '[object Object]') return trimmed;
      }
      if (typeof value.detail === 'string') {
        const trimmed = value.detail.trim();
        if (trimmed && trimmed !== '[object Object]') return trimmed;
      } else if (typeof value.detail === 'object' && value.detail !== null) {
        const nested = resolve(value.detail);
        if (nested) return nested;
      }
      if (typeof value.msg === 'string') {
        const trimmed = value.msg.trim();
        if (trimmed && trimmed !== '[object Object]') return trimmed;
      }
      if (typeof value.description === 'string') {
        const trimmed = value.description.trim();
        if (trimmed && trimmed !== '[object Object]') return trimmed;
      }
      if (typeof value.reason === 'string') {
        const trimmed = value.reason.trim();
        if (trimmed && trimmed !== '[object Object]') return trimmed;
      }
      if (typeof value.title === 'string') {
        const trimmed = value.title.trim();
        if (trimmed && trimmed !== '[object Object]') return trimmed;
      }
      if (typeof value.data === 'object' && value.data !== null) {
        const nested = resolve(value.data);
        if (nested) return nested;
      }
      if (typeof value.cause === 'object' && value.cause !== null) {
        const nested = resolve(value.cause);
        if (nested) return nested;
      }
      return '';
    }
    const extracted = resolve(err);
    const resolvedFallback = (typeof fallback === 'string' && fallback.trim() !== '[object Object]')
      ? fallback.trim()
      : (resolve(fallback) || '');
    const resultMsg = extracted || resolvedFallback;
    return (resultMsg && resultMsg !== '[object Object]') ? resultMsg : '';
  }

  async function requestTelegramAuth(url, payload, method = 'POST') {
    const capabilities = await window.AdminV2.getJson(`${V2_API}/capabilities`);
    if (capabilities.read_only_reason === 'invalid_admin_token') {
      throw new Error('Admin write token configuration is invalid.');
    }
    if (capabilities.read_only_reason === 'admin_disabled') {
      throw new Error('Admin actions are disabled.');
    }
    const headers = new Headers({ 'Accept': 'application/json', 'Content-Type': 'application/json' });
    if (capabilities.token_required === true) {
      if (!wizardWriteToken) {
        wizardWriteToken = (prompt('Enter the admin write token:') || '').trim();
      }
      if (!wizardWriteToken) throw new Error('An admin write token is required.');
      headers.set('Authorization', `Bearer ${wizardWriteToken}`);
    }
    const bodyPayload = { ...(payload || {}) };
    if (!bodyPayload.account_id && state.activeAuthAccountId) {
      bodyPayload.account_id = state.activeAuthAccountId;
    }
    const response = await fetch(url, {
      method,
      headers,
      body: method === 'GET' ? undefined : JSON.stringify(bodyPayload),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) {
      if (response.status === 403 && capabilities.token_required === true) {
        wizardWriteToken = '';
      }
      const floodWait = result.flood_wait_seconds || (result.error && typeof result.error === 'object' ? result.error.flood_wait_seconds : undefined);
      const fallback = floodWait
        ? `Please wait ${floodWait}s before requesting a code.`
        : `Request failed with status ${response.status}`;
      const err = new Error(extractErrorMessage(result, fallback));
      err.status = response.status;
      err.data = result;
      err.flood_wait_seconds = floodWait;
      err.code = result.code || (result.error && typeof result.error === 'object' ? result.error.code : undefined);
      throw err;
    }
    return result;
  }

  function postTelegramAuth(url, payload) {
    return requestTelegramAuth(url, payload, 'POST');
  }

  async function renderTelegramAccountCard(authStatus) {
    const card = $('#telegramAccountCard');
    if (!card) return;
    const intro = $('#telegramAccountIntro');
    const container = $('#telegramAccountsContainer');
    const banner = $('#telegramAccountBanner');

    let accountsPayload = null;
    try {
      accountsPayload = await window.AdminV2.getJson(TELEGRAM_ACCOUNTS_URL);
    } catch (e) {
      // Offline fallback
    }
    const accounts = Array.isArray(accountsPayload?.accounts) ? accountsPayload.accounts : [];

    if (intro) {
      intro.textContent = accounts.length
        ? `Configured Telegram user accounts: ${accounts.length}. Multi-account routing balances stream and history requests automatically.`
        : 'Connect Telegram user accounts to enable channel synchronization and streaming.';
    }

    if (!container) return;

    if (!accounts.length) {
      const emptyNote = create('p', 'detail-note', 'No Telegram accounts configured yet. Click "Add Telegram account" to authenticate your first account.');
      container.replaceChildren(emptyNote);
      return;
    }

    const listWrap = create('div', 'telegram-accounts-wrap');
    for (const acc of accounts) {
      const accCard = create('div', 'telegram-account-card');
      const header = create('div', 'telegram-account-card-header');
      const titleWrap = create('div');
      const title = create('h3', '', acc.label || acc.account_id);
      if (acc.label && acc.label !== acc.account_id) {
        title.appendChild(create('span', 'detail-kicker', ` (${acc.account_id})`));
      }
      titleWrap.appendChild(title);

      const badge = create('span', 'telegram-badge');
      if (acc.status === 'cooling_down') {
        badge.className += ' telegram-badge-warning';
        badge.textContent = `Cooldown (${acc.cooldown_seconds}s)`;
      } else if (acc.status === 'authorized') {
        badge.className += ' telegram-badge-success';
        badge.textContent = 'Connected';
      } else if (acc.status === 'disabled') {
        badge.className += ' telegram-badge-muted';
        badge.textContent = 'Disabled';
      } else if (acc.status === 'unauthorized_session_present') {
        badge.className += ' telegram-badge-warning';
        badge.textContent = 'Session unverified';
      } else if (acc.status === 'in_progress') {
        badge.className += ' telegram-badge-warning';
        badge.textContent = 'In progress';
      } else {
        badge.className += ' telegram-badge-muted';
        badge.textContent = 'Not connected';
      }
      header.append(titleWrap, badge);

      const body = create('div', 'telegram-account-card-body');
      if (acc.phone_mask) {
        body.appendChild(create('span', 'telegram-stat', `Phone: ${acc.phone_mask}`));
      }
      const channelSummary = acc.channel_summary || {};
      const accessibleCount = channelSummary.accessible ?? (acc.channels || []).filter(c => c.access_status === 'accessible').length;
      const inaccessibleCount = channelSummary.inaccessible ?? (acc.channels || []).filter(c => c.access_status === 'no_access' || c.access_status === 'inaccessible').length;
      let channelText = `Verified Channels: ${accessibleCount}`;
      if (inaccessibleCount > 0) {
        channelText += ` (${inaccessibleCount} inaccessible)`;
      }
      body.appendChild(create('span', 'telegram-stat', channelText));

      const metrics = acc.metrics || {};
      const activeOps = metrics.active_ops || 0;
      body.appendChild(create('span', 'telegram-stat', `Active: ${activeOps} ${activeOps === 1 ? 'stream' : 'streams'}`));

      const bytesTransferred = metrics.bytes_transferred || 0;
      const formatBytes = window.AdminV2?.formatBytes || ((b) => `${(b / (1024 * 1024)).toFixed(1)} MB`);
      body.appendChild(create('span', 'telegram-stat', `Transferred: ${formatBytes(bytesTransferred)}`));

      if (typeof metrics.throughput_mb_s === 'number' && metrics.throughput_mb_s > 0) {
        body.appendChild(create('span', 'telegram-stat', `Throughput: ${metrics.throughput_mb_s} MB/s`));
      }
      if (typeof metrics.buffer_init_latency_ms === 'number' && metrics.buffer_init_latency_ms > 0) {
        body.appendChild(create('span', 'telegram-stat', `TTFB: ${Math.round(metrics.buffer_init_latency_ms)} ms`));
      }

      const errorsCount = metrics.errors_count || 0;
      const floodCount = metrics.flood_wait_count || 0;
      if (errorsCount > 0 || floodCount > 0) {
        const errText = floodCount > 0
          ? `Errors: ${errorsCount} (${floodCount} FloodWait)`
          : `Errors: ${errorsCount}`;
        const errSpan = create('span', 'telegram-stat telegram-stat-warning', errText);
        body.appendChild(errSpan);
      }

      const actions = create('div', 'telegram-account-card-actions');

      if (!acc.authorized) {
        const loginBtn = create('button', 'button button-primary', acc.has_session_file ? 'Verify session' : 'Log in');
        loginBtn.type = 'button';
        loginBtn.addEventListener('click', () => {
          if (!isValidTelegramAccountId(acc.account_id)) {
            setNotice('error', 'Invalid account ID.');
            return;
          }
          return openTelegramAuthWizard(loginBtn, acc.account_id);
        });
        actions.appendChild(loginBtn);
      } else {
        const testBtn = create('button', 'button button-quiet', 'Test access');
        testBtn.type = 'button';
        testBtn.addEventListener('click', async () => {
          if (!isValidTelegramAccountId(acc.account_id)) {
            setNotice('error', 'Invalid account ID.');
            return;
          }
          testBtn.disabled = true;
          testBtn.textContent = 'Testing…';
          try {
            const res = await postTelegramAuth(`${TELEGRAM_ACCOUNTS_URL}/${encodeURIComponent(acc.account_id)}/test`, {});
            if (res.ok && res.authorized) {
              setNotice('success', `Account ${acc.label || acc.account_id} verified. Accessible channels: ${Object.values(res.channels || {}).filter(s => s === 'accessible').length}`);
            } else {
              setNotice('warning', `Account check returned status: ${res.status}`);
            }
            await renderTelegramAccountCard();
          } catch (err) {
            setNotice('error', `Test failed: ${err.message}`);
          } finally {
            testBtn.disabled = false;
            testBtn.textContent = 'Test access';
          }
        });
        actions.appendChild(testBtn);
      }

      const renameBtn = create('button', 'button button-quiet', 'Rename');
      renameBtn.type = 'button';
      renameBtn.addEventListener('click', async () => {
        if (!isValidTelegramAccountId(acc.account_id)) {
          setNotice('error', 'Invalid account ID.');
          return;
        }
        const newLabel = prompt('Enter new label for this account:', acc.label || acc.account_id);
        if (newLabel !== null && newLabel.trim()) {
          try {
            await requestTelegramAuth(`${TELEGRAM_ACCOUNTS_URL}/${encodeURIComponent(acc.account_id)}`, { label: newLabel.trim() }, 'PATCH');
            setNotice('success', 'Account renamed.');
            await renderTelegramAccountCard();
          } catch (err) {
            setNotice('error', `Rename failed: ${err.message}`);
          }
        }
      });
      actions.appendChild(renameBtn);

      const toggleBtn = create('button', 'button button-quiet', acc.enabled ? 'Disable' : 'Enable');
      toggleBtn.type = 'button';
      toggleBtn.addEventListener('click', async () => {
        if (!isValidTelegramAccountId(acc.account_id)) {
          setNotice('error', 'Invalid account ID.');
          return;
        }
        try {
          await requestTelegramAuth(`${TELEGRAM_ACCOUNTS_URL}/${encodeURIComponent(acc.account_id)}`, { enabled: !acc.enabled }, 'PATCH');
          setNotice('success', `Account ${acc.enabled ? 'disabled' : 'enabled'}.`);
          await renderTelegramAccountCard();
        } catch (err) {
          setNotice('error', `Toggle failed: ${err.message}`);
        }
      });
      actions.appendChild(toggleBtn);

      const deleteBtn = create('button', 'button button-quiet', 'Remove');
      deleteBtn.type = 'button';
      deleteBtn.addEventListener('click', () => {
        if (!isValidTelegramAccountId(acc.account_id)) {
          setNotice('error', 'Invalid account ID.');
          return;
        }
        openTelegramAccountRemovalDialog(acc, deleteBtn);
      });
      actions.appendChild(deleteBtn);

      accCard.append(header, body, actions);
      listWrap.appendChild(accCard);
    }
    container.replaceChildren(listWrap);
  }

  let authCountdownTimer = null;

  async function openTelegramAddAccountWizard(opener) {
    const dialog = $('#telegramAuthDialog');
    if (!dialog) return;
    state.activeWizardMode = 'add_account';
    state.addAccountDraft = null;
    const sequence = ++state.wizardSeq;
    const title = $('#telegramAuthTitle');
    if (title) title.textContent = 'Add Telegram Account';
    setWizardBanner('', '');
    const body = $('#telegramAuthBody');
    if (body) {
      body.replaceChildren(create('p', 'detail-note', 'Preparing account setup…'));
    }
    window.AdminV2.openDialog(dialog, opener || document.activeElement);

    let defaultId = 'worker_2';
    try {
      const list = await window.AdminV2.getJson(TELEGRAM_ACCOUNTS_URL);
      if (!list?.accounts?.length) {
        defaultId = 'default';
      } else {
        defaultId = `account_${list.accounts.length + 1}`;
      }
    } catch (e) {}

    if (sequence !== state.wizardSeq || state.activeWizardMode !== 'add_account') return;
    renderWizardAddAccountStep(defaultId, opener);
  }

  function renderWizardAddAccountStep(defaultId, opener) {
    const body = $('#telegramAuthBody');
    if (!body) return;
    setWizardBanner('', '');

    const wrap = create('div', 'telegram-auth-view');
    const note = create('p', 'detail-note', 'Configure a new Telegram account identifier and display label before starting login authorization.');
    const form = create('form', 'telegram-auth-form');
    form.id = 'tgAddAccountForm';
    form.noValidate = true;

    const inlineError = create('p', 'detail-error');
    inlineError.id = 'tgAddAccountError';
    inlineError.setAttribute('role', 'alert');
    inlineError.hidden = true;

    const idLabel = create('label');
    idLabel.setAttribute('for', 'tgAccountIdInput');
    idLabel.textContent = 'Account ID';
    const idInput = create('input');
    idInput.id = 'tgAccountIdInput';
    idInput.name = 'account_id';
    idInput.type = 'text';
    idInput.placeholder = 'e.g. worker_2';
    idInput.setAttribute('aria-required', 'true');
    idInput.autocomplete = 'off';
    idInput.value = state.addAccountDraft?.account_id ?? (defaultId || '');
    idLabel.appendChild(idInput);

    const labelLabel = create('label');
    labelLabel.setAttribute('for', 'tgAccountLabelInput');
    labelLabel.textContent = 'Label';
    const labelInput = create('input');
    labelInput.id = 'tgAccountLabelInput';
    labelInput.name = 'label';
    labelInput.type = 'text';
    labelInput.placeholder = 'e.g. Worker 2';
    labelInput.autocomplete = 'off';
    labelInput.value = state.addAccountDraft?.label ?? (defaultId || '');
    labelLabel.appendChild(labelInput);

    const clearError = () => {
      if (!inlineError.hidden) {
        inlineError.hidden = true;
        inlineError.textContent = '';
      }
      idInput.removeAttribute('aria-invalid');
      idInput.removeAttribute('aria-describedby');
    };

    const setInputError = (errorMsg) => {
      inlineError.textContent = errorMsg;
      inlineError.hidden = false;
      idInput.setAttribute('aria-invalid', 'true');
      idInput.setAttribute('aria-describedby', 'tgAddAccountError');
      idInput.focus({ preventScroll: true });
    };

    idInput.addEventListener('input', () => {
      clearError();
      state.addAccountDraft = state.addAccountDraft || {};
      state.addAccountDraft.account_id = idInput.value;
    });

    labelInput.addEventListener('input', () => {
      clearError();
      state.addAccountDraft = state.addAccountDraft || {};
      state.addAccountDraft.label = labelInput.value;
    });

    const actions = create('div', 'operation-dialog-actions');
    const cancelBtn = create('button', 'button button-quiet', 'Cancel');
    cancelBtn.type = 'button';
    cancelBtn.id = 'tgAddAccountCancel';
    cancelBtn.addEventListener('click', () => closeTelegramAuthWizard());

    const continueBtn = create('button', 'button button-primary', state.addAccountPending ? 'Continuing…' : 'Continue');
    continueBtn.type = 'submit';
    continueBtn.id = 'tgAddAccountContinue';
    if (state.addAccountPending) {
      continueBtn.setAttribute('aria-disabled', 'true');
    }

    actions.append(cancelBtn, continueBtn);
    form.append(inlineError, idLabel, labelLabel, actions);
    wrap.append(note, form);
    body.replaceChildren(wrap);

    idInput.focus({ preventScroll: true });
    if (typeof idInput.select === 'function') {
      idInput.select();
    }

    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (state.addAccountPending) return;

      const rawId = idInput.value;
      const normalizedId = rawId ? rawId.trim() : '';

      if (!normalizedId) {
        setInputError('Account ID is required.');
        return;
      }

      if (!isValidTelegramAccountId(normalizedId)) {
        setInputError('Account ID must contain only alphanumeric characters, underscores, and hyphens (1-64 characters).');
        return;
      }

      const rawLabel = labelInput.value;
      const normalizedLabel = (rawLabel && rawLabel.trim()) ? rawLabel.trim() : normalizedId;

      clearError();
      setWizardBanner('', '');

      state.addAccountPending = true;
      const requestToken = Symbol('addAccount');
      state.addAccountToken = requestToken;
      continueBtn.setAttribute('aria-disabled', 'true');
      continueBtn.textContent = 'Continuing…';

      const sequence = ++state.wizardSeq;
      const dialog = $('#telegramAuthDialog');

      try {
        await postTelegramAuth(TELEGRAM_ACCOUNTS_URL, { account_id: normalizedId, label: normalizedLabel });
      } catch (err) {
        if (sequence !== state.wizardSeq || state.activeWizardMode !== 'add_account' || !dialog?.open) {
          setNotice('error', `Could not create account "${normalizedId}": ${err.message}`);
          renderTelegramAccountCard();
          return;
        }
        setInputError(`Could not create account: ${err.message}`);
        continueBtn.removeAttribute('aria-disabled');
        continueBtn.textContent = 'Continue';
        return;
      } finally {
        if (state.addAccountToken === requestToken) {
          state.addAccountPending = false;
          state.addAccountToken = null;
          const currentContinueBtn = $('#tgAddAccountContinue');
          if (currentContinueBtn && currentContinueBtn.getAttribute('aria-disabled') === 'true') {
            currentContinueBtn.removeAttribute('aria-disabled');
            currentContinueBtn.textContent = 'Continue';
          }
        }
      }

      state.addAccountDraft = null;

      if (sequence !== state.wizardSeq || state.activeWizardMode !== 'add_account' || !dialog?.open) {
        setNotice('info', `Account "${normalizedId}" was created. Click "Log in" to authenticate.`);
        renderTelegramAccountCard();
        return;
      }

      try {
        await openTelegramAuthWizard(opener, normalizedId);
      } catch (err) {
        setWizardBanner('error', `Account created, but could not start login wizard: ${err.message}`);
      }
    });
  }

  async function openTelegramAuthWizard(opener, accountId = 'default') {
    const rawTarget = typeof accountId === 'string' ? accountId.trim() : '';
    const validTarget = isValidTelegramAccountId(rawTarget) ? rawTarget : 'default';
    state.activeAuthAccountId = validTarget;
    state.activeWizardMode = 'auth';
    state.addAccountDraft = null;
    const dialog = $('#telegramAuthDialog');
    if (!dialog) return;
    const title = $('#telegramAuthTitle');
    if (title) title.textContent = `Telegram Login: ${state.activeAuthAccountId}`;
    setWizardBanner('', '');
    const sequence = ++state.wizardSeq;
    window.AdminV2.openDialog(dialog, opener || document.activeElement);
    await refreshTelegramAuthWizard(state.activeAuthAccountId, sequence);
  }

  function closeTelegramAuthWizard() {
    const dialog = $('#telegramAuthDialog');
    if (!dialog) return;
    if (authCountdownTimer) {
      clearInterval(authCountdownTimer);
      authCountdownTimer = null;
    }
    state.activeWizardMode = null;
    state.addAccountDraft = null;
    state.wizardSeq = (state.wizardSeq || 0) + 1;
    window.AdminV2.closeDialog(dialog);
    renderTelegramAccountCard();
  }

  function setWizardBanner(type, message) {
    const banner = $('#telegramAuthBanner');
    if (!banner) return;
    const text = extractErrorMessage(message, typeof message === 'string' ? message : '');
    if (!text || text === '[object Object]') {
      banner.hidden = true;
      banner.textContent = '';
      return;
    }
    banner.className = `state-banner state-${type}`;
    banner.textContent = text;
    banner.hidden = false;
  }

  function setAccountActionBanner(type, message) {
    const banner = $('#telegramAccountActionBanner');
    if (!banner) return;
    const text = extractErrorMessage(message, typeof message === 'string' ? message : '');
    if (!text || text === '[object Object]') {
      banner.hidden = true;
      banner.textContent = '';
      return;
    }
    banner.className = `state-banner state-${type}`;
    banner.textContent = text;
    banner.hidden = false;
  }

  function openTelegramAccountRemovalDialog(acc, opener) {
    if (!isValidTelegramAccountId(acc?.account_id)) {
      setNotice('error', 'Invalid account ID.');
      return;
    }
    const dialog = $('#telegramAccountActionDialog');
    if (!dialog) return;

    const actionToken = Symbol('accountRemoval');
    state.accountActionToken = actionToken;
    state.activeAccountActionId = acc.account_id;

    const title = $('#telegramAccountActionTitle');
    if (title) title.textContent = 'Remove Telegram Account';
    setAccountActionBanner('', '');

    const body = $('#telegramAccountActionBody');
    if (!body) return;

    const wrap = create('div', 'telegram-account-action-view');
    const labelText = acc.label && acc.label !== acc.account_id
      ? `account “${acc.label}” (${acc.account_id})`
      : `account “${acc.account_id}”`;
    const note = create('p', 'detail-note',
      `Are you sure you want to remove ${labelText}? Its local session file will be removed. Channels and media files will NOT be deleted.`
    );

    const actions = create('div', 'operation-dialog-actions');
    const cancelBtn = create('button', 'button button-quiet', 'Cancel');
    cancelBtn.type = 'button';
    cancelBtn.id = 'telegramAccountActionCancel';
    cancelBtn.addEventListener('click', () => closeTelegramAccountActionDialog());

    const removeBtn = create('button', 'button button-primary', 'Remove');
    removeBtn.type = 'button';
    removeBtn.id = 'telegramAccountActionConfirm';

    removeBtn.addEventListener('click', async () => {
      if (removeBtn.disabled) return;
      removeBtn.disabled = true;
      removeBtn.textContent = 'Removing…';
      cancelBtn.disabled = true;
      setAccountActionBanner('', '');

      const requestToken = actionToken;
      const targetId = acc.account_id;
      const targetLabel = acc.label || acc.account_id;

      try {
        await requestTelegramAuth(`${TELEGRAM_ACCOUNTS_URL}/${encodeURIComponent(targetId)}?confirm=true`, {}, 'DELETE');
        const isCurrentDialog = (state.accountActionToken === requestToken && state.activeAccountActionId === targetId && dialog?.open);
        if (isCurrentDialog) {
          closeTelegramAccountActionDialog(false);
        }
        setNotice('success', `Account ${targetLabel} removed.`);
        await renderTelegramAccountCard();
        const extantAccountsControl = $('#telegramLoginButton') || $('#telegramAccountsRefreshBtn') || $('#telegramAccountHeading');
        if (extantAccountsControl) {
          const active = document.activeElement;
          if (isCurrentDialog || !active || !active.isConnected || active === document.body) {
            const tag = (extantAccountsControl.tagName || '').toUpperCase();
            if (!['BUTTON', 'A', 'INPUT', 'SELECT', 'TEXTAREA'].includes(tag) && extantAccountsControl.getAttribute('tabindex') === null) {
              extantAccountsControl.setAttribute('tabindex', '-1');
            }
            if (typeof extantAccountsControl.focus === 'function') {
              extantAccountsControl.focus({ preventScroll: true });
            }
          }
        }
      } catch (err) {
        const isCurrentDialog = (state.accountActionToken === requestToken && state.activeAccountActionId === targetId && dialog?.open);
        if (isCurrentDialog) {
          removeBtn.disabled = false;
          removeBtn.textContent = 'Remove';
          cancelBtn.disabled = false;
          setAccountActionBanner('error', extractErrorMessage(err, `Removal failed: ${err.message}`));
        } else {
          setNotice('error', extractErrorMessage(err, `Could not remove account "${targetLabel}": ${err.message}`));
          await renderTelegramAccountCard();
        }
      }
    });

    actions.append(cancelBtn, removeBtn);
    wrap.append(note, actions);
    body.replaceChildren(wrap);

    window.AdminV2.openDialog(dialog, opener || document.activeElement);
    cancelBtn.focus({ preventScroll: true });
  }

  function closeTelegramAccountActionDialog(restoreFocus = true) {
    const dialog = $('#telegramAccountActionDialog');
    if (!dialog) return;
    setAccountActionBanner('', '');
    const body = $('#telegramAccountActionBody');
    if (body) body.replaceChildren();
    state.activeAccountActionId = null;
    state.accountActionToken = null;
    window.AdminV2.closeDialog(dialog, restoreFocus);
  }

  async function refreshTelegramAuthWizard(accountId, sequence) {
    const rawTarget = typeof accountId === 'string' ? accountId.trim() : '';
    const target = isValidTelegramAccountId(rawTarget) ? rawTarget : (isValidTelegramAccountId(state.activeAuthAccountId) ? state.activeAuthAccountId : 'default');
    state.activeAuthAccountId = target;
    setWizardBanner('', '');
    const body = $('#telegramAuthBody');
    if (!body) return;

    const seq = sequence !== undefined ? sequence : ++state.wizardSeq;

    body.replaceChildren(create('p', 'detail-note', `Checking Telegram authorization status for ${target}…`));

    let statusPayload = null;
    try {
      statusPayload = await window.AdminV2.getJson(`${TELEGRAM_AUTH_STATUS_URL}?account_id=${encodeURIComponent(target)}&check_live=false`);
    } catch (err) {
      if (seq !== state.wizardSeq || state.activeWizardMode !== 'auth') return;
      body.replaceChildren(create('p', 'detail-error', `Could not check Telegram status: ${scrub(err.message)}`));
      return;
    }

    if (seq !== state.wizardSeq || state.activeWizardMode !== 'auth') return;

    state.telegramAuth = statusPayload;

    if (statusPayload.status === 'authorized') {
      renderWizardAuthorized(body);
    } else if (statusPayload.status === 'unauthorized_session_present') {
      renderWizardUnauthorizedPresent(body, statusPayload.message);
    } else if (statusPayload.status === 'session_file_present') {
      renderWizardSessionPresent(body, statusPayload.message);
    } else if (statusPayload.status === 'unconfigured') {
      renderWizardUnconfigured(body, statusPayload.message);
    } else if (statusPayload.status === 'in_progress') {
      if (statusPayload.login_state === 'password_needed') {
        renderWizardPasswordStep(body);
      } else {
        renderWizardCodeStep(body, statusPayload.phone_mask, statusPayload.expires_in_seconds);
      }
    } else {
      renderWizardPhoneStep(body);
    }
  }

  function renderWizardAuthorized(body) {
    const wrap = create('div', 'telegram-auth-view');
    const msg = create('p', 'detail-note', '✓ Your Telegram account is already authorized and active. To protect existing data, re-authentication is not permitted.');
    const actions = create('div', 'operation-dialog-actions');
    const closeBtn = create('button', 'button button-primary', 'Close');
    closeBtn.type = 'button';
    closeBtn.addEventListener('click', closeTelegramAuthWizard);
    actions.appendChild(closeBtn);
    wrap.append(msg, actions);
    body.replaceChildren(wrap);
  }

  function renderWizardUnauthorizedPresent(body, message) {
    const wrap = create('div', 'telegram-auth-view');
    const err = create('p', 'detail-error', message || 'Existing session file is present but unauthorized. Adult Hub will not overwrite it automatically. Please back up or remove the invalid file manually.');
    const actions = create('div', 'operation-dialog-actions');
    const closeBtn = create('button', 'button button-primary', 'Close');
    closeBtn.type = 'button';
    closeBtn.addEventListener('click', closeTelegramAuthWizard);
    actions.appendChild(closeBtn);
    wrap.append(err, actions);
    body.replaceChildren(wrap);
  }

  function renderWizardSessionPresent(body, message) {
    const wrap = create('div', 'telegram-auth-view');
    const note = create('p', 'detail-note', message || 'An existing Telegram session file is present on the server. You can verify its authorization status without replacing or re-authenticating.');
    const actions = create('div', 'operation-dialog-actions');
    const cancelBtn = create('button', 'button button-quiet', 'Cancel');
    cancelBtn.type = 'button';
    cancelBtn.addEventListener('click', closeTelegramAuthWizard);

    const verifyBtn = create('button', 'button button-primary', 'Verify session');
    verifyBtn.type = 'button';
    verifyBtn.addEventListener('click', async () => {
      const expectedSeq = state.wizardSeq;
      const target = isValidTelegramAccountId(state.activeAuthAccountId) ? state.activeAuthAccountId : 'default';
      verifyBtn.disabled = true;
      verifyBtn.textContent = 'Verifying…';
      try {
        const liveStatus = await window.AdminV2.getJson(`${TELEGRAM_AUTH_STATUS_URL}?account_id=${encodeURIComponent(target)}&check_live=true`);
        if (expectedSeq !== state.wizardSeq || state.activeWizardMode !== 'auth' || state.activeAuthAccountId !== target) {
          return;
        }
        state.telegramAuth = liveStatus;
        renderTelegramAccountCard(liveStatus);
        if (liveStatus.status === 'authorized') {
          renderWizardAuthorized(body);
        } else if (liveStatus.status === 'unauthorized_session_present') {
          renderWizardUnauthorizedPresent(body, liveStatus.message);
        } else {
          setWizardBanner('warning', liveStatus.message || 'Session verification inconclusive.');
          verifyBtn.disabled = false;
          verifyBtn.textContent = 'Verify session';
        }
      } catch (err) {
        if (expectedSeq !== state.wizardSeq || state.activeWizardMode !== 'auth' || state.activeAuthAccountId !== target) {
          return;
        }
        setWizardBanner('error', err.message || 'Failed to verify session.');
        verifyBtn.disabled = false;
        verifyBtn.textContent = 'Verify session';
      }
    });

    actions.append(cancelBtn, verifyBtn);
    wrap.append(note, actions);
    body.replaceChildren(wrap);
  }

  function renderWizardUnconfigured(body, message) {
    const wrap = create('div', 'telegram-auth-view');
    const err = create('p', 'detail-error', message || 'TELEGRAM_API_ID and TELEGRAM_API_HASH are not configured in the server environment.');
    const actions = create('div', 'operation-dialog-actions');
    const closeBtn = create('button', 'button button-primary', 'Close');
    closeBtn.type = 'button';
    closeBtn.addEventListener('click', closeTelegramAuthWizard);
    actions.appendChild(closeBtn);
    wrap.append(err, actions);
    body.replaceChildren(wrap);
  }

  function createStepIndicator(activeStep, show2fa) {
    const nav = create('div', 'telegram-auth-steps');
    const step1 = create('div', `telegram-auth-step ${activeStep === 1 ? 'active' : activeStep > 1 ? 'done' : ''}`, '1. Phone number');
    const step2 = create('div', `telegram-auth-step ${activeStep === 2 ? 'active' : activeStep > 2 ? 'done' : ''}`, '2. Verification code');
    nav.append(step1, step2);
    if (show2fa || activeStep === 3) {
      const step3 = create('div', `telegram-auth-step ${activeStep === 3 ? 'active' : activeStep > 3 ? 'done' : ''}`, '3. 2FA Password');
      nav.appendChild(step3);
    }
    return nav;
  }

  function renderWizardPhoneStep(body) {
    const wrap = create('div', 'telegram-auth-view');
    wrap.appendChild(createStepIndicator(1, false));

    const note = create('p', 'detail-note', 'Enter your Telegram phone number with international country code (e.g. +48123456789). A verification code will be sent to your Telegram app or SMS.');
    const form = create('form', 'telegram-auth-form');
    form.id = 'tgPhoneForm';

    const label = create('label');
    label.setAttribute('for', 'tgPhoneInput');
    label.textContent = 'Phone number';
    const input = create('input');
    input.id = 'tgPhoneInput';
    input.type = 'tel';
    input.placeholder = '+48123456789';
    input.required = true;
    input.autocomplete = 'tel';
    label.appendChild(input);

    const actions = create('div', 'operation-dialog-actions');
    const cancelBtn = create('button', 'button button-quiet', 'Cancel');
    cancelBtn.type = 'button';
    cancelBtn.addEventListener('click', closeTelegramAuthWizard);

    const submitBtn = create('button', 'button button-primary', 'Send verification code');
    submitBtn.type = 'submit';

    actions.append(cancelBtn, submitBtn);
    form.append(label, actions);
    wrap.append(note, form);
    body.replaceChildren(wrap);

    input.focus({ preventScroll: true });

    form.addEventListener('submit', async event => {
      event.preventDefault();
      const phoneVal = input.value.trim();
      if (!phoneVal.startsWith('+') || phoneVal.length < 8) {
        setWizardBanner('error', 'Phone number must be in international format (e.g. +48123456789).');
        return;
      }
      setWizardBanner('', '');
      submitBtn.disabled = true;
      submitBtn.textContent = 'Sending code…';
      try {
        const result = await postTelegramAuth(TELEGRAM_AUTH_START_URL, { phone: phoneVal });
        if (result.ok) {
          if (result.state === 'authorized') {
            renderWizardSuccess(body);
          } else {
            renderWizardCodeStep(body, result.phone_mask || phoneVal, result.expires_in_seconds || 600);
          }
        } else {
          setWizardBanner('error', extractErrorMessage(result, 'Failed to send verification code.'));
          submitBtn.disabled = false;
          submitBtn.textContent = 'Send verification code';
        }
      } catch (err) {
        if (err.flood_wait_seconds) {
          setWizardBanner('warning', extractErrorMessage(err, `Please wait ${err.flood_wait_seconds}s before requesting a code.`));
        } else {
          setWizardBanner('error', extractErrorMessage(err, 'Could not send verification code.'));
        }
        submitBtn.disabled = false;
        submitBtn.textContent = 'Send verification code';
      }
    });
  }

  function renderWizardCodeStep(body, phoneMask, expiresIn) {
    const wrap = create('div', 'telegram-auth-view');
    wrap.appendChild(createStepIndicator(2, false));

    const note = create('p', 'detail-note', `A verification code has been sent to ${phoneMask || 'your phone'}. Please enter the code below.`);
    const form = create('form', 'telegram-auth-form');
    form.id = 'tgCodeForm';

    const label = create('label');
    label.setAttribute('for', 'tgCodeInput');
    label.textContent = 'Verification code';
    const input = create('input');
    input.id = 'tgCodeInput';
    input.type = 'text';
    input.placeholder = '12345';
    input.required = true;
    input.autocomplete = 'one-time-code';
    input.inputMode = 'numeric';
    label.appendChild(input);

    const actions = create('div', 'operation-dialog-actions');
    const cancelBtn = create('button', 'button button-quiet', 'Cancel');
    cancelBtn.type = 'button';
    cancelBtn.addEventListener('click', async () => {
      try {
        await postTelegramAuth(TELEGRAM_AUTH_CANCEL_URL, {});
      } catch (e) {}
      renderWizardPhoneStep(body);
    });

    const resendBtn = create('button', 'button button-quiet', 'Resend code');
    resendBtn.type = 'button';
    resendBtn.addEventListener('click', async () => {
      resendBtn.disabled = true;
      resendBtn.textContent = 'Resending…';
      try {
        const result = await postTelegramAuth(TELEGRAM_AUTH_RESEND_URL, {});
        const floodWait = result.flood_wait_seconds || (result.error && typeof result.error === 'object' ? result.error.flood_wait_seconds : undefined);
        if (result.ok) {
          setWizardBanner('info', result.message || 'Verification code resent.');
          resendBtn.textContent = 'Code resent';
          setTimeout(() => { resendBtn.disabled = false; resendBtn.textContent = 'Resend code'; }, 10000);
        } else if (floodWait) {
          setWizardBanner('warning', extractErrorMessage(result, `Please wait ${floodWait}s before requesting a code.`));
          startResendCountdown(resendBtn, floodWait);
        } else {
          setWizardBanner('error', extractErrorMessage(result, 'Could not resend code.'));
          resendBtn.disabled = false;
          resendBtn.textContent = 'Resend code';
        }
      } catch (err) {
        if (err.flood_wait_seconds) {
          setWizardBanner('warning', extractErrorMessage(err, `Please wait ${err.flood_wait_seconds}s before requesting a code.`));
          startResendCountdown(resendBtn, err.flood_wait_seconds);
        } else {
          setWizardBanner('error', extractErrorMessage(err, 'Could not resend code.'));
          resendBtn.disabled = false;
          resendBtn.textContent = 'Resend code';
        }
      }
    });

    const submitBtn = create('button', 'button button-primary', 'Verify code');
    submitBtn.type = 'submit';

    actions.append(cancelBtn, resendBtn, submitBtn);
    form.append(label, actions);
    wrap.append(note, form);
    body.replaceChildren(wrap);

    input.focus({ preventScroll: true });

    form.addEventListener('submit', async event => {
      event.preventDefault();
      const codeVal = input.value.trim();
      if (!codeVal) return;
      setWizardBanner('', '');
      submitBtn.disabled = true;
      submitBtn.textContent = 'Verifying…';
      try {
        const result = await postTelegramAuth(TELEGRAM_AUTH_CODE_URL, { code: codeVal });
        if (result.ok) {
          if (result.state === 'password_needed') {
            renderWizardPasswordStep(body);
          } else if (result.state === 'authorized') {
            renderWizardSuccess(body, result.sync_notified);
          } else {
            setWizardBanner('error', extractErrorMessage(result, 'Verification code failed.'));
            submitBtn.disabled = false;
            submitBtn.textContent = 'Verify code';
          }
        } else {
          setWizardBanner('error', extractErrorMessage(result, 'Verification code failed.'));
          submitBtn.disabled = false;
          submitBtn.textContent = 'Verify code';
        }
      } catch (err) {
        setWizardBanner('error', extractErrorMessage(err, 'Verification failed.'));
        submitBtn.disabled = false;
        submitBtn.textContent = 'Verify code';
      }
    });
  }

  function startResendCountdown(button, seconds) {
    if (authCountdownTimer) clearInterval(authCountdownTimer);
    let remaining = Number(seconds) || 0;
    button.disabled = true;
    button.textContent = `Wait ${remaining}s`;
    authCountdownTimer = setInterval(() => {
      remaining -= 1;
      if (remaining <= 0) {
        clearInterval(authCountdownTimer);
        authCountdownTimer = null;
        button.disabled = false;
        button.textContent = 'Resend code';
      } else {
        button.disabled = true;
        button.textContent = `Wait ${remaining}s`;
      }
    }, 1000);
  }

  function renderWizardPasswordStep(body) {
    const wrap = create('div', 'telegram-auth-view');
    wrap.appendChild(createStepIndicator(3, true));

    const note = create('p', 'detail-note', 'Two-Factor Authentication (2FA) is enabled on this account. Please enter your 2FA password to complete login.');
    const form = create('form', 'telegram-auth-form');
    form.id = 'tgPasswordForm';

    const label = create('label');
    label.setAttribute('for', 'tgPasswordInput');
    label.textContent = '2FA Password';
    const input = create('input');
    input.id = 'tgPasswordInput';
    input.type = 'password';
    input.required = true;
    input.autocomplete = 'current-password';
    label.appendChild(input);

    const actions = create('div', 'operation-dialog-actions');
    const cancelBtn = create('button', 'button button-quiet', 'Cancel');
    cancelBtn.type = 'button';
    cancelBtn.addEventListener('click', async () => {
      try {
        await postTelegramAuth(TELEGRAM_AUTH_CANCEL_URL, {});
      } catch (e) {}
      renderWizardPhoneStep(body);
    });

    const submitBtn = create('button', 'button button-primary', 'Sign in');
    submitBtn.type = 'submit';

    actions.append(cancelBtn, submitBtn);
    form.append(label, actions);
    wrap.append(note, form);
    body.replaceChildren(wrap);

    input.focus({ preventScroll: true });

    form.addEventListener('submit', async event => {
      event.preventDefault();
      const pwdVal = input.value;
      if (!pwdVal) return;
      setWizardBanner('', '');
      submitBtn.disabled = true;
      submitBtn.textContent = 'Signing in…';
      try {
        const result = await postTelegramAuth(TELEGRAM_AUTH_PASSWORD_URL, { password: pwdVal });
        if (result.ok && result.state === 'authorized') {
          renderWizardSuccess(body, result.sync_notified);
        } else {
          setWizardBanner('error', extractErrorMessage(result, '2FA authentication failed.'));
          submitBtn.disabled = false;
          submitBtn.textContent = 'Sign in';
        }
      } catch (err) {
        setWizardBanner('error', extractErrorMessage(err, '2FA authentication failed.'));
        submitBtn.disabled = false;
        submitBtn.textContent = 'Sign in';
      }
    });
  }

  function renderWizardSuccess(body, syncNotified) {
    if (authCountdownTimer) {
      clearInterval(authCountdownTimer);
      authCountdownTimer = null;
    }
    const wrap = create('div', 'telegram-auth-view');
    const msg = syncNotified
      ? '✓ Telegram authentication successful! Your session has been promoted and background sync has been notified.'
      : '✓ Telegram authentication successful! Your session has been promoted and is ready for channel synchronization.';
    const banner = create('div', 'state-banner state-success', msg);
    const actions = create('div', 'operation-dialog-actions');
    const doneBtn = create('button', 'button button-primary', 'Done');
    doneBtn.type = 'button';
    doneBtn.addEventListener('click', () => {
      closeTelegramAuthWizard();
      loadSources();
    });
    actions.appendChild(doneBtn);
    wrap.append(banner, actions);
    body.replaceChildren(wrap);
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
    const telegramLoginBtn = $('#telegramLoginButton');
    if (telegramLoginBtn) {
      telegramLoginBtn.addEventListener('click', event => {
        return openTelegramAddAccountWizard(event.currentTarget);
      });
    }
    const accountsRefreshBtn = $('#telegramAccountsRefreshBtn');
    if (accountsRefreshBtn) {
      accountsRefreshBtn.addEventListener('click', () => renderTelegramAccountCard());
    }
    const telegramAuthClose = $('#telegramAuthClose');
    if (telegramAuthClose) {
      telegramAuthClose.addEventListener('click', () => closeTelegramAuthWizard());
    }
    const telegramAuthDialog = $('#telegramAuthDialog');
    if (telegramAuthDialog) {
      telegramAuthDialog.addEventListener('cancel', event => {
        event.preventDefault();
        closeTelegramAuthWizard();
      });
      telegramAuthDialog.addEventListener('click', event => {
        if (event.target === telegramAuthDialog) {
          closeTelegramAuthWizard();
        }
      });
    }
    const telegramAccountActionClose = $('#telegramAccountActionClose');
    if (telegramAccountActionClose) {
      telegramAccountActionClose.addEventListener('click', () => closeTelegramAccountActionDialog());
    }
    const telegramAccountActionDialog = $('#telegramAccountActionDialog');
    if (telegramAccountActionDialog) {
      telegramAccountActionDialog.addEventListener('cancel', event => {
        event.preventDefault();
        closeTelegramAccountActionDialog();
      });
      telegramAccountActionDialog.addEventListener('click', event => {
        if (event.target === telegramAccountActionDialog) {
          closeTelegramAccountActionDialog();
        }
      });
    }
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
        if (event.target === dialog) { closeSourceDetail(); return; }
        const sync = event.target.closest('[data-sync-source]');
        if (sync) { syncNow(sync.dataset.syncSource, sync.dataset.syncMode || 'scheduled', sync); return; }
        const check = event.target.closest('[data-check-source]');
        if (check) { checkSourceAccess(check.dataset.checkSource, check); return; }
        if (event.target.closest('[data-source-create-cancel]')) { state.editorCandidate = null; closeSourceDetail(); return; }
        const createPreview = event.target.closest('[data-source-create-preview]'); if (createPreview) { previewNewChannel(createPreview); return; }
        const createSave = event.target.closest('#sourceCreateSave'); if (createSave) { saveNewChannel(createSave); return; }
        const remove = event.target.closest('[data-source-remove]'); if (remove) { removeSourceChannel(remove); return; }
        if (event.target.closest('[data-source-edit-name]') && state.detailItem) { renderChannelNameEditor(state.detailItem); return; }
        if (event.target.closest('[data-source-edit-name-cancel]')) {
          if (state.detailItem) renderSourceDetail(state.detailItem);
          else closeSourceDetail();
          return;
        }
        const editNameSave = event.target.closest('#sourceEditNameSave');
        if (editNameSave) { saveChannelName(editNameSave); return; }
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
    }
    document.addEventListener('adminv2:navigate', sync);
  }

  setupEvents();
  sync();
})();
