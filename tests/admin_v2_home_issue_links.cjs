const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

let activeDocument = null;
class Element {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.listeners = {};
    this.dataset = {};
    this.attributes = {};
    this.style = {};
    this.hidden = false;
    this.disabled = false;
    this.value = '';
    this.textContent = '';
    this.className = '';
    this.href = '';
    this.isConnected = true;
    this.open = false;
    this.queryResults = new Map();
    this.classList = {
      add: (...names) => { this.className = [...new Set(`${this.className} ${names.join(' ')}`.trim().split(/\s+/))].join(' '); },
      remove: (...names) => { this.className = this.className.split(/\s+/).filter(name => name && !names.includes(name)).join(' '); },
      contains: name => this.className.split(/\s+/).includes(name),
      toggle: (name, force) => {
        const shouldAdd = force === undefined ? !this.classList.contains(name) : force;
        this.classList[shouldAdd ? 'add' : 'remove'](name);
        return shouldAdd;
      },
    };
  }
  addEventListener(type, callback) { this.listeners[type] = callback; }
  add(node) { this.children.push(node); }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  removeAttribute(name) { delete this.attributes[name]; }
  querySelectorAll(selector) { return this.queryResults.get(selector) || []; }
  querySelector(selector) { return this.queryResults.get(selector) || null; }
  contains(node) { return this.children.some(child => child === node || child.contains(node)); }
  getClientRects() { return this.hidden ? [] : [{}]; }
  get offsetParent() { return this.hidden ? null : {}; }
  focus() { if (activeDocument) activeDocument.activeElement = this; }
}

const html = fs.readFileSync('resolver/templates/admin_v2.html', 'utf8');
assert.match(html, /class="view"[^>]*data-view="operational"/,
  'Sources, Tasks, and System must use the named shared operational view');
assert.doesNotMatch(html, /roadmapCard|Coming soon|legacyRoute/,
  'The implemented v2 views must not retain the obsolete coming-soon fallback');
assert.match(html, /id="issueCount"[^>]*role="status"[^>]*aria-atomic="true"/,
  'Home announces issue result counts accessibly');
const elements = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(([, id]) => [id, new Element('div')]));
const connectionState = elements.get('connectionState');
const connectionLabel = new Element('span');
let connectionLabelText = 'Connecting…';
let connectionLabelWrites = 0;
Object.defineProperty(connectionLabel, 'textContent', {
  get: () => connectionLabelText,
  set: value => { connectionLabelWrites += 1; connectionLabelText = String(value); },
});
connectionState.queryResults.set('span:last-child', connectionLabel);
const views = [...html.matchAll(/<section\b[^>]*class="[^"]*view[^\"]*"[^>]*data-view="([^"]+)"/g)]
  .map(([, route]) => { const element = new Element('section'); element.dataset.view = route; return element; });
const navLinks = [...html.matchAll(/<a\b[^>]*data-route="([^"]+)"/g)]
  .map(([, route]) => { const element = new Element('a'); element.dataset.route = route; return element; });
const sectionTabLinks = [...html.matchAll(/<nav\b[^>]*class="([^"]*)"[^>]*>([\s\S]*?)<\/nav>/g)]
  .flatMap(([, className, contents]) => [...contents.matchAll(/<a\b[^>]*data-section-tab="([^"]+)"/g)]
    .map(([, tab]) => { const element = new Element('a'); element.className = className; element.dataset.sectionTab = tab; return element; }));
const systemTabLinks = [...html.matchAll(/<a\b[^>]*data-system-tab="([^"]+)"/g)]
  .map(([, tab]) => { const element = new Element('a'); element.dataset.systemTab = tab; return element; });
const documentListeners = new Map();
let lastAdminNavigation = null;
const operationDialog = elements.get('operationDialog');
const metadataDialog = elements.get('metadataDialog');
const detailOverlay = elements.get('detailOverlay');
const openDialogs = [operationDialog, metadataDialog, detailOverlay];
let detailDrawer = null;
const expectedRoutes = ['start', 'library', 'quality', 'sources', 'organize', 'performers', 'jobs', 'system'];
const primaryRoutes = ['start', 'library', 'performers', 'quality', 'sources', 'jobs', 'system'];
assert.deepEqual(navLinks.map(link => link.dataset.route), primaryRoutes,
  'the primary navigation must expose exactly the seven v3.1 destinations in order');
