const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

let activeElement = null;
class Element {
  constructor(tag = 'div') {
    this.tagName = tag; this.children = []; this.listeners = {}; this.dataset = {};
    this.hidden = false; this.disabled = false; this.value = ''; this.textContent = '';
    this.className = ''; this.isConnected = true; this.attributes = {};
  }
  addEventListener(type, callback) { this.listeners[type] = callback; }
  append(...nodes) { nodes.forEach(node => { node.parentElement = this; }); this.children.push(...nodes); }
  replaceChildren(...nodes) { nodes.forEach(node => { node.parentElement = this; }); this.children = nodes; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  removeAttribute() {}
  querySelector(selector) {
    if (selector.startsWith('.')) return this.children.find(node => node.className.split(/\s+/).includes(selector.slice(1))) || null;
    return null;
  }
  focus() { if (!this.disabled) activeElement = this; }
  querySelectorAll() { return []; }
}

function descendants(root, predicate) {
  return [
    ...(predicate(root) ? [root] : []),
    ...(root.children || []).flatMap(child => descendants(child, predicate)),
  ];
}

(async () => {
  const ids = [
    'sourcesView', 'sourcesFilters', 'sourcesClearFilters', 'sourcesRefresh', 'sourcesAddChannel',
    'sourcesStatusFilter',
    'sourcesRows', 'sourcesPagination', 'sourceDetailDialog', 'sourcesNotice', 'sourcesFeedback',
    'sourceDetailContent',
    'sourcesHistoryFeedback', 'sourcesHistoryRows',
    'sourcesRevision', 'sourcesState', 'sourcesTableWrap', 'sourcesQuery', 'liveMessage', 'crumbCurrent',
    'operationalEyebrow', 'operationalTitle', 'operationalIntro',
  ];
  const elements = Object.fromEntries(ids.map(id => [id, new Element(id === 'sourcesFilters' ? 'form' : 'div')]));
  const document = {
    get activeElement() { return activeElement; },
    querySelector(selector) {
      if (selector.startsWith('#')) return elements[selector.slice(1)] || null;
      const pageMatch = selector.match(/^\[data-source-page="(previous|next)"\]$/);
      if (pageMatch) return descendants(elements.sourcesPagination, node => node.dataset.sourcePage === pageMatch[1])[0] || null;
      return null;
    },
    querySelectorAll: () => [],
    createElement: tag => new Element(tag),
    addEventListener() {},
  };
  const channels = Array.from({ length: 1201 }, (_, index) => ({
    id: String(index + 1), studio: `Channel ${String(index + 1).padStart(4, '0')}`, enabled: true,
    stream_mode: 'proxy', max_pages: 10, min_duration_seconds: 60, history_batch_size: 20,
    tags_whitelist: [], tags_blacklist: [],
  }));
  channels[0].enabled = false;
  let historyPayload = { runs: [] };
  let historyFails = false;
  let sourcesFail = false;
  const postCalls = [];
  let checkResult = {
    ok: true, status: 'accessible', message: 'Telegram read access confirmed.',
    checked_at: '2026-01-01T00:00:00Z',
  };
  const windowListeners = {};
  const window = { addEventListener(type, callback) { windowListeners[type] = callback; }, AdminV2: {
    async getJson(path) {
      if (path.endsWith('/sources/list')) {
        if (sourcesFail) throw new Error('source configuration unavailable');
        return {
        available: true, managed_revision: 1, applied_revision: 1, applied_revision_available: true,
        sources: [
          { provider: 'telegram', enabled: true, defaults: {}, channels: channels.slice(0, 1200) },
          { provider: 'telegram', enabled: false, defaults: {}, channels: [channels[1200]] },
          { provider: 'secondweb', enabled: true, channels: [], configuration: {
            address_configured: true, stream_mode: 'redirect', max_pages: 7, sections: ['Scene', 'Featured'],
          } },
        ],
        };
      }
      if (path.includes('/sync-history')) {
        if (historyFails) throw new Error('history unavailable');
        return historyPayload;
      }
      if (path.endsWith('/sources/settings')) return { revision: 1, overrides: {} };
      throw new Error(`Unexpected URL ${path}`);
    },
    async postJson(path, payload) {
      postCalls.push({ path, payload });
      if (path === '/admin/api/v2/sources/check') return checkResult;
      return { ok: true, job_id: `job-${payload.sync_mode}`, message: 'Sync was requested.' };
    },
    openDialog(dialog) { if (dialog) dialog.open = true; },
    closeDialog(dialog) { if (dialog) dialog.open = false; },
  } };
  const location = { hash: '#sources', search: '', href: 'http://localhost/admin/v2/#sources' };
  const history = {
    replaceState(_state, _title, value) {
      const url = new URL(String(value), location.href);
      location.href = url.href; location.search = url.search; location.hash = url.hash;
    },
    pushState(_state, _title, value) {
      const url = new URL(String(value), location.href);
      location.href = url.href; location.search = url.search; location.hash = url.hash;
    },
  };
  const context = {
    window, document, location, history, URL, URLSearchParams, AbortController,
    setTimeout, clearTimeout, Intl, Date, Map, Set, Array, Object, Number, String, Boolean, Math,
  };
  vm.runInNewContext(fs.readFileSync('resolver/static/admin-v2/sources.js', 'utf8'), context);
  await new Promise(resolve => setTimeout(resolve, 0));

  assert.equal(elements.sourcesRows.children.length, 50);
  assert.equal(elements.sourcesRows.children[0].children[2].children[0].textContent, 'Disabled');
  assert.equal(elements.sourcesRows.children[0].children[4].children[0].children[0].textContent, 'Never synced');
  const pageInfo = () => elements.sourcesPagination.children.find(child => child.tagName === 'span');
  assert.equal(pageInfo().textContent, 'Page 1 of 25');
  assert.match(elements.sourcesFeedback.textContent, /Showing 1–50 of 1202 matching channels/);

  const next = elements.sourcesPagination.children.find(button => button.dataset.sourcePage === 'next');
  elements.sourcesPagination.listeners.click({ target: { closest: () => next } });
  assert.equal(elements.sourcesRows.children.length, 50);
  assert.equal(pageInfo().textContent, 'Page 2 of 25');
  assert.equal(new URLSearchParams(location.search).get('source_page'), '1', 'the current page is linkable');
  assert.equal(document.activeElement.dataset.sourcePage, 'next');
  assert.match(elements.sourcesRows.children[0].children[0].children[0].textContent, /Channel 0051/);
  const sourceActionGroup = elements.sourcesRows.children[0].children[6].children[0];
  const syncButtons = sourceActionGroup.children.filter(button => button.dataset.syncSource);
  assert.deepEqual(syncButtons.map(button => button.dataset.syncMode), ['scheduled', 'new', 'backfill']);
  assert.deepEqual(syncButtons.map(button => button.getAttribute('aria-label')),
    ['Run sync for Channel 0051', 'Sync new for Channel 0051', 'Backfill Channel 0051'],
    'each repeated sync action exposes the source name in its accessible name');
  assert.equal(sourceActionGroup.children.find(button => button.dataset.sourceDetail).getAttribute('aria-label'),
    'Details for Channel 0051');
  assert.equal(sourceActionGroup.children.find(button => button.dataset.checkSource).getAttribute('aria-label'),
    'Check access for Channel 0051');
  for (const button of syncButtons) {
    elements.sourcesRows.listeners.click({ target: { closest: selector =>
      selector === '[data-sync-source]' ? button : null } });
    await new Promise(resolve => setTimeout(resolve, 0));
  }
  assert.equal(JSON.stringify(postCalls), JSON.stringify(['scheduled', 'new', 'backfill'].map(sync_mode => ({
    path: '/admin/api/v2/maintenance/sync', payload: { source: '51', sync_mode },
  }))));
  const checkButton = sourceActionGroup.children.find(button => button.dataset.checkSource === '51');
  elements.sourcesRows.listeners.click({ target: { closest: selector =>
    selector === '[data-check-source]' ? checkButton : null } });
  await new Promise(resolve => setTimeout(resolve, 0));
  const accessOutcome = sourceActionGroup.children.find(node => node.dataset.accessOutcome === '51');
  assert.equal(checkButton.textContent, 'Check access', 'the action remains available after a successful check');
  assert.equal(accessOutcome.dataset.accessStatus, 'accessible');
  assert.match(accessOutcome.textContent, /^Accessible · Telegram read access confirmed\. · Checked /);
  for (let page = 2; page < 25; page++) {
    const currentNext = elements.sourcesPagination.children.find(button => button.dataset.sourcePage === 'next');
    elements.sourcesPagination.listeners.click({ target: { closest: () => currentNext } });
  }
  assert.equal(elements.sourcesRows.children.length, 2);
  assert.equal(pageInfo().textContent, 'Page 25 of 25');
  assert.match(elements.sourcesRows.children[0].children[0].children[0].textContent, /Channel 1201/);
  assert.equal(elements.sourcesRows.children[0].children[2].children[0].textContent, 'Source disabled');
  assert.match(elements.sourcesRows.children[1].children[0].children[0].textContent, /Secondweb configuration/);
  assert.equal(elements.sourcesRows.children[1].children[6].children[0].children.length, 1);
  assert.equal(document.activeElement.dataset.sourcePage, 'previous');

  for (let page = 25; page > 2; page--) {
    const currentPrevious = elements.sourcesPagination.children.find(button => button.dataset.sourcePage === 'previous');
    elements.sourcesPagination.listeners.click({ target: { closest: () => currentPrevious } });
  }
  assert.equal(pageInfo().textContent, 'Page 2 of 25');
  assert.equal(document.activeElement.dataset.sourcePage, 'previous');
  const returnedCheckButton = elements.sourcesRows.children[0].children[6].children[0].children.find(
    button => button.dataset.checkSource === '51');
  const returnedOutcome = elements.sourcesRows.children[0].children[6].children[0].children.find(
    node => node.dataset.accessOutcome === '51');
  assert.equal(returnedCheckButton.textContent, 'Check access');
  assert.equal(returnedOutcome.dataset.accessStatus, 'accessible', 'the last result survives paging');
  checkResult = {
    ok: true, status: 'unknown', message: 'Cannot confirm: https://private.invalid/channel?token=secret password=hunter2',
    checked_at: '2026-01-01T00:05:00Z',
  };
  elements.sourcesRows.listeners.click({ target: { closest: selector =>
    selector === '[data-check-source]' ? returnedCheckButton : null } });
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(returnedCheckButton.textContent, 'Check access');
  assert.equal(returnedOutcome.dataset.accessStatus, 'unknown');
  assert.match(returnedOutcome.textContent, /^Unknown · Cannot confirm: \[link hidden\] password: \[hidden\] · Checked /);
  assert.doesNotMatch(returnedOutcome.textContent, /private\.invalid|secret|hunter2/,
    'upstream detail is redacted in the visible result');
  location.href = 'http://localhost/admin/v2/?source_status=disabled#sources';
  location.search = '?source_status=disabled';
  windowListeners.popstate();
  assert.equal(elements.sourcesStatusFilter.value, 'disabled');
  assert.equal(elements.sourcesRows.children.length, 2, 'browser history restores the configured status filter');
  location.href = 'http://localhost/admin/v2/?source_page=1#sources';
  location.search = '?source_page=1';
  windowListeners.popstate();
  assert.equal(pageInfo().textContent, 'Page 2 of 25', 'browser history restores pagination');
  const pageTwoPrevious = elements.sourcesPagination.children.find(button => button.dataset.sourcePage === 'previous');
  elements.sourcesPagination.listeners.click({ target: { closest: () => pageTwoPrevious } });
  assert.equal(pageInfo().textContent, 'Page 1 of 25');
  assert.equal(document.activeElement.dataset.sourcePage, 'next');
  const sortButton = { dataset: { sourceSort: 'videos' }, focus() {} };
  elements.sourcesTableWrap.listeners.click({ target: { closest: selector =>
    selector === '[data-source-sort]' ? sortButton : null } });
  assert.equal(new URLSearchParams(location.search).get('source_sort'), 'videos', 'sort order is linkable');
  elements.sourcesTableWrap.listeners.click({ target: { closest: selector =>
    selector === '[data-source-sort]' ? sortButton : null } });
  assert.equal(new URLSearchParams(location.search).get('source_dir'), 'desc', 'sort direction is linkable');
  const channelSortButton = { dataset: { sourceSort: 'channel' }, focus() {} };
  elements.sourcesTableWrap.listeners.click({ target: { closest: selector =>
    selector === '[data-source-sort]' ? channelSortButton : null } });
  assert.equal(new URLSearchParams(location.search).get('source_sort'), null, 'default sort is omitted from the URL');

  const query = { name: 'q', value: 'Channel 0051' };
  elements.sourcesFilters.listeners.input({ target: query });
  assert.equal(elements.sourcesRows.children.length, 1);
  assert.equal(elements.sourcesPagination.hidden, true);
  assert.match(elements.sourcesFeedback.textContent, /Showing 1–1 of 1 matching channels/);
  assert.equal(new URLSearchParams(location.search).get('source_q'), 'Channel 0051', 'reload context is persisted immediately');
  await new Promise(resolve => setTimeout(resolve, 350));
  assert.equal(new URLSearchParams(location.search).get('source_q'), 'Channel 0051', 'search is persisted to the URL');
  query.value = '';
  elements.sourcesFilters.listeners.input({ target: query });
  await new Promise(resolve => setTimeout(resolve, 350));
  assert.equal(new URLSearchParams(location.search).get('source_q'), null);
  historyPayload = { available: true, total: 1, truncated: false, runs: [{
    started_at: 1791367200, ended_at: 1791367210, duration_seconds: 10, success: false,
    sync_mode: 'scheduled', managed_config_revision: 4,
    sources: [{ source: 'telegram:2', label: 'Channel 02', success: false, videos: 8, added: 2, removed: 1,
      duration_seconds: 4.25, error: 'Sync failed (TimeoutError).',
      detail: 'history write incomplete; [link hidden] [path hidden]' }],
    stages: { telegram_history: { calls: 2, total_seconds: 4.25 } },
    http: { failed_attempts: 1, retries: 1, rate_limited: 1 },
  }, {
    started_at: 1791280800, ended_at: 1791280810, success: true,
    sources: [{ source: 'telegram:2', success: true, videos: 6, added: 1, removed: 0 }],
  }] };
  await elements.sourcesRefresh.listeners.click();
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(elements.sourcesRows.children[1].children[4].children[0].children[0].textContent, 'Failed');
  const textOf = node => [node.textContent, ...node.children.map(textOf)].join(' ');
  const runText = textOf(elements.sourcesHistoryRows.children[0]);
  assert.match(runText, /Partial \/ failed/);
  assert.match(runText, /Failed sources: Channel 02 · Sync failed \(TimeoutError\)\./);
  assert.match(runText, /telegram history 4\.3 s \(2 calls\)/);
  assert.match(runText, /HTTP: 1 failed attempts · 1 retries · 1 rate limits/);
  assert.match(elements.sourcesHistoryFeedback.textContent, /Showing 2 of 2 recent sync runs/);
  const channelDetailButton = descendants(elements.sourcesRows,
    node => node.dataset.sourceDetail === 'telegram::2')[0];
  elements.sourcesRows.listeners.click({ target: { closest: selector =>
    selector === '[data-source-detail]' ? channelDetailButton : null } });
  const detailText = () => textOf(elements.sourceDetailContent);
  assert.match(detailText(), /Last successful sync/);
  assert.match(detailText(), /Failure: Sync failed \(TimeoutError\)\. — history write incomplete/,
    'per-channel history includes actionable failure context');
  assert.doesNotMatch(detailText(), /private\.invalid|\/srv\/media/,
    'per-channel failure context never exposes URLs or filesystem paths');
  assert.match(detailText(), /Oct 6, 2026/,
    'the channel detail retains its last successful run even when the latest run failed');
  historyPayload = { available: true, total: 60, truncated: true, runs: [{
    ended_at: 1791367210, success: false,
    sources: [{ source: 'telegram:2', success: false, videos: 8 }],
  }] };
  await elements.sourcesRefresh.listeners.click();
  await new Promise(resolve => setTimeout(resolve, 0));
  const updatedDetailButton = descendants(elements.sourcesRows,
    node => node.dataset.sourceDetail === 'telegram::2')[0];
  elements.sourcesRows.listeners.click({ target: { closest: selector =>
    selector === '[data-source-detail]' ? updatedDetailButton : null } });
  assert.match(detailText(), /Not found in recent history/,
    'a truncated history window must not imply the channel never succeeded');
  historyPayload = { available: true, total: 1, truncated: false, runs: [{
    ended_at: 1791367210, success: false,
    sources: [{ source: 'telegram:2', success: false, videos: 8 }],
  }] };
  await elements.sourcesRefresh.listeners.click();
  await new Promise(resolve => setTimeout(resolve, 0));
  const completeHistoryButton = descendants(elements.sourcesRows,
    node => node.dataset.sourceDetail === 'telegram::2')[0];
  elements.sourcesRows.listeners.click({ target: { closest: selector =>
    selector === '[data-source-detail]' ? completeHistoryButton : null } });
  assert.match(detailText(), /No successful sync recorded/,
    'a complete history with no successful run can report that none is recorded');
  historyFails = true;
  await elements.sourcesRefresh.listeners.click();
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(elements.sourcesRows.children[1].children[4].children[0].children[0].textContent, 'History unavailable');
  assert.match(elements.sourcesNotice.children.map(child => child.textContent).join(' '), /Sync history is unavailable/);
  assert.match(elements.sourcesHistoryFeedback.textContent, /Sync history is unavailable/);
  assert.equal(elements.sourcesHistoryRows.children.length, 0);

  historyFails = false;
  sourcesFail = true;
  historyPayload = { runs: [] };
  await elements.sourcesRefresh.listeners.click();
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.match(elements.sourcesFeedback.textContent, /Source list unavailable/);
  assert.match(elements.sourcesHistoryFeedback.textContent, /No sync runs have been recorded yet/,
    'sync-history state resolves even when configured-source loading fails');

  sourcesFail = false;
  location.hash = '#sources';
  location.search = '?source_id=telegram%3A2';
  location.href = 'http://localhost/admin/v2/?source_id=telegram%3A2#sources';
  vm.runInNewContext(fs.readFileSync('resolver/static/admin-v2/sources.js', 'utf8'), context);
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(elements.sourcesQuery.value, '2');
  assert.equal(elements.sourcesRows.children.length, 1);
  assert.match(elements.sourcesRows.children[0].children[0].children[0].textContent, /Channel 0002/);
  const deepLinkParams = new URLSearchParams(location.search);
  assert.equal(deepLinkParams.get('source_id'), null, 'the one-shot source deep link is normalized');
  assert.equal(deepLinkParams.get('source_key'), 'telegram::2');
  assert.equal(deepLinkParams.get('source_q'), '2');
  elements.sourcesFilters.listeners.input({ target: { name: 'q', value: 'Channel 0051' } });
  location.href = `http://localhost/admin/v2/?${deepLinkParams.toString()}#sources`;
  location.search = `?${deepLinkParams.toString()}`;
  windowListeners.popstate();
  assert.equal(elements.sourcesQuery.value, '2', 'history navigation cancels pending search URL commits');
  await new Promise(resolve => setTimeout(resolve, 350));
  assert.equal(new URLSearchParams(location.search).get('source_q'), '2');
  elements.sourcesAddChannel.listeners.click({ currentTarget: elements.sourcesAddChannel });
  const newChannelFields = descendants(elements.sourceDetailContent, node => node.tagName === 'input');
  assert.equal(newChannelFields.length, 2, 'the add-channel form renders channel ID and studio inputs');
  assert.deepEqual(newChannelFields.map(input => {
    const label = descendants(elements.sourceDetailContent,
      node => node.tagName === 'label' && node.children.includes(input))[0];
    return label?.children[0]?.textContent;
  }), ['Telegram channel ID', 'Studio label'], 'each dynamically rendered channel input has a visible wrapping label');
  process.stdout.write('PASS: Sources URL state, redacted sync history, and persistent access-check outcomes\n');
})().catch(error => { console.error(error); process.exitCode = 1; });
