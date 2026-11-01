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
  scrollIntoView() {}
  focus() { if (activeDocument) activeDocument.activeElement = this; }
}
const textOf = node => `${node?.textContent || ''} ${(node?.children || []).map(textOf).join(' ')}`.trim();

const html = fs.readFileSync('resolver/templates/admin_v2.html', 'utf8');
const routerSource = fs.readFileSync('resolver/static/admin-v2/admin-v2.js', 'utf8');

assert.match(html, /class="view"[^>]*data-view="operational"/,
  'Sources, Tasks, and System must use the named shared operational view');
assert.doesNotMatch(html, /roadmapCard|Coming soon|legacyRoute/,
  'The implemented v2 views must not retain the obsolete coming-soon fallback');

// Removed manual triage entry points must not leave markup or behaviour behind.
assert.doesNotMatch(html, /command-palette|commandPalette|commandTrigger|data-command/,
  'The command palette must be fully removed from the template');
assert.doesNotMatch(html, /command-palette\.js/, 'The command palette script must not be loaded');
assert.doesNotMatch(routerSource, /commandPalette|open-palette|command-palette/,
  'The router must not keep palette-only behaviour');

const topbar = html.slice(html.indexOf('<header class="topbar">'), html.indexOf('</header>'));
assert.deepEqual([...topbar.matchAll(/\bid="([^"]+)"/g)].map(([, id]) => id),
  ['menuButton', 'crumbCurrent', 'serviceStatus', 'themeToggle'],
  'The header keeps the title, one aggregate service control, and the theme toggle only');
assert.match(topbar, /<a class="service-status" id="serviceStatus" href="#system"/,
  'The aggregate service control leads to System');
assert.doesNotMatch(html, /id="connectionState"/, 'The removed per-source connection chip must not return');

assert.match(html, /id="issueCount"[^>]*role="status"[^>]*aria-atomic="true"/,
  'Home announces issue result counts accessibly');
assert.match(html, /<details class="all-problems" id="allProblems">\s*<summary>All problems<\/summary>/,
  'The full problem list is available but collapsed by default');
assert.doesNotMatch(html, /<details[^>]*id="allProblems"[^>]*open/,
  'The full problem list must not expand on load');

const startView = html.slice(html.indexOf('data-view="start"'), html.indexOf('data-view="library"'));
assert.match(startView, /id="statusCards"/, 'Overview renders its summary tiles into the metric grid');
assert.match(startView, /id="problemGroups"/, 'Overview renders grouped problem summaries');
assert.match(startView, /id="attentionList"/, 'Overview keeps the full filtered problem list reachable');
assert.doesNotMatch(startView, /catalogHealthCards|pHash|OSHASH|Database size|db_size/,
  'Overview drops the detailed catalog health statistics');

// Stage 3 promotes the catalog tools that used to hide behind "More tools" to real Library tabs.
assert.doesNotMatch(html, /id="libraryMoreTools"/, 'Library must not keep the temporary tool list');
for (const id of ['libraryTabList', 'libraryVideosTab', 'libraryDuplicatesTab', 'librarySuggestionsTab', 'libraryQuarantineTab',
  'libraryVideosPane', 'libraryDuplicatesPane', 'librarySuggestionsPane', 'libraryQuarantinePane']) {
  assert.ok(html.includes(`id="${id}"`), `Library must expose ${id}`);
}
assert.match(html, /<nav class="section-tabs library-section-tabs" id="libraryTabList" role="tablist"/,
  'the Library tab strip is an ARIA tablist');
assert.deepEqual([...html.matchAll(/<a href="#library[^"]*" id="library(\w+)Tab" role="tab" aria-selected="(true|false)" aria-controls="library\1Pane"/g)]
  .map(([, tab]) => tab), ['Videos', 'Duplicates', 'Suggestions', 'Quarantine'],
  'every Library tab keeps its own bookmarkable address and pane pairing');
assert.match(html, /<details class="more-filters" id="libraryMoreFilters">\s*<summary>More filters/,
  'the remaining Library filters collapse behind More filters');
assert.match(html, /id="duplicateMatchCatalog"[^>]*aria-pressed="true"[^>]*>\s*Catalog matches\s*<\/button>[\s\S]{0,160}id="duplicateMatchVisual"[^>]*aria-pressed="false"[^>]*>\s*Visual matches/,
  'the Duplicates tab switches between catalog and visual matches in place');
assert.doesNotMatch(html, /<section class="view"[^>]*data-view="organize"/, 'the Organize host view must not return');
assert.match(html, /<nav class="section-tabs performer-section-tabs" id="performersTabList" role="tablist"/,
  'the Performers tab strip is an ARIA tablist');
// The router marks each tab selected by deriving "<name>Tab" from "<name>Pane", so the
// template must keep that convention or aria-selected silently stops updating.
for (const [, pane] of html.matchAll(/id="([A-Za-z]+)Pane" role="tabpanel"/g)) {
  assert.ok(html.includes(`id="${pane}Tab"`), `the ${pane}Pane panel needs a matching ${pane}Tab anchor`);
  assert.ok(html.includes(`aria-labelledby="${pane}Tab"`), `the ${pane}Pane panel must be labelled by ${pane}Tab`);
}
assert.match(html, /href="#performers\?tab=review" id="performerReviewTab"[^\n]*aria-controls="performerReviewPane"/,
  'the Review matches tab keeps its own address and pane pairing');
assert.match(html, /id="performerReviewPane"[\s\S]{0,4000}id="performerProposalsPane"[\s\S]{0,4000}id="performerNameMatchingPane"/,
  'Review matches holds both the recognition proposals and the name variants');
assert.doesNotMatch(html, /systemMoreTools|systemActivityView|diagnostics\/playback|linkcheckRows|enrichmentRows/,
  'retired reports and their panels are absent from the System interface');
assert.match(html, /data-system-tab="status"/, 'System has a Status tab');
assert.match(html, /data-system-tab="backups"/, 'System has a Backups tab');
assert.match(html, /data-system-tab="maintenance"/, 'System has a Maintenance tab');
assert.match(html, /id="writeAccessNotice"/, 'System reports why writes are limited');
assert.match(html, /id="systemProbeRows"/, 'System retains the bounded service probes');

const elements = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(([, id]) => [id, new Element('div')]));
const serviceChip = elements.get('serviceStatus');
const chipLabel = new Element('span');
let chipLabelText = 'Checking services…';
let chipLabelWrites = 0;
Object.defineProperty(chipLabel, 'textContent', {
  get: () => chipLabelText,
  set: value => { chipLabelWrites += 1; chipLabelText = String(value); },
});
serviceChip.queryResults.set('span:last-child', chipLabel);
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
const expectedRoutes = ['start', 'library', 'sources', 'performers', 'jobs', 'schedules', 'system'];
assert.deepEqual(navLinks.map(link => link.dataset.route),
  ['start', 'library', 'performers', 'sources', 'jobs', 'schedules', 'system'],
  'the primary navigation must expose exactly the seven Stage 2 destinations in order');
