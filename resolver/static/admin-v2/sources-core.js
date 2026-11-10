(() => {
  'use strict';
  // Sources shared core: constants, state, item model, filters, and feedback helpers.
  const SRC = window.AdminV2Sources = {};
  const applyAccessButtonOutcome = (...args) => SRC.applyAccessButtonOutcome(...args);
  const readEffectiveSettings = (...args) => SRC.readEffectiveSettings(...args);
  const readProviderDefaults = (...args) => SRC.readProviderDefaults(...args);
  const renderPagination = (...args) => SRC.renderPagination(...args);
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

  // Shared helpers live in admin-v2-core.js and are published through window.AdminV2.
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

  // Publish core bindings for the other Admin V2 scripts.
  Object.assign(SRC, { $, HISTORY_URL, SOURCES_URL, SOURCE_CHECK_URL, SOURCE_CREATE_URL, SOURCE_PAGE_SIZE,
    SOURCE_PREVIEW_URL, SOURCE_REMOVE_URL, SOURCE_SAVE_URL, SOURCE_SETTINGS_URL, TELEGRAM_ACCOUNTS_URL,
    TELEGRAM_AUTH_CANCEL_URL, TELEGRAM_AUTH_CODE_URL, TELEGRAM_AUTH_PASSWORD_URL, TELEGRAM_AUTH_RESEND_URL,
    TELEGRAM_AUTH_START_URL, TELEGRAM_AUTH_STATUS_URL, TRIGGER_URL, V2_API, announce, appendRow,
    buildChannelHistory, buildHistoryIndex, buildItems, compareSourceItems, create, displayTime, helpers,
    historyFor, isSourcesRoute, isValidTelegramAccountId, itemKey, lastSuccessfulSync, matchesFilter,
    safeId, scrub, setFeedback, setNotice, setSourcesState, setText, sourcesTab, state, statusFor,
    updateSortHeaders });
})();