const primaryLabels = [...html.matchAll(/<a\b[^>]*data-route="[^"]+"[^>]*>([\s\S]*?)<\/a>/g)]
  .map(([, contents]) => contents.replace(/<span\b[^>]*aria-hidden="true"[^>]*>[\s\S]*?<\/span>/g, '')
    .replace(/<[^>]*>/g, '').replace(/&amp;/g, '&').trim());
assert.deepEqual(primaryLabels, ['Dashboard', 'Library', 'Performers', 'Catalog Health', 'Sources', 'Jobs & Automation', 'System'],
  'the primary navigation uses the v3.1 section names');
const routerSource = fs.readFileSync('resolver/static/admin-v2/admin-v2.js', 'utf8');
const detailRenderer = routerSource.slice(routerSource.indexOf('async function loadAvailabilityHistory'), routerSource.indexOf('function duration(value)'));
for (const heading of ['Overview', 'Metadata', 'Files', 'Availability & History']) {
  assert.ok(detailRenderer.includes(`'${heading}'`), `the video detail must expose a ${heading} section`);
}
assert.match(detailRenderer, /Current metadata revision/,
  'the detail makes the revision checked by the override workflow visible');
assert.match(detailRenderer, /Loading availability history/,
  'availability history exposes a loading state');
assert.match(detailRenderer, /Availability history could not be loaded/,
  'availability history exposes a recoverable read error');
assert.match(detailRenderer, /create\('button', 'button button-quiet', 'Edit metadata'\)/,
  'the metadata action label describes all supported editor fields');
assert.match(detailRenderer, /Open file consistency audits[\s\S]*?go\('quality', \{ tab: 'issues' \}\)/,
  'file repair discovery navigates to the primary Issues & Audits tab');
assert.match(html, /id="auditTitle" tabindex="-1"/,
  'the file-audit destination has a programmatically focusable heading');
for (const route of expectedRoutes) {
  assert.match(routerSource, new RegExp(`\\b${route}:\\s*\\{\\s*title:`),
    `the Admin v2 router must define the ${route} destination`);
}
const document = {
  hidden: false,
  readyState: 'complete',
  activeElement: new Element('body'),
  documentElement: new Element('html'),
  querySelector(selector) {
    if (selector.startsWith('#')) {
      const direct = elements.get(selector.slice(1));
      if (direct) return direct;
      const pending = [...elements.values()];
      while (pending.length) {
        const item = pending.shift();
        if (item.id === selector.slice(1)) return item;
        pending.push(...(item.children || []));
      }
      return null;
    }
    if (selector === '.detail-drawer') return detailDrawer;
    return null;
  },
  querySelectorAll(selector, root) {
    if (root) return root.querySelectorAll(selector);
    if (selector === '.view[data-view]') return views;
    if (selector === '.primary-nav a[data-route]') return navLinks;
    if (selector === '.performer-section-tabs [data-section-tab]') return sectionTabLinks.filter(link => link.className.includes('performer-section-tabs'));
    if (selector === '.catalog-health-tabs [data-section-tab]') return sectionTabLinks.filter(link => link.className.includes('catalog-health-tabs'));
    if (selector === '#systemSectionTabs [data-system-tab]') return systemTabLinks;
    if (selector === 'dialog[open]') return openDialogs.filter(dialog => dialog.open);
    return [];
  },
  createElement(tag) { return new Element(tag); },
  addEventListener(type, callback) { documentListeners.set(type, callback); },
  dispatchEvent(event) { if (event.type === 'adminv2:navigate') lastAdminNavigation = event; },
};
activeDocument = document;
detailDrawer = new Element('aside');
detailDrawer.className = 'detail-drawer';
const libraryFields = new Map(['q', 'channel_id', 'studio', 'performer', 'tag', 'availability', 'missing', 'sort']
  .map(name => { const field = new Element(name === 'availability' || name === 'sort' ? 'select' : 'input'); field.name = name; field.value = ''; return [name, field]; }));
const libraryForm = elements.get('libraryFilters');
libraryForm.elements = { namedItem: name => libraryFields.get(name) || null };
libraryForm.reset = () => { for (const [name, field] of libraryFields) field.value = name === 'sort' ? 'first_seen' : ''; };
const issues = [
  { kind: 'source_failure', severity: 'high', title: 'Channel failed', action: { label: 'Open source', route: '/admin/v2/?source_id=telegram%3A1#sources' } },
  { kind: 'sync_never_run', severity: 'low', title: 'Channel never synced', action: { label: 'Open source', route: '/admin/v2/?source_id=telegram%3A2#sources' } },
  { kind: 'sync_backlog', severity: 'medium', title: 'Sync queue is old', action: { label: 'Review queue', route: '/admin/v2/?job_state=queued&job_action=sync#jobs' } },
  { kind: 'job_failure', severity: 'high', title: 'Task failed', action: { label: 'Open task', route: '/admin/v2/?job_id=task-9#jobs' } },
  { kind: 'job_stalled', severity: 'high', title: 'Task may be stalled', action: { label: 'Open task details', route: '/admin/v2/?job_id=task-10#jobs' } },
  { kind: 'unavailable_video', severity: 'medium', title: 'Unavailable video', action: { label: 'Review unavailable', route: '/admin/v2/#library?availability=unavailable' } },
  { kind: 'quality_finding', severity: 'medium', title: 'NFO metadata differs', count: 7,
    action: { label: 'Review findings', route: '/admin/v2/?quality_kind=nfo_metadata_mismatch#quality' } },
  { kind: 'backup', severity: 'high', title: 'Database backup is overdue', action: { label: 'Review backups', route: '/admin/v2/#system?tab=backups' } },
  { kind: 'system', severity: 'low', title: 'Unsafe external issue', action: { label: 'Open external site', route: 'https://example.invalid/' } },
];
const calls = [];
let failCapabilities = false;
let failStatus = false;
let failQualitySummary = false;
let failJellyfinStatus = false;
let failedTaskSummaryAvailable = true;
let failedTaskSummary = 2;
let lastSyncFailedSources = 3;
let lastSyncRateLimited = 2;
let issueFeedIncomplete = false;
let failIssueFeed = false;
let malformedIssueFeed = false;
async function fetch(url) {
  calls.push(String(url));
  if (failCapabilities && String(url).endsWith('/capabilities')) return { ok: false, status: 503, json: async () => ({ detail: 'unavailable' }) };
  if (failStatus && String(url).endsWith('/status')) return { ok: false, status: 503, json: async () => ({ detail: 'unavailable' }) };
  let payload;
  const parsedUrl = new URL(String(url), 'http://localhost');
  if (parsedUrl.pathname === '/admin/api/v2/issues') {
    if (failIssueFeed) return { ok: false, status: 503, json: async () => ({ detail: 'unavailable' }) };
    const filtered = issues.filter(issue => (!parsedUrl.searchParams.get('severity') || issue.severity === parsedUrl.searchParams.get('severity'))
      && (!parsedUrl.searchParams.get('kind') || issue.kind === parsedUrl.searchParams.get('kind')));
    payload = {
      ok: true, items: filtered, total: malformedIssueFeed ? null : filtered.length,
      truncated: false, data: { incomplete: issueFeedIncomplete },
    };
  }
  else if (String(url).endsWith('/capabilities')) payload = {
    ok: true, actions_enabled: false, worker: { available: true }, worker_ready: true,
  };
  else if (String(url).endsWith('/status')) payload = {
    ok: true,
    db: { db_ok: true, database_backend: 'PostgreSQL', db_size_mb: 42.5 },
    sync: { available: true, running: false, freshness: 'fresh', last_success: true },
    runtime: { status: 'ok', version: '1.2.3', cache_entries: 5, providers: ['telegram'] },
    attention: {
      failed_tasks: failedTaskSummary, failed_tasks_available: failedTaskSummaryAvailable,
      last_sync_failed_sources: lastSyncFailedSources, last_sync_rate_limited: lastSyncRateLimited,
    },
  };
  else if (String(url).endsWith('/quality/summary')) {
    if (failQualitySummary) return { ok: false, status: 503, json: async () => ({ error: 'unavailable' }) };
    payload = { ok: true, latest_audit: { jellyfin_import_state: 'checked' },
      fingerprint_coverage: { available: true, total_videos: 10, oshash: 5, phash: 3 } };
  }
  else if (parsedUrl.pathname === '/admin/api/v2/status/jellyfin') {
    if (failJellyfinStatus) return { ok: false, status: 503, json: async () => ({ error: 'unavailable' }) };
    payload = { ok: true, status: 'reachable', checked_at: '2026-10-14T10:00:00+00:00' };
  }
  else if (String(url).includes('/library/videos?')) {
    const parsed = new URL(String(url), 'http://localhost');
    const offset = Number(parsed.searchParams.get('offset') || 0);
    payload = {
      ok: true,
      items: Array.from({ length: 50 }, (_, index) => ({ id: `video-${offset + index + 1}`, title: `Video ${offset + index + 1}`, studio: 'Fixture studio' })),
      page: { offset, total: 100, total_exact: true },
      data: { available: true, stale: false, incomplete: false },
    };
  }
  else if (String(url).includes('/jobs?state=active')) payload = { ok: true, jobs: [], total: 0 };
  else throw new Error(`Unexpected request: ${url}`);
  return { ok: true, status: 200, json: async () => payload };
}
const location = { href: 'http://localhost/admin/v2/?campaign=keep#start', hash: '#start', search: '?campaign=keep' };
const windowListeners = new Map();
const historyUrls = [location.href];
let historyIndex = 0;
function setLocation(value) {
  const url = new URL(String(value), location.href);
  location.href = url.href; location.hash = url.hash; location.search = url.search;
}
function resetHistory(value) {
  setLocation(value); historyUrls.splice(0, historyUrls.length, location.href); historyIndex = 0;
}
let timerSequence = 0;
const timers = new Map();
let savedViewsFixture = null;
function runLatestTimer() {
  const entry = [...timers.entries()].at(-1);
  assert.ok(entry, 'a debounced Library load should be scheduled');
  timers.delete(entry[0]); entry[1]();
}
const history = {
  pushState(_state, _title, value) {
    const url = new URL(String(value), location.href);
    historyUrls.splice(historyIndex + 1);
    historyUrls.push(url.href); historyIndex += 1; setLocation(url);
  },
  replaceState(_state, _title, value) {
    const url = new URL(String(value), location.href);
    historyUrls[historyIndex] = url.href; setLocation(url);
  },
  back() {
    if (historyIndex <= 0) return;
    historyIndex -= 1; setLocation(historyUrls[historyIndex]); windowListeners.get('popstate')?.();
  },
  forward() {
    if (historyIndex + 1 >= historyUrls.length) return;
    historyIndex += 1; setLocation(historyUrls[historyIndex]); windowListeners.get('popstate')?.();
  },
};
const context = {
  document, location, fetch, URL, URLSearchParams, Headers, AbortController, Intl, Date, Number, String, Boolean,
  Promise, Map, Set, Math, JSON, Array, Object, RegExp, Error, console,
  localStorage: { getItem: () => savedViewsFixture, setItem() {}, removeItem() {} },
  sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  window: { AdminV2: {} },
  history,
  matchMedia: () => ({ matches: false }),
  CustomEvent: class { constructor(type, options) { this.type = type; this.detail = options?.detail; } },
  Option: class { constructor(text, value) { this.text = text; this.value = value; } },
  setTimeout(callback) { const id = ++timerSequence; timers.set(id, callback); return id; },
  clearTimeout(id) { timers.delete(id); }, setInterval: () => 1, clearInterval() {},
  requestAnimationFrame: callback => callback(),
  addEventListener(type, callback) { windowListeners.set(type, callback); },
};

(async () => {
  vm.runInNewContext(fs.readFileSync('resolver/static/admin-v2/header-status.js', 'utf8'), context);
  vm.runInNewContext(fs.readFileSync('resolver/static/admin-v2/admin-v2.js', 'utf8'), context);
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));

  assert.equal(connectionState.dataset.state, 'ok', 'Home marks a successful status response as connected');
  assert.equal(connectionLabelText, 'Connected');
  assert.equal(connectionLabelWrites, 1, 'The initial connection result is announced once');

  const rows = elements.get('attentionList').children;
  const links = rows.flatMap(row => row.children).filter(child => child.tagName === 'A');
  const actual = links.map(link => [link.getAttribute('aria-label'), link.href]);
  assert.deepEqual(actual, [
    ['Open source: Channel failed', '/admin/v2/?source_id=telegram%3A1#sources'],
    ['Open source: Channel never synced', '/admin/v2/?source_id=telegram%3A2#sources'],
    ['Review queue: Sync queue is old', '/admin/v2/?job_state=queued&job_action=sync#jobs'],
    ['Open task: Task failed', '/admin/v2/?job_id=task-9#jobs'],
    ['Open task details: Task may be stalled', '/admin/v2/?job_id=task-10#jobs'],
    ['Review unavailable: Unavailable video', '/admin/v2/#library?availability=unavailable'],
    ['Review findings: NFO metadata differs', '/admin/v2/?quality_kind=nfo_metadata_mismatch#quality'],
    ['Review backups: Database backup is overdue', '/admin/v2/#system?tab=backups'],
  ]);
  assert.equal(rows.length, issues.length, 'All issue summaries, including issues without a safe action, remain visible');
  assert.equal(elements.get('issueCount').textContent, `Showing ${issues.length} issues.`,
    'Home announces the loaded issue count');
  const backendCard = elements.get('catalogHealthCards').children.find(card =>
    card.children[0].children[0].textContent === 'Database backend');
  assert.ok(backendCard, 'Home must render the database backend in catalog health');
  assert.equal(backendCard.children[1].textContent, 'PostgreSQL', 'Home shows only the safe backend label');
  const unresolvedTaskCard = elements.get('statusCards').children.find(card =>
    card.children[0].children[0].textContent === 'Unresolved task groups');
  assert.equal(unresolvedTaskCard.children[1].textContent, '2', 'Home shows actionable failed task groups');
  const syncIssueCard = elements.get('statusCards').children.find(card =>
    card.children[0].children[0].textContent === 'Latest sync failures / HTTP 429');
  assert.equal(syncIssueCard.children[1].textContent, '3 / 2',
    'Home preserves the classic failed-source and rate-limited response counts');
  assert.match(syncIssueCard.children[2].textContent, /Failed sources \/ rate-limited responses/);
  const databaseSizeCard = elements.get('catalogHealthCards').children.find(card =>
    card.children[0].children[0].textContent === 'Database size');
  assert.equal(databaseSizeCard.children[1].textContent, '42.5 MB', 'Home preserves the classic database size field');
  const phashCard = elements.get('catalogHealthCards').children.find(card =>
    card.children[0].children[0].textContent === 'pHash coverage');
  assert.equal(phashCard.children[1].textContent, '3 / 10 (30%)', 'Dashboard reports pHash coverage from the authoritative summary');
  assert.match(phashCard.children[2].textContent, /partial by design/,
    'Dashboard does not imply pHash coverage is expected for every video');
  const oshashCard = elements.get('catalogHealthCards').children.find(card =>
    card.children[0].children[0].textContent === 'OSHASH coverage');
  assert.equal(oshashCard.children[1].textContent, '5 / 10 (50%)');
  const serviceValues = new Map(elements.get('serviceList').children.map(row =>
    [row.children[0].textContent, row.children[1].textContent]));
  assert.equal(serviceValues.get('Application version'), '1.2.3', 'Home preserves the classic application version field');
  assert.equal(serviceValues.get('Synchronization'), 'Last run succeeded', 'Home preserves the classic sync status field');
  assert.equal(serviceValues.get('Resolver'), 'Operational', 'Home shows resolver status with the configuration details');
  assert.equal(serviceValues.get('Background worker'), 'Operational', 'Dashboard reflects worker capability state');
  assert.equal(serviceValues.get('Jellyfin connection'), 'Reachable', 'Dashboard shows the sanitized Jellyfin reachability probe');
  assert.equal(serviceValues.get('Jellyfin import check (latest audit)'), 'Checked in latest audit',
    'Dashboard describes Jellyfin evidence as coming from the latest audit, not a live probe');
  assert(calls.includes('/admin/api/v2/status/jellyfin'), 'Dashboard loads the bounded Jellyfin health probe');
  assert(calls.includes('/admin/api/v2/issues?limit=50&offset=0'), 'Home must load the V2 issue feed');
  assert(calls.includes('/admin/api/v2/quality/summary'), 'Dashboard uses the bounded quality summary for fingerprint coverage');

  elements.get('issueSeverity').value = 'high';
  elements.get('issueSeverity').listeners.change();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(new URLSearchParams(location.search).get('issue_severity'), 'high', 'Home saves issue severity in the URL');
  assert.equal(new URLSearchParams(location.search).get('campaign'), 'keep', 'Home issue filters preserve unrelated query parameters');
  assert.equal(elements.get('attentionList').children.length, 4, 'Home applies the selected issue severity');
  assert.equal(elements.get('issueCount').textContent, 'Showing 4 issues.', 'Issue count follows the severity filter');
  assert(calls.includes('/admin/api/v2/issues?limit=50&offset=0&severity=high'), 'Home sends severity to the issues API');
  elements.get('issueKind').value = 'job_stalled';
  elements.get('issueKind').listeners.change();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(new URLSearchParams(location.search).get('issue_kind'), 'job_stalled', 'Home saves issue type in the URL');
  assert.equal(new URLSearchParams(location.search).get('campaign'), 'keep', 'Home combined filters preserve unrelated query parameters');
  assert.equal(elements.get('attentionList').children.length, 1, 'Home applies combined severity and type filters');
  assert.equal(elements.get('issueCount').textContent, 'Showing 1 issue.', 'Combined filters update the announced count');
  assert(calls.includes('/admin/api/v2/issues?limit=50&offset=0&severity=high&kind=job_stalled'), 'Home sends both issue filters to the API');
  history.back();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(elements.get('issueKind').value, '', 'Back restores the previous Home issue filters');
  assert.equal(elements.get('attentionList').children.length, 4, 'Back reloads the previous severity-filtered issues');
  history.forward();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(elements.get('issueKind').value, 'job_stalled', 'Forward restores the Home issue type');
  assert.equal(elements.get('attentionList').children.length, 1, 'Forward reloads the combined issue filters');

  issueFeedIncomplete = true;
  documentListeners.get('click')({ target: { closest: selector => selector === '[data-action]' ? { dataset: { action: 'refresh-status' } } : null } });
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(elements.get('issueCount').textContent, 'Showing 1 returned issue; the total may be incomplete.',
    'Home qualifies the count when some issue sources are unavailable');
  assert.equal(elements.get('issueCompleteness').hidden, false, 'Home exposes the incomplete-feed explanation');
  issueFeedIncomplete = false;
  documentListeners.get('click')({ target: { closest: selector => selector === '[data-action]' ? { dataset: { action: 'refresh-status' } } : null } });
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));

  const previouslyLoadedIssueCount = elements.get('attentionList').children.length;
  failIssueFeed = true;
  documentListeners.get('click')({ target: { closest: selector => selector === '[data-action]' ? { dataset: { action: 'refresh-status' } } : null } });
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(elements.get('issueCount').textContent, 'Issue count unavailable; showing previously loaded results.',
    'A failed refresh marks the issue count unavailable without presenting stale counts as current');
  assert.equal(elements.get('attentionList').children.length, previouslyLoadedIssueCount,
    'A failed refresh preserves the previously loaded issue rows');
  failIssueFeed = false;

  malformedIssueFeed = true;
  documentListeners.get('click')({ target: { closest: selector => selector === '[data-action]' ? { dataset: { action: 'refresh-status' } } : null } });
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(elements.get('issueCount').textContent, 'Issue count unavailable; showing previously loaded results.',
    'A null issue total cannot be coerced into a false zero count');
  assert.equal(elements.get('attentionList').children.length, previouslyLoadedIssueCount,
    'A malformed issue response preserves the last valid rows');
  malformedIssueFeed = false;

  failedTaskSummaryAvailable = false;
  lastSyncFailedSources = null;
  lastSyncRateLimited = null;
  failCapabilities = true;
  documentListeners.get('click')({ target: { closest: selector => selector === '[data-action]' ? { dataset: { action: 'refresh-status' } } : null } });
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(elements.get('statusContent').hidden, false, 'A capabilities failure must not hide a successfully loaded system status');
  assert.match(elements.get('statusState').children[0].textContent, /write access could not be verified/i,
    'Home must disclose when capabilities are unavailable while status remains current');
  const panelAccess = elements.get('serviceList').children.find(row => row.children[0].textContent === 'Panel access');
  assert.match(panelAccess.children[1].textContent, /unavailable.*writes need verification/i,
    'Home must not imply read-only or writable access when capability state is unknown');
  const unavailableTaskCard = elements.get('statusCards').children.find(card =>
    card.children[0].children[0].textContent === 'Unresolved task groups');
  assert.equal(unavailableTaskCard.children[1].textContent, 'No data',
    'Home must not report zero failed tasks when the failure summary is incomplete');
  const unavailableSyncIssueCard = elements.get('statusCards').children.find(card =>
    card.children[0].children[0].textContent === 'Latest sync failures / HTTP 429');
  assert.equal(unavailableSyncIssueCard.children[1].textContent, 'No data',
    'Home must not show zero sync failures when the latest sync counts are unavailable');
  assert.equal(connectionLabelWrites, 1, 'An unchanged connection state is not re-announced on refresh');
  assert(calls.includes('/admin/api/v2/issues?limit=50&offset=0&severity=high&kind=job_stalled'),
    'Refreshing Home status preserves the active issue filters');

  failedTaskSummaryAvailable = true;
  failCapabilities = false;
  failStatus = true;
  documentListeners.get('click')({ target: { closest: selector => selector === '[data-action]' ? { dataset: { action: 'refresh-status' } } : null } });
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(elements.get('statusContent').hidden, false, 'A later status failure must preserve the previously loaded status');
  assert.match(elements.get('statusState').children[0].textContent, /out of date/i, 'A failed status refresh must visibly mark retained data as stale');
  assert.equal(connectionState.dataset.state, 'error', 'A failed status refresh marks the connection unavailable');
  assert.equal(connectionLabelText, 'Connection unavailable');
  assert.equal(connectionLabelWrites, 2, 'Only the changed connection state is announced');
  failStatus = false;

  const routeTitles = {
    start: 'Dashboard', library: 'Library', sources: 'Sources', organize: 'Catalog Health',
    quality: 'Catalog Health', performers: 'Performers', jobs: 'Jobs & Automation', system: 'System',
  };
  const onHashChange = windowListeners.get('hashchange');
  assert.equal(typeof onHashChange, 'function', 'The router must listen for direct hash navigation');
  for (const route of expectedRoutes) {
    location.hash = `#${route}`;
    onHashChange();

    const visibleViews = views.filter(view => !view.hidden).map(view => view.dataset.view);
    const expectedView = ['start', 'library', 'organize', 'performers'].includes(route) ? route : 'operational';
    assert.deepEqual(visibleViews, [expectedView], `${route} must select exactly its intended view`);
    const expectedPrimaryRoute = route === 'organize' ? 'quality' : route;
    assert.deepEqual(navLinks.filter(link => link.getAttribute('aria-current') === 'page').map(link => link.dataset.route), [expectedPrimaryRoute],
      `${route} must activate its primary v3.1 section or legacy-compatible parent`);
    assert.equal(elements.get('crumbCurrent').textContent, routeTitles[route], `${route} must update the breadcrumb`);
    if (['quality', 'sources', 'jobs', 'system'].includes(route)) {
      assert.equal(elements.get('operationalTitle').textContent, routeTitles[route],
        `${route} must name the shared operational view after navigation`);
    }
  }
  location.hash = '#quality?tab=duplicates';
  onHashChange();
  assert.deepEqual(views.filter(view => !view.hidden).map(view => view.dataset.view), ['organize'],
    'Catalog Health duplicate links select the existing organizer view');
  assert.equal(elements.get('organizeDuplicatesPane').hidden, false);
  assert.equal(elements.get('organizePeoplePane').hidden, true);
  assert.equal(elements.get('organizeTabList').hidden, true,
    'canonical Catalog Health hides the legacy Organize tab bar to avoid duplicate tabs');
  assert.deepEqual(navLinks.filter(link => link.getAttribute('aria-current') === 'page').map(link => link.dataset.route), ['quality'],
    'Catalog Health subtabs keep the Catalog Health sidebar destination active');
  assert.equal(sectionTabLinks.filter(link => link.className.includes('catalog-health-tabs')
    && link.dataset.sectionTab === 'duplicates' && link.getAttribute('aria-current') === 'page').length, 2,
  'the current Catalog Health tab is marked in both legacy and canonical containers');
  location.hash = '#quality?tab=quarantine';
  onHashChange();
  assert.equal(elements.get('organizeQuarantinePane').hidden, false,
    'Catalog Health quarantine deep links select the existing quarantine panel');
  location.hash = '#quality?tab=enrichment';
  onHashChange();
  assert.equal(elements.get('qualityView').hidden, false);
  assert.equal(elements.get('qualityIssuesPane').hidden, true);
  assert.equal(elements.get('qualityEnrichmentPane').hidden, false,
    'the Enrichment tab opens its single proposal/progress implementation');
  assert.equal(sectionTabLinks.filter(link => link.className.includes('catalog-health-tabs')
    && link.dataset.sectionTab === 'enrichment' && link.getAttribute('aria-current') === 'page').length, 2,
  'Catalog Health Enrichment is marked active in both canonical and legacy-compatible tab bars');
  assert.equal(lastAdminNavigation.detail.tab, 'enrichment',
    'route events carry the selected Catalog Health tab to its existing controllers');
  location.hash = '#performers?tab=directory';
  onHashChange();
  assert.deepEqual(views.filter(view => !view.hidden).map(view => view.dataset.view), ['organize'],
    'Performer Directory can reuse the current directory implementation');
  assert.equal(elements.get('organizePeoplePane').hidden, false);
  assert.equal(elements.get('organizeTitle').textContent, 'Performer Directory');
  assert.equal(elements.get('organizeTabList').hidden, true,
    'canonical Performer Directory hides the legacy People/Duplicates tab bar');
  assert.deepEqual(navLinks.filter(link => link.getAttribute('aria-current') === 'page').map(link => link.dataset.route), ['performers'],
    'the Performer Directory deep link activates Performers in the sidebar');
  assert.equal(sectionTabLinks.filter(link => link.className.includes('performer-section-tabs')
    && link.dataset.sectionTab === 'directory' && link.getAttribute('aria-current') === 'page').length, 2,
  'the current Performer tab is marked in both canonical and reused directory containers');
  location.hash = '#organize?tab=people';
  onHashChange();
  assert.equal(elements.get('organizePeoplePane').hidden, false,
    'the legacy organizer people tab remains an alias for Performer Directory');
  assert.deepEqual(navLinks.filter(link => link.getAttribute('aria-current') === 'page').map(link => link.dataset.route), ['performers']);
  location.hash = '#performers?tab=matching';
  onHashChange();
  assert.equal(elements.get('performerProposalsPane').hidden, true);
  assert.equal(elements.get('performerNameMatchingPane').hidden, false,
    'Name Matching has a bookmarkable section tab');
  location.hash = '#organize';
  onHashChange();
  assert.equal(elements.get('organizeTabList').hidden, true,
    'legacy organizer URLs still resolve without restoring duplicate canonical tabs');
  assert.equal(elements.get('organizeDuplicatesPane').hidden, false,
    'the legacy #organize deep link still opens duplicate suggestions');
  location.hash = '#unknown-route';
  onHashChange();
  assert.deepEqual(views.filter(view => !view.hidden).map(view => view.dataset.view), ['start'],
    'Unknown hashes must fall back to the Home view');
  assert.deepEqual(navLinks.filter(link => link.getAttribute('aria-current') === 'page').map(link => link.dataset.route), ['start'],
    'Unknown hashes must select Home in the primary navigation');
  assert.equal(elements.get('crumbCurrent').textContent, 'Dashboard', 'Unknown hashes must reset the breadcrumb to Dashboard');

  resetHistory('http://localhost/admin/v2/?q=alpha&campaign=keep#library');
  onHashChange();
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(libraryFields.get('q').value, 'alpha', 'Library restores its search from the URL');
  assert.equal(new URLSearchParams(location.search).get('sort'), 'first_seen');
  libraryFields.get('q').value = 'beta';
  libraryForm.listeners.input({ target: libraryFields.get('q') });
  libraryFields.get('q').value = 'gamma';
  libraryForm.listeners.input({ target: libraryFields.get('q') });
  assert.equal(new URLSearchParams(location.search).get('q'), 'gamma', 'search updates the URL before the request debounce');
  assert.equal(new URLSearchParams(location.search).get('campaign'), 'keep', 'Library URL updates preserve unrelated query parameters');
  assert.equal(location.hash, '#library', 'Library URL updates preserve the active route hash');
  assert.equal(historyUrls.length, 2, 'typing one search burst creates one Back step');
  runLatestTimer();
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
  assert(calls.some(url => url.includes('/library/videos?q=gamma&sort=first_seen&limit=50&offset=0')),
    'the settled search requests the URL state shown to the operator');
  const gammaSelection = elements.get('videoGrid').children[0].children[0].children[0];
  gammaSelection.checked = true;
  gammaSelection.listeners.change();
  assert.equal(elements.get('bulkSelectionCount').textContent, '1 selected');
  history.back();
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(libraryFields.get('q').value, 'alpha', 'Back restores the prior Library filter');
  assert.equal(elements.get('bulkSelectionCount').textContent, '0 selected', 'Back to different filters clears the hidden selection');
  assert.equal(elements.get('reviewQuarantine').disabled, true, 'bulk actions stay disabled after history changes filters');
  assert.equal(new URLSearchParams(location.search).get('campaign'), 'keep', 'Back preserves unrelated query parameters');
  assert(calls.some(url => url.includes('/library/videos?q=alpha&sort=first_seen&limit=50&offset=0')),
    'restored filter state triggers a matching catalog request');
  elements.get('pageNext').listeners.click();
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(new URLSearchParams(location.search).get('offset'), '50', 'page navigation is linkable');
  const pageTwoSelection = elements.get('videoGrid').children[0].children[0].children[0];
  pageTwoSelection.checked = true;
  pageTwoSelection.listeners.change();
  assert.equal(elements.get('bulkSelectionCount').textContent, '1 selected');
  history.back();
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(new URLSearchParams(location.search).get('offset'), null, 'Back restores the previous page');
  assert.equal(elements.get('pageInfo').textContent, 'Page 1 of 2');
  assert.equal(elements.get('bulkSelectionCount').textContent, '1 selected', 'page navigation preserves selections across pages');
  history.forward();
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(elements.get('pageInfo').textContent, 'Page 2 of 2', 'Forward restores the next page');
  assert.equal(elements.get('bulkSelectionCount').textContent, '1 selected', 'Forward keeps the multi-page selection');

  resetHistory('http://localhost/admin/v2/?q=alpha&campaign=keep#library');
  onHashChange();
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
  const callsBeforePendingSearch = calls.length;
  libraryFields.get('q').value = 'beta';
  libraryForm.listeners.input({ target: libraryFields.get('q') });
  libraryFields.get('q').value = 'gamma';
  libraryForm.listeners.input({ target: libraryFields.get('q') });
  const timersBeforeBack = timers.size;
  history.back();
  assert.ok(timers.size < timersBeforeBack, 'Back cancels the pending Library search debounce');
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(libraryFields.get('q').value, 'alpha', 'Back during typing restores the prior search');
  assert.equal(calls.slice(callsBeforePendingSearch).some(url => url.includes('/library/videos?q=gamma')),
    false, 'the cancelled search does not issue a stale request after Back');
  history.forward();
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(libraryFields.get('q').value, 'gamma', 'Forward restores the grouped search entry');

  savedViewsFixture = JSON.stringify([{ name: 'Unavailable catalog', filters: { q: 'saved title', availability: 'unavailable', sort: 'title' } }]);
  const historyBeforeSavedView = historyUrls.length;
  elements.get('savedViews').listeners.change({ target: { value: '0' } });
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(historyUrls.length, historyBeforeSavedView + 1, 'applying a saved view creates one history entry');
  assert.equal(new URLSearchParams(location.search).get('q'), 'saved title');
  assert.equal(new URLSearchParams(location.search).get('availability'), 'unavailable');
  assert.equal(new URLSearchParams(location.search).get('sort'), 'title');
  assert.equal(new URLSearchParams(location.search).get('campaign'), 'keep');
  assert(calls.some(url => url.includes('/library/videos?q=saved+title&availability=unavailable&sort=title&limit=50&offset=0')),
    'applying a saved view fetches its exact URL-backed filters');
  history.back();
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(libraryFields.get('q').value, 'gamma', 'Back restores the filters before a saved view');
  history.forward();
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(libraryFields.get('availability').value, 'unavailable', 'Forward restores the saved view filters');
  const historyBeforeClearFilters = historyUrls.length;
  elements.get('clearFilters').listeners.click();
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(historyUrls.length, historyBeforeClearFilters + 1, 'clearing filters creates one history entry');
  assert.equal(libraryFields.get('q').value, '');
  assert.equal(libraryFields.get('availability').value, '');
  assert.equal(new URLSearchParams(location.search).get('q'), null);
  history.back();
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(libraryFields.get('q').value, 'saved title', 'Back restores filters before clearing');
  assert.equal(libraryFields.get('availability').value, 'unavailable');
  history.forward();
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(libraryFields.get('q').value, '', 'Forward restores the cleared filters');

  const onKeydown = documentListeners.get('keydown');
  assert.equal(typeof onKeydown, 'function', 'Admin v2 must register its keyboard focus handler');
  const tab = (shiftKey = false) => {
    const event = { key: 'Tab', shiftKey, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
    onKeydown(event);
    return event;
  };
  const modalFocusSelector = 'a[href],button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled),[tabindex]:not([tabindex="-1"])';
  const modalFirst = new Element('button');
  const modalMiddle = new Element('button');
  const modalLast = new Element('button');
  operationDialog.children = [modalFirst, modalMiddle, modalLast];
  operationDialog.queryResults.set(modalFocusSelector, [modalFirst, modalMiddle, modalLast]);
  operationDialog.open = true;
  document.activeElement = new Element('body');
  assert.equal(tab().defaultPrevented, true, 'Tab from outside a modal must enter at its first control');
  assert.equal(document.activeElement, modalFirst);
  assert.equal(tab(true).defaultPrevented, true, 'Shift+Tab from the first modal control must wrap to the last');
  assert.equal(document.activeElement, modalLast);
  assert.equal(tab().defaultPrevented, true, 'Tab from the last modal control must wrap to the first');
  assert.equal(document.activeElement, modalFirst);
  document.activeElement = modalMiddle;
  assert.equal(tab().defaultPrevented, false, 'Tab between modal controls must retain native navigation');
  operationDialog.queryResults.set(modalFocusSelector, []);
  document.activeElement = new Element('body');
  assert.equal(tab().defaultPrevented, true, 'An empty modal must receive focus instead of losing focus');
  assert.equal(document.activeElement, operationDialog);
  operationDialog.open = false;

  const drawerFirst = new Element('button');
  const drawerLast = new Element('button');
  detailDrawer.children = [drawerFirst, drawerLast];
  detailOverlay.children = [detailDrawer];
  detailOverlay.queryResults.set(modalFocusSelector, [drawerFirst, drawerLast]);
  elements.get('detailOverlay').hidden = false;
  detailOverlay.open = true;
  document.activeElement = new Element('body');
  assert.equal(tab().defaultPrevented, true, 'Tab from outside the detail drawer must enter at its first control');
  assert.equal(document.activeElement, drawerFirst);
  assert.equal(tab(true).defaultPrevented, true, 'Shift+Tab from the first drawer control must wrap to the last');
  assert.equal(document.activeElement, drawerLast);
  assert.equal(tab().defaultPrevented, true, 'Tab from the last drawer control must wrap to the first');
  assert.equal(document.activeElement, drawerFirst);
  detailOverlay.queryResults.set(modalFocusSelector, []);
  document.activeElement = new Element('body');
  assert.equal(tab().defaultPrevented, true, 'An empty detail drawer must receive focus instead of losing focus');
  assert.equal(document.activeElement, detailOverlay);
  detailOverlay.open = false;

  failQualitySummary = true;
  failJellyfinStatus = true;
  const dashboardUrl = new URL(location.href);
  dashboardUrl.hash = '#start';
  history.pushState({}, '', dashboardUrl);
  windowListeners.get('popstate')?.();
  for (let i = 0; i < 6; i += 1) await new Promise(resolve => setImmediate(resolve));
  const unavailablePhashCard = elements.get('catalogHealthCards').children.find(card =>
    card.children[0].children[0].textContent === 'pHash coverage');
  assert.equal(unavailablePhashCard.children[1].textContent, 'No data',
    'a failed quality summary does not invent a fingerprint count');
  assert.equal(elements.get('statusContent').hidden, false,
    'a failed quality summary does not hide the rest of the Dashboard');
  assert.equal(elements.get('serviceList').children.length > 0, true,
    'service status remains available when fingerprint coverage cannot be loaded');
  const jellyfinWithoutAudit = new Map(elements.get('serviceList').children.map(row =>
    [row.children[0].textContent, row.children[1].textContent]));
  assert.equal(jellyfinWithoutAudit.get('Jellyfin import check (latest audit)'), 'No data',
    'Dashboard does not claim current Jellyfin health when the quality summary is unavailable');
  assert.equal(jellyfinWithoutAudit.get('Jellyfin connection'), 'No data',
    'A failed Jellyfin probe becomes No data without hiding the Dashboard');
  assert.deepEqual(systemTabLinks.map(link => link.dataset.systemTab), ['maintenance', 'diagnostics', 'backups', 'activity']);
  const activityUrl = new URL(location.href); activityUrl.hash = '#system?tab=activity';
  history.pushState({}, '', activityUrl); windowListeners.get('popstate')?.();
  assert.equal(elements.get('systemActivityView').hidden, false, 'System activity is reachable as a bookmarkable tab');
  assert.equal(elements.get('systemMaintenanceView').hidden, true, 'System shows only the selected tab');
  assert.equal(systemTabLinks.find(link => link.dataset.systemTab === 'activity').getAttribute('aria-current'), 'page');
  const backupUrl = new URL(location.href); backupUrl.hash = '#system?tab=backups';
  history.pushState({}, '', backupUrl); windowListeners.get('popstate')?.();
  assert.equal(elements.get('systemBackupView').hidden, false, 'the Backups tab has a stable route');
  assert.equal(elements.get('systemActivityView').hidden, true);
  const legacyLinkcheck = new URL(location.href); legacyLinkcheck.hash = '#system?tab=linkcheck';
  history.pushState({}, '', legacyLinkcheck); windowListeners.get('popstate')?.();
  assert.equal(elements.get('qualityView').hidden, false, 'the old System link-check route redirects to Catalog Health');
  assert.match(routerSource, /\['linkcheck', 'links', 'availability'\]\.includes\(tab\)[\s\S]*?qualityTab: 'issues'[\s\S]*?focusTarget: '#linkcheckTitle'/,
    'the old link-check route opens the authoritative Catalog Health issues tab and focus target');
  assert.match(fs.readFileSync('resolver/static/admin-v2/sources.js', 'utf8'),
    /route === 'system' && \['sync', 'sync-activity', 'sync_activity', 'history'\]\.includes\(tab\)/,
    'the old global sync-history hashes still render the Sources history view');
  process.stdout.write('PASS: seven-section navigation, legacy route/tab deep links, Library URL history, and modal/detail-drawer focus boundaries\n');
})().catch(error => { console.error(error); process.exitCode = 1; });