const primaryLabels = [...html.matchAll(/<a\b[^>]*data-route="[^"]+"[^>]*>([\s\S]*?)<\/a>/g)]
  .map(([, contents]) => contents.replace(/<span\b[^>]*aria-hidden="true"[^>]*>[\s\S]*?<\/span>/g, '')
    .replace(/<[^>]*>/g, '').replace(/&amp;/g, '&').trim());
assert.deepEqual(primaryLabels, ['Overview', 'Library', 'Performers', 'Sources', 'Tasks', 'Schedules', 'System'],
  'the primary navigation uses the Stage 2 section names');

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
assert.doesNotMatch(detailRenderer, /Open file consistency audits|go\('quality'/,
  'removed manual audit screens are not linked from video details');
for (const route of expectedRoutes) {
  assert.match(routerSource, new RegExp(`\\b${route}:\\s*\\{\\s*title:`),
    `the Admin v2 router must define the ${route} destination`);
}
assert.match(routerSource, /jobs: \{ title: 'Tasks'/, 'the queue destination is named Tasks');
assert.match(routerSource, /start: \{ title: 'Overview'/, 'the landing destination is named Overview');
assert.match(routerSource, /const navOwner = route => route;/,
  'retired Catalog Health reports no longer own a navigation section');

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
// The server summarizes the complete open set before filtering and pagination; the client only renders it.
const problemSummary = {
  total_open: 9,
  groups_total: 7,
  incomplete: false,
  groups: [
    { kind: 'job_failure', operation_id: 'sync', severity: 'high', label: 'Task failed — sync',
      occurrences: 12, objects: 4, last_seen: '2026-10-14T08:30:00+00:00',
      action: { label: 'Review failures', route: '/admin/v2/?job_state=failed&job_action=sync#jobs' } },
    { kind: 'sync_failure', operation_id: 'telegram:1', severity: 'high', label: 'Channel failed — Action Channel',
      occurrences: 3, objects: 1, last_seen: '2026-10-14T09:00:00+00:00',
      action: { label: 'Open source', route: '/admin/v2/?source_id=telegram%3A1#sources' } },
    { kind: 'backup', operation_id: 'backup', severity: 'high', label: 'Database backup is overdue',
      occurrences: 1, objects: 1, last_seen: '2026-10-14T05:00:00+00:00',
      action: { label: 'Review backups', route: '/admin/v2/#system?tab=backups' } },
    { kind: 'quality_finding', operation_id: 'video-9', severity: 'medium', label: 'NFO metadata differs',
      occurrences: 7, objects: 7, last_seen: '2026-10-14T07:00:00+00:00',
      action: { label: 'Review findings', route: '/admin/v2/?quality_kind=nfo_metadata_mismatch#quality' } },
    { kind: 'sync_backlog', operation_id: 'sync', severity: 'medium', label: 'Sync queue is old',
      occurrences: 40, objects: 1, last_seen: '2026-10-14T06:00:00+00:00',
      action: { label: 'Review queue', route: '/admin/v2/?job_state=queued&job_action=sync#jobs' } },
  ],
};
const FIRST_PAGE_SIZE = 2;
const calls = [];
let failCapabilities = false;
let failStatus = false;
let failQualitySummary = false;
let failJellyfinStatus = false;
let writableCapabilities = false;
let issueFeedIncomplete = false;
let failIssueFeed = false;
let malformedIssueFeed = false;
const syncFixture = {
  available: true, running: false, freshness: 'fresh', last_success: true, history_available: true,
  last_run: { started_at: '2026-10-14T09:40:00+00:00', ended_at: '2026-10-14T10:00:00+00:00', success: true },
};
const expectedSyncTime = new Intl.DateTimeFormat('en-US', { dateStyle: 'medium', timeStyle: 'short' })
  .format(new Date(syncFixture.last_run.ended_at));
async function fetch(url) {
  calls.push(String(url));
  if (failCapabilities && String(url).endsWith('/capabilities')) return { ok: false, status: 503, json: async () => ({ detail: 'unavailable' }) };
  if (failStatus && (String(url).endsWith('/status') || String(url).endsWith('/status/sync'))) return { ok: false, status: 503, json: async () => ({ detail: 'unavailable' }) };
  let payload;
  const parsedUrl = new URL(String(url), 'http://localhost');
  if (parsedUrl.pathname === '/admin/api/v2/issues') {
    if (failIssueFeed) return { ok: false, status: 503, json: async () => ({ detail: 'unavailable' }) };
    const filtered = issues.filter(issue => (!parsedUrl.searchParams.get('severity') || issue.severity === parsedUrl.searchParams.get('severity'))
      && (!parsedUrl.searchParams.get('kind') || issue.kind === parsedUrl.searchParams.get('kind')));
    const offset = Number(parsedUrl.searchParams.get('offset') || 0);
    // Only the first page is short so "Load more" appends the remaining fixtures.
    const page = offset === 0 ? filtered.slice(0, FIRST_PAGE_SIZE) : filtered.slice(offset);
    payload = {
      ok: true,
      items: page,
      total: malformedIssueFeed ? null : filtered.length,
      truncated: offset + page.length < filtered.length,
      summary: { ...problemSummary, incomplete: issueFeedIncomplete },
      data: { incomplete: issueFeedIncomplete },
    };
  }
  else if (parsedUrl.pathname === '/admin/api/v2/quality/summary') {
    if (failQualitySummary) return { ok: false, status: 503, json: async () => ({ error: 'unavailable' }) };
    payload = { ok: true, latest_audit: { jellyfin_import_state: 'checked', created_at: '2026-10-14T09:00:00+00:00' } };
  }
  else if (parsedUrl.pathname === '/admin/api/v2/status/jellyfin') {
    if (failJellyfinStatus) return { ok: false, status: 503, json: async () => ({ error: 'unavailable' }) };
    payload = { ok: true, status: 'reachable', checked_at: '2026-10-14T10:00:00+00:00' };
  }
  else if (parsedUrl.pathname === '/admin/api/v2/status/sync') payload = { ok: true, sync: syncFixture };
  else if (parsedUrl.pathname === '/admin/api/v2/capabilities') payload = {
    ok: true, actions_enabled: writableCapabilities, worker: { available: true }, worker_ready: true,
    read_only_reason: writableCapabilities ? undefined : 'admin_disabled',
  };
  else if (parsedUrl.pathname === '/admin/api/v2/status') payload = {
    ok: true,
    db: { db_ok: true, database_backend: 'PostgreSQL', videos_total: 1234 },
    sync: syncFixture,
    runtime: { status: 'ok', version: '1.2.3', cache_entries: 5, providers: ['telegram'] },
    attention: { pending_tasks: 4 },
  };
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
  else if (String(url).includes('/jobs?state=active')) payload = { ok: true, jobs: [], total: 0, active_total: 4, data: { available: true } };
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
  const flush = async (ticks = 5) => {
    for (let index = 0; index < ticks; index += 1) await new Promise(resolve => setImmediate(resolve));
  };
  const refresh = async () => {
    documentListeners.get('click')({ target: { closest: selector => selector === '[data-action]' ? { dataset: { action: 'refresh-status' } } : null } });
    await flush();
  };
  await flush();

  // Overview: exactly four tiles, each backed by an explicit source value.
  const tileLabels = () => elements.get('statusCards').children.map(card => card.children[0].children[0].textContent);
  const tileValue = index => elements.get('statusCards').children[index]?.children[1].textContent;
  const tileCaption = index => elements.get('statusCards').children[index]?.children[2].textContent;
  assert.deepEqual(tileLabels(), ['Videos in library', 'Last sync', 'Active tasks', 'Problems'],
    'Overview must show exactly the four Stage 2 summary tiles');
  assert.equal(tileValue(0), '1234', 'the catalog tile reports the library video count');
  assert.equal(tileValue(1), expectedSyncTime,
    'the sync tile reports the recorded run time, not the time the status was read');
  assert.match(tileCaption(1), /The recorded run completed\. Started .*\./,
    'the sync tile pairs the run time with its outcome and start');
  assert.equal(tileValue(2), '4', 'the task tile reports queued or processing work');
  assert.equal(tileValue(3), '9', 'the problem tile reports unresolved problems');
  assert.match(tileCaption(3), /Unresolved problems/, 'the problem tile names what it counts');

  // Grouped problems: five groups maximum, with occurrences, last detection, and a working link.
  const groupRows = elements.get('problemGroups').children;
  assert.equal(groupRows.length, problemSummary.groups.length + 1,
    'Overview shows the returned groups plus the overflow note');
  const firstGroup = groupRows[0];
  assert.equal(textOf(firstGroup.children[1].children[0]), 'Task failed — sync',
    'a group is named by its problem and operation, not by an arbitrary object');
  assert.equal(textOf(firstGroup.children[1].children[1]), '12 occurrences · 4 items',
    'a group sums recorded occurrences and mentions the number of affected items');
  assert.match(textOf(firstGroup.children[1].children[2]), /^Last detected /, 'a group shows its last occurrence');
  assert.equal(textOf(firstGroup.children[2]), 'high', 'a group carries its highest severity');
  assert.equal(firstGroup.children[3].tagName, 'A');
  assert.equal(firstGroup.children[3].href, '/admin/v2/?job_state=failed&job_action=sync#jobs',
    'a multi-object group links to the filtered list rather than one arbitrary job');
  assert.equal(firstGroup.children[3].getAttribute('aria-label'), 'Review failures: Task failed — sync');
  assert.equal(groupRows.at(-1).children.length, 0, 'the overflow note is plain text');
  assert.match(textOf(groupRows.at(-1)), /^2 more problem groups in All problems\.$/,
    'Overview discloses that more groups exist than it shows');
  assert.equal(elements.get('problemCompleteness').hidden, true, 'a complete summary shows no partial-data warning');

  // The full list keeps its own links, count, and pagination inside "All problems".
  const rows = elements.get('attentionList').children;
  assert.equal(rows.length, FIRST_PAGE_SIZE, 'the issue list renders one page at a time');
  assert.equal(elements.get('issueCount').textContent, `Showing ${FIRST_PAGE_SIZE} of ${issues.length} issues.`,
    'Home announces the loaded issue count against the server total');
  assert.equal(elements.get('issuesLoadMore').hidden, false, 'a truncated issue list offers more results');
  elements.get('issuesLoadMore').listeners.click();
  await flush();
  assert(calls.includes('/admin/api/v2/issues?limit=50&offset=2'), 'loading more issues requests the next offset');
  const allRows = elements.get('attentionList').children;
  assert.equal(allRows.length, issues.length, 'Load more appends the remaining issues');
  const links = allRows.flatMap(row => row.children).filter(child => child.tagName === 'A');
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
  ], 'unsafe external actions must not become links');
  assert.equal(elements.get('issueCount').textContent, `Showing ${issues.length} issues.`,
    'appended pages update the announced count');
  assert.equal(elements.get('issuesLoadMore').hidden, true, 'the last issue page hides Load more');
  const groupSnapshot = textOf(elements.get('problemGroups'));
  assert.equal(tileValue(3), '9', 'pagination must not change the unfiltered problem total');
  assert.equal(textOf(elements.get('problemGroups')), groupSnapshot,
    'pagination must not change the grouped problem summary');

  // Filtering the list must not move the summary tiles or groups.
  elements.get('issueSeverity').value = 'high';
  elements.get('issueSeverity').listeners.change();
  await flush();
  assert.equal(new URLSearchParams(location.search).get('issue_severity'), 'high', 'Home saves issue severity in the URL');
  assert.equal(new URLSearchParams(location.search).get('campaign'), 'keep', 'Home issue filters preserve unrelated query parameters');
  assert.equal(elements.get('attentionList').children.length, FIRST_PAGE_SIZE, 'Home applies the selected issue severity');
  assert.equal(elements.get('issueCount').textContent, 'Showing 2 of 4 issues.', 'Issue count follows the severity filter');
  assert(calls.includes('/admin/api/v2/issues?limit=50&offset=0&severity=high'), 'Home sends severity to the issues API');
  assert.equal(textOf(elements.get('problemGroups')), groupSnapshot,
    'the problem summary is independent of the list filters');
  assert.equal(tileValue(3), '9', 'filtering the issue list must not change the Problems tile');
  elements.get('issueKind').value = 'job_stalled';
  elements.get('issueKind').listeners.change();
  await flush();
  assert.equal(new URLSearchParams(location.search).get('issue_kind'), 'job_stalled', 'Home saves issue type in the URL');
  assert.equal(new URLSearchParams(location.search).get('campaign'), 'keep', 'Home combined filters preserve unrelated query parameters');
  assert.equal(elements.get('attentionList').children.length, 1, 'Home applies combined severity and type filters');
  assert.equal(elements.get('issueCount').textContent, 'Showing 1 issue.', 'Combined filters update the announced count');
  assert(calls.includes('/admin/api/v2/issues?limit=50&offset=0&severity=high&kind=job_stalled'), 'Home sends both issue filters to the API');
  assert.equal(tileValue(3), '9', 'combined filters must not change the Problems tile');
  history.back();
  await flush();
  assert.equal(elements.get('issueKind').value, '', 'Back restores the previous Home issue filters');
  assert.equal(elements.get('attentionList').children.length, FIRST_PAGE_SIZE, 'Back reloads the previous severity-filtered issues');
  history.forward();
  await flush();
  assert.equal(elements.get('issueKind').value, 'job_stalled', 'Forward restores the Home issue type');
  assert.equal(elements.get('attentionList').children.length, 1, 'Forward reloads the combined issue filters');

  issueFeedIncomplete = true;
  await refresh();
  assert.equal(elements.get('issueCount').textContent, 'Showing 1 returned issue; the total may be incomplete.',
    'Home qualifies the count when some issue sources are unavailable');
  assert.equal(elements.get('issueCompleteness').hidden, false, 'Home exposes the incomplete-feed explanation');
  assert.equal(elements.get('problemCompleteness').hidden, false,
    'an incomplete summary is disclosed above the grouped problems');
  assert.match(textOf(elements.get('problemCompleteness')), /may be incomplete/i,
    'the partial-data notice explains that problem groups may be missing');
  issueFeedIncomplete = false;
  await refresh();

  const previouslyLoadedIssueCount = elements.get('attentionList').children.length;
  failIssueFeed = true;
  await refresh();
  assert.equal(elements.get('issueCount').textContent, 'Issue count unavailable; showing previously loaded results.',
    'A failed refresh marks the issue count unavailable without presenting stale counts as current');
  assert.equal(elements.get('attentionList').children.length, previouslyLoadedIssueCount,
    'A failed refresh preserves the previously loaded issue rows');
  assert.match(textOf(elements.get('problemGroups')), /Problem summaries unavailable\./,
    'a failed issue read removes the unverified problem groups');
  assert.equal(tileValue(3), 'No data', 'the Problems tile must not present an unverified count as current');
  failIssueFeed = false;

  malformedIssueFeed = true;
  await refresh();
  assert.equal(elements.get('issueCount').textContent, 'Issue count unavailable; showing previously loaded results.',
    'A null issue total cannot be coerced into a false zero count');
  assert.equal(elements.get('attentionList').children.length, previouslyLoadedIssueCount,
    'A malformed issue response preserves the last valid rows');
  malformedIssueFeed = false;
  await refresh();
  assert.equal(tileValue(3), '9', 'a recovered issue feed restores the unfiltered problem count');

  // Empty and unreadable sources: the tiles must say which of the two it is.
  const problemSnapshot = JSON.parse(JSON.stringify(problemSummary));
  const issueSnapshot = issues.slice();
  problemSummary.total_open = 0; problemSummary.groups_total = 0; problemSummary.groups = [];
  issues.length = 0;
  await refresh();
  assert.equal(tileValue(3), '0', 'a catalog without problems reports zero, not missing data');
  assert.match(textOf(elements.get('problemGroups')), /No problems need attention\./,
    'an empty problem set is reported as empty, not as an error');
  assert.equal(tileCaption(3), 'Unresolved problems', 'the complete zero keeps its plain caption');
  Object.assign(problemSummary, problemSnapshot);
  issues.push(...issueSnapshot);

  const syncSnapshot = { ...syncFixture };
  syncFixture.running = true;
  await refresh();
  assert.equal(tileValue(1), 'In progress', 'a running synchronization is named as such');
  assert.match(tileCaption(1), /executing now/, 'the sync tile does not present a past run while one is running');
  syncFixture.running = false; syncFixture.last_run = null;
  await refresh();
  assert.equal(tileValue(1), 'No history', 'a panel without any recorded run says so plainly');
  assert.match(tileCaption(1), /No completed synchronization run is recorded yet\./);
  syncFixture.history_available = false;
  await refresh();
  assert.equal(tileValue(1), 'No data', 'an unreadable history must not read as "no history"');
  assert.match(tileCaption(1), /history could not be read/);
  syncFixture.history_available = true; syncFixture.available = false;
  await refresh();
  assert.equal(tileValue(1), 'No data', 'an unavailable sync status has no invented time');
  assert.match(tileCaption(1), /status could not be read/);
  Object.assign(syncFixture, syncSnapshot);
  syncFixture.last_run = {
    started_at: '2026-10-14T09:40:00+00:00', ended_at: '2026-10-14T10:00:00+00:00',
    success: false, interrupted: true,
  };
  await refresh();
  assert.match(tileCaption(1), /The recorded run was interrupted\. Started /,
    'an interrupted run is reported with its outcome and start time');
  syncFixture.last_run = { started_at: '2026-10-14T09:40:00+00:00', ended_at: '2026-10-14T10:00:00+00:00' };
  await refresh();
  assert.match(tileCaption(1), /The recorded outcome is unknown\. Started /,
    'a run without a recorded outcome is never presented as a success');
  Object.assign(syncFixture, syncSnapshot);
  await refresh();
  assert.equal(tileValue(1), expectedSyncTime, 'a restored status read returns the recorded run time');

  // The Overview must not pay for statistics that only System shows.
  assert.equal(calls.some(url => url.includes('/quality/summary')), false,
    'Overview must not load the quality summary for its tiles');
  assert.equal(calls.some(url => url.includes('/status/jellyfin')), false,
    'Overview must not load the Jellyfin probe for its tiles');

  // Header: one aggregate service control that never looks healthy when unverified.
  assert.equal(serviceChip.dataset.state, 'attention', 'a read-only panel reads as needing attention');
  assert.equal(chipLabelText, 'Attention needed');
  assert.match(serviceChip.title, /Writes are disabled by the current admin configuration\./,
    'the aggregate control names the reason it needs attention');
  assert.match(serviceChip.getAttribute('aria-label'), /Attention needed\. Open System\./,
    'the aggregate control announces where the detail lives');
  assert.equal(elements.get('writeAccessNotice').hidden, false, 'System reports the write restriction');
  assert.match(textOf(elements.get('writeAccessNotice')), /Read-only: Writes are disabled/,
    'the write-access notice explains the read-only cause');

  const chipWritesBefore = chipLabelWrites;
  failCapabilities = true;
  await refresh();
  assert.equal(elements.get('statusContent').hidden, false, 'A capabilities failure must not hide a successfully loaded system status');
  assert.match(elements.get('statusState').children[0].textContent, /write access could not be verified/i,
    'Home must disclose when capabilities are unavailable while status remains current');
  assert.equal(serviceChip.dataset.state, 'partial', 'unknown permissions must not read as healthy');
  assert.equal(chipLabelText, 'Partial status', 'the aggregate control reports partially verified status');
  assert.equal(chipLabelWrites, chipWritesBefore + 1, 'only the changed service state is re-announced');
  assert.match(textOf(elements.get('writeAccessNotice')), /Access unavailable/i,
    'an unverified permission check is reported instead of assumed read-only');
  failCapabilities = false;
  writableCapabilities = true;
  await refresh();
  assert.equal(serviceChip.dataset.state, 'ok', 'verified writable access with current data reads as healthy');
  assert.equal(chipLabelText, 'All services available · 4 active tasks', 'the healthy state summarizes active work');
  assert.equal(elements.get('writeAccessNotice').hidden, true, 'enabled writes need no warning');
  const chipWritesBeforeRestore = chipLabelWrites;
  writableCapabilities = false;
  await refresh();
  assert.equal(serviceChip.dataset.state, 'attention', 'a restored read-only state is reported again');
  assert.equal(chipLabelWrites, chipWritesBeforeRestore + 1, 'an unchanged service state is not re-announced on every refresh');

  failStatus = true;
  await refresh();
  assert.equal(elements.get('statusContent').hidden, false, 'A later status failure must preserve the previously loaded status');
  assert.match(elements.get('statusState').children[0].textContent, /out of date/i, 'A failed status refresh must visibly mark retained data as stale');
  assert.equal(serviceChip.dataset.state, 'error', 'A failed status refresh marks the connection unavailable');
  assert.equal(chipLabelText, 'Connection unavailable');
  failStatus = false;
  await refresh();

  const routeTitles = {
    start: 'Overview', library: 'Library', sources: 'Sources', performers: 'Performers', jobs: 'Tasks', schedules: 'Schedules', system: 'System',
  };
  const navOwners = {
    start: 'start', library: 'library', performers: 'performers',
    sources: 'sources', jobs: 'jobs', schedules: 'schedules', system: 'system',
  };
  const onHashChange = windowListeners.get('hashchange');
  assert.equal(typeof onHashChange, 'function', 'The router must listen for direct hash navigation');
  for (const route of expectedRoutes) {
    location.hash = `#${route}`;
    onHashChange();

    const visibleViews = views.filter(view => !view.hidden).map(view => view.dataset.view);
    const expectedView = ['start', 'library', 'organize', 'performers'].includes(route) ? route : 'operational';
    assert.deepEqual(visibleViews, [expectedView], `${route} must select exactly its intended view`);
    assert.deepEqual(navLinks.filter(link => link.getAttribute('aria-current') === 'page').map(link => link.dataset.route), [navOwners[route]],
      `${route} must activate the Stage 2 section that owns it`);
    assert.equal(elements.get('crumbCurrent').textContent, routeTitles[route], `${route} must update the breadcrumb`);
    if (['sources', 'jobs', 'schedules', 'system'].includes(route)) {
      assert.equal(elements.get('operationalTitle').textContent, routeTitles[route],
        `${route} must name the shared operational view after navigation`);
    }
  }
  location.hash = '#jobs?tab=schedules';
  onHashChange();
  assert.equal(location.hash, '#schedules', 'the historical Jobs/Schedules address moves to Schedules');
  assert.deepEqual(navLinks.filter(link => link.getAttribute('aria-current') === 'page').map(link => link.dataset.route), ['schedules'],
    'the migrated Schedules address activates the Schedules menu entry');
  location.hash = '#library';
  onHashChange();
  location.hash = '#system';
  onHashChange();
  assert.equal(elements.get('systemStatusView').hidden, false, 'System opens Status by default');
  assert.equal(elements.get('systemBackupView').hidden, true);
  assert.equal(elements.get('systemMaintenanceView').hidden, true);

  // Stage 3 keeps every old catalog address working from the new Library/Performers hosts.
  location.hash = '#quality?tab=duplicates';
  onHashChange();
  assert.equal(location.hash, '#library?tab=duplicates',
    'the legacy Catalog Health duplicate address redirects to Library → Duplicates');
  assert.deepEqual(views.filter(view => !view.hidden).map(view => view.dataset.view), ['library'],
    'Library hosts the duplicate screens in its own view');
  assert.equal(elements.get('libraryDuplicatesPane').hidden, false);
  assert.equal(elements.get('libraryVideosPane').hidden, true);
  assert.equal(elements.get('duplicateCatalogGroup').hidden, false);
  assert.equal(elements.get('duplicateVisualGroup').hidden, true,
    'visual comparison stays collapsed until the operator switches to it');
  assert.equal(elements.get('libraryDuplicatesTab').getAttribute('aria-selected'), 'true',
    'the redirected Library tab is marked selected');
  assert.deepEqual(navLinks.filter(link => link.getAttribute('aria-current') === 'page').map(link => link.dataset.route), ['library'],
    'the duplicate redirect keeps the Library menu entry active');
  assert.equal(lastAdminNavigation.detail.libraryTab, 'duplicates',
    'route events carry the Library tab to the controllers that own it');

  location.hash = '#quality?tab=visual';
  onHashChange();
  assert.equal(location.hash, '#library?tab=duplicates&match=visual',
    'the legacy visual address redirects to the Duplicates tab with the visual switch on');
  assert.equal(elements.get('duplicateVisualGroup').hidden, false, 'the visual group opens for the visual address');
  assert.equal(elements.get('duplicateCatalogGroup').hidden, true);
  assert.equal(elements.get('duplicateMatchVisual').getAttribute('aria-pressed'), 'true',
    'the visual switch reports its pressed state');
  assert.equal(lastAdminNavigation.detail.duplicateMatch, 'visual',
    'route events carry the duplicate match mode to the duplicate controllers');

  location.hash = '#quality?tab=quarantine';
  onHashChange();
  assert.equal(location.hash, '#library?tab=quarantine',
    'the legacy Catalog Health quarantine address redirects to Library → Quarantine');
  assert.equal(elements.get('libraryQuarantinePane').hidden, false);
  assert.equal(elements.get('libraryDuplicatesPane').hidden, true);

  location.hash = '#quality?tab=enrichment';
  onHashChange();
  assert.equal(location.hash, '#library?tab=suggestions',
    'the legacy Enrichment address redirects to the new Library Metadata suggestions tab');
  assert.equal(elements.get('librarySuggestionsPane').hidden, false);
  location.hash = '#quality?tab=enrichment-progress';
  onHashChange();
  assert.equal(location.hash, '#system', 'retired enrichment progress opens System Status');
  assert.equal(elements.get('systemStatusView').hidden, false);
  assert.equal(elements.get('legacyScreenNotice').hidden, false);

  location.hash = '#performers?tab=directory';
  onHashChange();
  assert.deepEqual(views.filter(view => !view.hidden).map(view => view.dataset.view), ['performers'],
    'the Performer Directory has its own view');
  assert.equal(elements.get('performerDirectoryPane').hidden, false);
  assert.equal(elements.get('performerReviewPane').hidden, true);
  assert.equal(elements.get('performerDirectoryTab').getAttribute('aria-selected'), 'true');
  assert.deepEqual(navLinks.filter(link => link.getAttribute('aria-current') === 'page').map(link => link.dataset.route), ['performers'],
    'the Performer Directory deep link activates Performers in the sidebar');
  location.hash = '#performers?tab=proposals';
  onHashChange();
  assert.equal(location.hash, '#performers?tab=review&area=proposals',
    'the legacy proposals address redirects to Review matches with that area selected');
  assert.equal(elements.get('performerReviewPane').hidden, false);
  assert.equal(elements.get('performerDirectoryPane').hidden, true);
  assert.equal(document.activeElement, elements.get('performerProposalsTitle'),
    'the proposals address focuses the recognition proposals area');
  assert.equal(lastAdminNavigation.detail.performerArea, 'proposals',
    'route events carry the selected review area to the performer controllers');
  location.hash = '#performers?tab=matching';
  onHashChange();
  assert.equal(location.hash, '#performers?tab=review&area=name-variants',
    'the legacy name-matching address redirects to Review matches with the variants area selected');
  assert.equal(document.activeElement, elements.get('fuzzyTitle'),
    'the name-variants address focuses the variants area, which stays beside the proposals');

  location.hash = '#organize?tab=people';
  onHashChange();
  assert.equal(location.hash, '#performers?tab=directory',
    'the legacy organizer people tab redirects to Performer Directory');
  assert.equal(elements.get('performerDirectoryPane').hidden, false);
  assert.deepEqual(navLinks.filter(link => link.getAttribute('aria-current') === 'page').map(link => link.dataset.route), ['performers']);
  location.hash = '#organize';
  onHashChange();
  assert.equal(location.hash, '#library?tab=duplicates',
    'the legacy #organize address still opens duplicate suggestions');
  assert.equal(elements.get('libraryDuplicatesPane').hidden, false);
  location.hash = '#organize?tab=people&person=Taylor';
  onHashChange();
  const redirectedParams = new URLSearchParams(location.hash.slice(location.hash.indexOf('?') + 1));
  assert.equal(location.hash.split('?', 1)[0], '#performers',
    'the legacy organizer address redirects to the Performers section');
  assert.equal(redirectedParams.get('tab'), 'directory');
  assert.equal(redirectedParams.get('person'), 'Taylor',
    'a redirect keeps the filters and identifiers the target screen supports');
  assert.equal(lastAdminNavigation.detail.performersTab, 'directory');
  assert.equal(elements.get('peopleQuery').value, 'Taylor',
    'the carried person parameter fills the Directory search');
  location.hash = '#unknown-route';
  onHashChange();
  assert.deepEqual(views.filter(view => !view.hidden).map(view => view.dataset.view), ['start'],
    'Unknown hashes must fall back to the Home view');
  assert.deepEqual(navLinks.filter(link => link.getAttribute('aria-current') === 'page').map(link => link.dataset.route), ['start'],
    'Unknown hashes must select Home in the primary navigation');
  assert.equal(elements.get('crumbCurrent').textContent, 'Overview', 'Unknown hashes must reset the breadcrumb to Overview');

  resetHistory('http://localhost/admin/v2/?q=alpha&campaign=keep#library');
  onHashChange();
  await flush();
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
  await flush();
  assert(calls.some(url => url.includes('/library/videos?q=gamma&sort=first_seen&limit=50&offset=0')),
    'the settled search requests the URL state shown to the operator');
  const gammaSelection = elements.get('videoGrid').children[0].children[0].children[0];
  gammaSelection.checked = true;
  gammaSelection.listeners.change();
  assert.equal(elements.get('bulkSelectionCount').textContent, '1 selected');
  history.back();
  await flush();
  assert.equal(libraryFields.get('q').value, 'alpha', 'Back restores the prior Library filter');
  assert.equal(elements.get('bulkSelectionCount').textContent, '0 selected', 'Back to different filters clears the hidden selection');
  assert.equal(elements.get('reviewQuarantine').disabled, true, 'bulk actions stay disabled after history changes filters');
  assert.equal(new URLSearchParams(location.search).get('campaign'), 'keep', 'Back preserves unrelated query parameters');
  assert(calls.some(url => url.includes('/library/videos?q=alpha&sort=first_seen&limit=50&offset=0')),
    'restored filter state triggers a matching catalog request');
  elements.get('pageNext').listeners.click();
  await flush();
  assert.equal(new URLSearchParams(location.search).get('offset'), '50', 'page navigation is linkable');
  const pageTwoSelection = elements.get('videoGrid').children[0].children[0].children[0];
  pageTwoSelection.checked = true;
  pageTwoSelection.listeners.change();
  assert.equal(elements.get('bulkSelectionCount').textContent, '1 selected');
  history.back();
  await flush();
  assert.equal(new URLSearchParams(location.search).get('offset'), null, 'Back restores the previous page');
  assert.equal(elements.get('pageInfo').textContent, 'Page 1 of 2');
  assert.equal(elements.get('bulkSelectionCount').textContent, '1 selected', 'page navigation preserves selections across pages');
  history.forward();
  await flush();
  assert.equal(elements.get('pageInfo').textContent, 'Page 2 of 2', 'Forward restores the next page');
  assert.equal(elements.get('bulkSelectionCount').textContent, '1 selected', 'Forward keeps the multi-page selection');

  resetHistory('http://localhost/admin/v2/?q=alpha&campaign=keep#library');
  onHashChange();
  await flush();
  const callsBeforePendingSearch = calls.length;
  libraryFields.get('q').value = 'beta';
  libraryForm.listeners.input({ target: libraryFields.get('q') });
  libraryFields.get('q').value = 'gamma';
  libraryForm.listeners.input({ target: libraryFields.get('q') });
  const timersBeforeBack = timers.size;
  history.back();
  assert.ok(timers.size < timersBeforeBack, 'Back cancels the pending Library search debounce');
  await flush();
  assert.equal(libraryFields.get('q').value, 'alpha', 'Back during typing restores the prior search');
  assert.equal(calls.slice(callsBeforePendingSearch).some(url => url.includes('/library/videos?q=gamma')),
    false, 'the cancelled search does not issue a stale request after Back');
  history.forward();
  await flush();
  assert.equal(libraryFields.get('q').value, 'gamma', 'Forward restores the grouped search entry');

  savedViewsFixture = JSON.stringify([{ name: 'Unavailable catalog', filters: { q: 'saved title', availability: 'unavailable', sort: 'title' } }]);
  const historyBeforeSavedView = historyUrls.length;
  elements.get('savedViews').listeners.change({ target: { value: '0' } });
  await flush();
  assert.equal(historyUrls.length, historyBeforeSavedView + 1, 'applying a saved view creates one history entry');
  assert.equal(new URLSearchParams(location.search).get('q'), 'saved title');
  assert.equal(new URLSearchParams(location.search).get('availability'), 'unavailable');
  assert.equal(new URLSearchParams(location.search).get('sort'), 'title');
  assert.equal(new URLSearchParams(location.search).get('campaign'), 'keep');
  assert(calls.some(url => url.includes('/library/videos?q=saved+title&availability=unavailable&sort=title&limit=50&offset=0')),
    'applying a saved view fetches its exact URL-backed filters');
  history.back();
  await flush();
  assert.equal(libraryFields.get('q').value, 'gamma', 'Back restores the filters before a saved view');
  history.forward();
  await flush();
  assert.equal(libraryFields.get('availability').value, 'unavailable', 'Forward restores the saved view filters');
  const historyBeforeClearFilters = historyUrls.length;
  elements.get('clearFilters').listeners.click();
  await flush();
  assert.equal(historyUrls.length, historyBeforeClearFilters + 1, 'clearing filters creates one history entry');
  assert.equal(libraryFields.get('q').value, '');
  assert.equal(libraryFields.get('availability').value, '');
  assert.equal(new URLSearchParams(location.search).get('q'), null);
  history.back();
  await flush();
  assert.equal(libraryFields.get('q').value, 'saved title', 'Back restores filters before clearing');
  assert.equal(libraryFields.get('availability').value, 'unavailable');
  history.forward();
  await flush();
  assert.equal(libraryFields.get('q').value, '', 'Forward restores the cleared filters');

  const onKeydown = documentListeners.get('keydown');
  assert.equal(typeof onKeydown, 'function', 'Admin v2 must register its keyboard focus handler');
  const tab = (shiftKey = false) => {
    const event = { key: 'Tab', shiftKey, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
    onKeydown(event);
    return event;
  };
  document.activeElement = new Element('body');
  assert.equal(tab().defaultPrevented, false, 'Tab outside a modal must keep native navigation');
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

  // Stage 5 routes retired reports to Status while preserving the three System panes.
  resetHistory('http://localhost/admin/v2/#system');
  onHashChange();
  await flush(6);
  assert.equal(elements.get('systemStatusView').hidden, false, 'System defaults to Status');
  assert.equal(elements.get('systemBackupView').hidden, true);
  assert.equal(elements.get('systemMaintenanceView').hidden, true);
  assert.equal(calls.some(url => url.includes('/quality/summary') || url.includes('/status/jellyfin')
    || url.includes('/diagnostics/playback') || url.includes('/activity')),
    false, 'System status avoids the retired catalog and admin history reports');
  assert.match(textOf(elements.get('writeAccessNotice')), /Read-only: Writes are disabled/,
    'System status includes the verified write restriction');

  const tabs = systemTabLinks.map(link => link.dataset.systemTab);
  assert.deepEqual(tabs, ['status', 'backups', 'maintenance']);
  const backupUrl = new URL(location.href); backupUrl.hash = '#system?tab=backups';
  history.pushState({}, '', backupUrl); windowListeners.get('popstate')?.();
  assert.equal(elements.get('systemBackupView').hidden, false, 'the Backups pane has a stable route');
  assert.equal(elements.get('systemStatusView').hidden, true);
  const maintenanceUrl = new URL(location.href); maintenanceUrl.hash = '#system?tab=maintenance';
  history.pushState({}, '', maintenanceUrl); windowListeners.get('popstate')?.();
  assert.equal(elements.get('systemMaintenanceView').hidden, false, 'the Maintenance pane has a stable route');
  assert.equal(elements.get('systemBackupView').hidden, true);
  const legacyLinkcheck = new URL(location.href); legacyLinkcheck.hash = '#system?tab=linkcheck';
  history.pushState({}, '', legacyLinkcheck); windowListeners.get('popstate')?.();
  assert.equal(location.hash, '#system', 'the old Link-check address redirects to System Status');
  assert.equal(elements.get('systemStatusView').hidden, false);
  assert.equal(elements.get('legacyScreenNotice').hidden, false, 'a retired report displays its one-time notice');
  assert.match(routerSource, /system.*linkcheck[\s\S]*?return retired\(\)/,
    'legacy report routes are rewritten without activating their former controllers');
  assert.match(fs.readFileSync('resolver/static/admin-v2/sources.js', 'utf8'),
    /route === 'system' && \['sync', 'sync-activity', 'sync_activity', 'history'\]\.includes\(tab\)/,
    'the old global sync-history hashes still render the Sources history view');
  const oldAutomation = new URL(location.href); oldAutomation.hash = '#system?tab=automation';
  history.pushState({}, '', oldAutomation); windowListeners.get('popstate')?.();
  assert.equal(location.hash, '#schedules', 'the old System Automation tab redirects to Schedules');
  assert.deepEqual(navLinks.filter(link => link.getAttribute('aria-current') === 'page').map(link => link.dataset.route), ['schedules']);
  assert.equal(elements.get('legacyScreenNotice').hidden, false);
  const oldAdminHistory = new URL(location.href); oldAdminHistory.hash = '#system?tab=activity';
  history.pushState({}, '', oldAdminHistory); windowListeners.get('popstate')?.();
  assert.equal(location.hash, '#system', 'the old administrative history link opens System Status');
  assert.equal(elements.get('systemStatusView').hidden, false);
  process.stdout.write('PASS: seven-section navigation, Overview tiles, grouped problems, System service state, legacy deep links, Library history, and modal focus boundaries\n');
})().catch(error => { console.error(error); process.exitCode = 1; });
