import http from 'node:http';
import fs from 'node:fs';
import { spawn } from 'node:child_process';

const port = Number(process.env.ADMIN_SMOKE_PORT || 4173);
const upstreamUrl = new URL(process.env.ADMIN_SMOKE_UPSTREAM || 'http://adult-resolver:8090');
const outputDir = process.env.ADMIN_SMOKE_OUTPUT || '/output';
fs.mkdirSync(outputDir, { recursive: true });
// "active" is the primary-navigation entry the panel must mark as current.
const routes = [
  { hash: 'start', view: 'start', active: 'start' },
  { hash: 'library', view: 'library', active: 'library' },
  { hash: 'library?tab=duplicates', view: 'library', active: 'library' },
  { hash: 'library?tab=duplicates&match=visual', view: 'library', active: 'library' },
  { hash: 'library?tab=suggestions', view: 'library', active: 'library' },
  { hash: 'library?tab=quarantine', view: 'library', active: 'library' },
  { hash: 'performers?tab=review', view: 'performers', active: 'performers' },
  { hash: 'performers', view: 'performers', active: 'performers' },
  { hash: 'performers?tab=directory', view: 'performers', active: 'performers' },
  { hash: 'quality?tab=issues', view: 'operational', active: 'system' },
  { hash: 'quality?tab=duplicates', view: 'library', active: 'library' },
  { hash: 'quality?tab=quarantine', view: 'library', active: 'library' },
  { hash: 'quality?tab=enrichment', view: 'library', active: 'library' },
  { hash: 'quality?tab=enrichment-progress', view: 'operational', active: 'system' },
  { hash: 'system', view: 'operational', active: 'system' },
  { hash: 'system?tab=backups', view: 'operational', active: 'system' },
  { hash: 'system?tab=maintenance', view: 'operational', active: 'system' },
  { hash: 'system?tab=automation', view: 'operational', active: 'schedules' },
  { hash: 'system?tab=activity', view: 'operational', active: 'system' },
  { hash: 'jobs?tab=schedules', view: 'operational', active: 'schedules' },
  { hash: 'organize', view: 'library', active: 'library' },
  { hash: 'sources?tab=history', view: 'operational', active: 'sources' },
  { hash: 'jobs?tab=history', view: 'operational', active: 'jobs' },
  ...(['stage4', 'stage5', 'stage6'].includes(process.env.ADMIN_SMOKE_FIXTURES) ? [
  { hash: 'jobs', query: 'job_id=qa-queued', name: 'task-queued', view: 'operational', active: 'jobs' },
  { hash: 'jobs', query: 'job_id=qa-processing', name: 'task-running', view: 'operational', active: 'jobs' },
  { hash: 'jobs?tab=history', query: 'job_id=qa-done', name: 'task-succeeded', view: 'operational', active: 'jobs' },
  { hash: 'jobs?tab=history', query: 'job_id=qa-failed', name: 'task-failed', view: 'operational', active: 'jobs' },
  { hash: 'jobs', query: 'job_id=qa-visual&job_queue=visual', name: 'task-visual', view: 'operational', active: 'jobs' },
  ] : []),
  { hash: 'sources', view: 'operational', active: 'sources' },
  { hash: 'jobs', view: 'operational', active: 'jobs' },
  { hash: 'schedules', view: 'operational', active: 'schedules' },
];
const overviewTiles = ['Videos in library', 'Active tasks', 'Problems', 'Artwork coverage', 'OSHASH coverage', 'pHash coverage'];
const widths = [390, 768, 1440];
const themes = ['light', 'dark'];
const statuses = { total: 0, ok: 0, redirects: 0, clientError: 0, serverError: 0, images: 0 };
const methods = new Set();
const apiPaths = new Set();
const allowQueueSync = process.env.ADMIN_SMOKE_ALLOW_QUEUE_SYNC === '1';
if (allowQueueSync && (!['stage4', 'stage5', 'stage6'].includes(process.env.ADMIN_SMOKE_FIXTURES) || upstreamUrl.hostname !== '127.0.0.1' || upstreamUrl.port !== '18093')) throw new Error('Sync admission smoke requires the isolated fixture server.');
let queuedSyncRequests = 0;
const pixel = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/XioAAAAASUVORK5CYII=', 'base64');
function send(res, code, body, type = 'text/plain; charset=utf-8') {
  res.writeHead(code, { 'content-type': type, 'cache-control': 'no-store' });
  res.end(body);
}

const server = http.createServer((req, res) => {
  methods.add(req.method || 'GET');
  if (req.method !== 'GET') {
    if (!allowQueueSync || req.method !== 'POST' || req.url !== '/admin/api/v2/maintenance/sync') return send(res, 405, 'Writes blocked by live visual smoke.');
    queuedSyncRequests += 1;
  }
  if (/^image\//.test(req.headers.accept || '')) {
    statuses.images += 1;
    return send(res, 200, pixel, 'image/png');
  }
  const upstream = http.request({
    host: upstreamUrl.hostname, port: upstreamUrl.port || 80, path: req.url, method: req.method,
    headers: { host: `127.0.0.1:${port}`, 'x-forwarded-host': `127.0.0.1:${port}`, accept: req.headers.accept || '*/*', ...(req.headers.origin ? { origin: req.headers.origin } : {}), ...(req.headers['content-type'] ? { 'content-type': req.headers['content-type'] } : {}) },
  }, up => {
    if ((req.url || '').startsWith('/admin/api/')) {
      apiPaths.add((req.url || '').split('?')[0]);
      statuses.total += 1;
      if (up.statusCode >= 200 && up.statusCode < 300) statuses.ok += 1;
      else if (up.statusCode >= 300 && up.statusCode < 400) statuses.redirects += 1;
      else if (up.statusCode >= 400 && up.statusCode < 500) statuses.clientError += 1;
      else if (up.statusCode >= 500) statuses.serverError += 1;
    }
    res.writeHead(up.statusCode || 502, up.headers);
    up.pipe(res);
  });
  upstream.on('error', () => send(res, 502, 'Resolver unavailable.'));
  req.pipe(upstream);
});
await new Promise(resolve => server.listen(port, '127.0.0.1', resolve));

const browserProfile = fs.mkdtempSync('/tmp/adulthub-visual-');
const debugPort = Number(process.env.ADMIN_SMOKE_CDP_PORT || 19223);
const chrome = spawn(process.env.CHROMIUM_PATH || '/ms-playwright/chromium-1228/chrome-linux64/chrome', [
  '--headless=new', '--no-proxy-server', '--no-sandbox', '--disable-dev-shm-usage', '--disable-background-networking',
  '--disable-extensions', '--no-first-run', '--no-default-browser-check', `--remote-debugging-port=${debugPort}`,
  '--remote-allow-origins=*', `--user-data-dir=${browserProfile}`, 'about:blank',
], { stdio: 'ignore' });
process.on('exit', () => chrome.kill('SIGTERM'));
chrome.on('error', error => { console.error(error.message); process.exit(1); });
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
async function cdpSocket() {
  for (let i = 0; i < 80; i += 1) {
    try {
      const response = await fetch(`http://127.0.0.1:${debugPort}/json/version`);
      if (response.ok) return (await response.json()).webSocketDebuggerUrl;
    } catch {}
    await sleep(100);
  }
  throw new Error('Chrome DevTools endpoint did not start');
}
const ws = new WebSocket(await cdpSocket());
await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
let nextId = 0;
const pending = new Map();
const handlers = new Map();
ws.onmessage = event => {
  const message = JSON.parse(event.data);
  if (message.id) {
    const slot = pending.get(message.id);
    if (slot) {
      pending.delete(message.id);
      message.error ? slot.reject(new Error(message.error.message)) : slot.resolve(message.result || {});
    }
    return;
  }
  const callbacks = handlers.get((message.sessionId || '') + ':' + message.method) || [];
  for (const callback of callbacks) callback(message.params || {});
};
function on(method, callback, session = '') {
  const key = session + ':' + method;
  if (!handlers.has(key)) handlers.set(key, []);
  handlers.get(key).push(callback);
}
function cmd(method, params = {}, session = '') {
  const id = ++nextId;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error('CDP timeout: ' + method)); }, 60000);
    pending.set(id, {
      resolve: value => { clearTimeout(timer); resolve(value); },
      reject: error => { clearTimeout(timer); reject(error); },
    });
    ws.send(JSON.stringify({ id, method, params, ...(session ? { sessionId: session } : {}) }));
  });
}
const { targetId } = await cmd('Target.createTarget', { url: 'about:blank' });
const { sessionId } = await cmd('Target.attachToTarget', { targetId, flatten: true });
for (const method of ['Page.enable', 'Runtime.enable', 'Log.enable']) await cmd(method, {}, sessionId);
await cmd('Page.addScriptToEvaluateOnNewDocument', {
  source: "localStorage.setItem('adminV2Theme',new URLSearchParams(location.search).get('qaTheme')||'light');",
}, sessionId);
let jsErrors = 0;
let consoleErrors = 0;
let loadResolve = null;
on('Runtime.exceptionThrown', () => { jsErrors += 1; }, sessionId);
on('Runtime.consoleAPICalled', event => { if (event.params.type === 'error') consoleErrors += 1; }, sessionId);
on('Page.loadEventFired', () => { if (loadResolve) { loadResolve(); loadResolve = null; } }, sessionId);

const results = [];
for (const route of routes) for (const width of widths) for (const theme of themes) {
  const errorsBefore = jsErrors;
  const consoleErrorsBefore = consoleErrors;
  const height = width === 390 ? 844 : width === 768 ? 1024 : 1000;
  await cmd('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile: width <= 600 }, sessionId);
  await cmd('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-color-scheme', value: theme }] }, sessionId);
  const loaded = new Promise(resolve => {
    const timer = setTimeout(() => { loadResolve = null; resolve('page load timeout'); }, 45000);
    loadResolve = () => { clearTimeout(timer); resolve(); };
  });
  await cmd('Page.navigate', { url: 'http://127.0.0.1:' + port + '/admin/v2/?qaTheme=' + theme + (route.query ? '&' + route.query : '') + '#' + route.hash }, sessionId);
  let loadError = '';
  loadError = await loaded || '';
  await sleep(800);
  const evaluated = await cmd('Runtime.evaluate', {
    expression: "JSON.stringify({visible:[...document.querySelectorAll('.view')].filter(v=>!v.hidden).map(v=>v.dataset.view),activeRoute:document.querySelector('.primary-nav a[aria-current=page]')?.dataset.route||'',innerWidth,documentWidth:document.documentElement.scrollWidth,openTaskIds:[...document.querySelectorAll('.job-detail-row')].filter(row=>!row.hidden).map(row=>row.dataset.jobId),search:location.search,sourceTab:document.querySelector('[data-sources-tab][aria-current=page]')?.dataset.sourcesTab,serviceState:document.querySelector('#serviceStatus')?.dataset.state||'',overviewTiles:[...document.querySelectorAll('#statusCards .metric-top > span:first-child')].map(node=>node.textContent)})",
    returnByValue: true,
  }, sessionId);
  let view = {};
  try { view = JSON.parse(evaluated.result.value || '{}'); } catch {}
  const screenshot = await cmd('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false }, sessionId);
  const routeName = (route.name || route.hash).replace(/[^A-Za-z0-9_-]+/g, '-').replace(/^-|-$/g, '');
  fs.writeFileSync(`${outputDir}/${routeName}-${width}-${theme}.png`, Buffer.from(screenshot.data, 'base64'));
  const overviewMatches = !route.hash.startsWith('start') ||
    (view.overviewTiles || []).join('|') === overviewTiles.join('|');
  const serviceStateKnown = ['checking', 'ok', 'attention', 'partial', 'unknown', 'error'].includes(view.serviceState);
  const expectedTask = new URLSearchParams(route.query || '').get('job_id');
  const taskMatches = !expectedTask || (view.openTaskIds?.includes(expectedTask) && new URLSearchParams(view.search).get('job_id') === expectedTask);
  const sourceTabMatches = !route.hash.startsWith('sources') || view.sourceTab === (route.hash.includes('tab=history') ? 'history' : 'channels');
  const result = {
    route: route.hash, ...(route.query ? { query: route.query } : {}), width, theme,
    ok: !loadError && view.visible?.length === 1 && view.visible[0] === route.view &&
      view.activeRoute === route.active && view.innerWidth === width && view.documentWidth <= width &&
      jsErrors === errorsBefore && consoleErrors === consoleErrorsBefore && serviceStateKnown && overviewMatches &&
      taskMatches && sourceTabMatches,
    overflow: view.documentWidth > width,
    errors: jsErrors - errorsBefore,
    consoleErrors: consoleErrors - consoleErrorsBefore,
    serviceState: view.serviceState,
    overviewMatches, taskMatches, sourceTabMatches,
  };
  results.push(result);
  console.log((result.ok ? 'PASS ' : 'FAIL ') + route.hash + '-' + width + '-' + theme +
    ' overflow=' + result.overflow + ' jsErrors=' + result.errors + (loadError ? ' load=' + loadError : ''));
}
// Capture the Schedules create editor independently of the route matrix.
await cmd('Emulation.setDeviceMetricsOverride', { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false }, sessionId);
await cmd('Page.navigate', { url: 'http://127.0.0.1:' + port + '/admin/v2/?qaTheme=light#schedules' }, sessionId);
await sleep(800);
const editor = await cmd('Runtime.evaluate', {
  expression: `(() => {
    const panel = document.querySelector('#scheduleCreatePanel');
    if (panel) panel.open = true;
    return JSON.stringify({ visible: !document.querySelector('#systemSchedulesView')?.hidden,
      open: !!panel?.open, fields: [...(panel?.querySelectorAll('label') || [])].map(label => label.textContent.trim()) });
  })()`, returnByValue: true,
}, sessionId);
let scheduleEditor = {};
try { scheduleEditor = JSON.parse(editor.result.value || '{}'); } catch {}
const editorScreenshot = await cmd('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false }, sessionId);
fs.writeFileSync(`${outputDir}/schedules-editor-1440-light.png`, Buffer.from(editorScreenshot.data, 'base64'));
// Opening and closing a film must retain visible filters, page and selection.
await cmd('Emulation.setDeviceMetricsOverride', { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false }, sessionId);
await cmd('Page.navigate', { url: 'http://127.0.0.1:' + port + '/admin/v2/?sort=title&offset=50#library' }, sessionId);
await sleep(1000);
const libraryBefore = await cmd('Runtime.evaluate', {
  expression: `(() => {
    const checkbox = document.querySelector('#videoGrid input[type=checkbox]');
    const opener = document.querySelector('#videoGrid .video-open');
    if (!checkbox || !opener) return null;
    checkbox.checked = true; checkbox.dispatchEvent(new Event('change'));
    const filters = [...document.querySelectorAll('#libraryFilters input, #libraryFilters select')].map(field => [field.id, field.value]);
    const before = { query: location.search, page: document.querySelector('#pageInfo').textContent,
      selection: document.querySelector('#bulkSelectionCount').textContent, filters };
    opener.click(); return before;
  })()`, returnByValue: true,
}, sessionId);
await sleep(400);
const filmOpened = await cmd('Runtime.evaluate', {
  expression: "location.hash.startsWith('#film/') && document.querySelector('#detailOverlay').open && !!document.querySelector('#detailTitle')",
  returnByValue: true,
}, sessionId);
await cmd('Runtime.evaluate', { expression: "document.querySelector('[data-action=close-detail]').click()" }, sessionId);
await sleep(400);
const libraryAfter = await cmd('Runtime.evaluate', {
  expression: "({query:location.search,page:document.querySelector('#pageInfo').textContent,selection:document.querySelector('#bulkSelectionCount').textContent,filters:[...document.querySelectorAll('#libraryFilters input, #libraryFilters select')].map(field=>[field.id,field.value]),advancedVisible:!!document.querySelector('#libraryFilters .library-advanced-filters')&&!document.querySelector('#libraryFilters .library-advanced-filters').hidden,closed:!document.querySelector('#detailOverlay').open,hash:location.hash})",
  returnByValue: true,
}, sessionId);
const before = libraryBefore.result.value;
const after = libraryAfter.result.value;
const libraryHistory = { ok: Boolean(before) && filmOpened.result.value === true && after.closed && after.advancedVisible && after.hash === '#library' && before.query === after.query && before.page === after.page && before.selection === after.selection && JSON.stringify(before.filters) === JSON.stringify(after.filters) };
console.log((libraryHistory.ok ? 'PASS ' : 'FAIL ') + 'Library filters, page and selection survive film details');
// Exercise native modal focus behavior in a real Chromium runtime, not a
// showModal()-only unit stub: Tab/Shift+Tab stay inside the performer detail
// dialog and Escape returns focus to the directory control that opened it.
await cmd('Emulation.setDeviceMetricsOverride', { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false }, sessionId);
await cmd('Page.navigate', { url: 'http://127.0.0.1:' + port + '/admin/v2/?qaTheme=light#performers?tab=directory' }, sessionId);
await sleep(1200);
const openerEval = await cmd('Runtime.evaluate', {
  expression: "(() => { const button = [...document.querySelectorAll('[data-view=\\\"performers\\\"] button')].find(node => node.textContent.trim() === 'Details'); if (!button) return false; window.__modalOpener = button; button.focus(); button.click(); return true; })()",
  returnByValue: true,
}, sessionId);
await sleep(400);
const focusableCountEval = await cmd('Runtime.evaluate', {
  expression: "document.querySelectorAll('#personDetailDialog button:not([disabled]),#personDetailDialog input:not([disabled]),#personDetailDialog select:not([disabled]),#personDetailDialog textarea:not([disabled]),#personDetailDialog a[href],[tabindex]:not([tabindex=\\\"-1\\\"])').length",
  returnByValue: true,
}, sessionId);
const dialogFocusableCount = Number(focusableCountEval.result.value || 0);
const focusInDialog = async () => {
  const evaluated = await cmd('Runtime.evaluate', {
    expression: "!!document.querySelector('#personDetailDialog')?.contains(document.activeElement)", returnByValue: true,
  }, sessionId);
  return evaluated.result.value === true;
};
const tabResults = [];
for (const modifiers of [0, 0, 8, 8]) {
  await cmd('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Tab', code: 'Tab', windowsVirtualKeyCode: 9, modifiers }, sessionId);
  await cmd('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Tab', code: 'Tab', windowsVirtualKeyCode: 9, modifiers }, sessionId);
  tabResults.push(await focusInDialog());
}
await cmd('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 }, sessionId);
await cmd('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 }, sessionId);
const dialogCloseEval = await cmd('Runtime.evaluate', {
  expression: "({closed:!document.querySelector('#personDetailDialog')?.open,restored:document.activeElement===window.__modalOpener})",
  returnByValue: true,
}, sessionId);
const modalKeyboard = {
  opened: openerEval.result.value === true,
  focusableCount: dialogFocusableCount,
  tabStayedInDialog: tabResults.every(Boolean),
  escapeClosed: dialogCloseEval.result.value?.closed === true,
  focusRestored: dialogCloseEval.result.value?.restored === true,
};
modalKeyboard.ok = modalKeyboard.opened && modalKeyboard.focusableCount > 0 && modalKeyboard.tabStayedInDialog &&
  modalKeyboard.escapeClosed && modalKeyboard.focusRestored;
console.log((modalKeyboard.ok ? 'PASS ' : 'FAIL ') + 'Performer detail modal keyboard focus ' + JSON.stringify(modalKeyboard));
let queuedSync = null;
if (allowQueueSync) {
  await cmd('Page.navigate', { url: `http://127.0.0.1:${port}/admin/v2/?qaTheme=light#sources` }, sessionId);
  await sleep(1000);
  await cmd('Runtime.evaluate', { expression: `(() => {
    const button = document.querySelector('[data-sync-source="-1001"][data-sync-mode="scheduled"]');
    if (button) { button.focus(); button.click(); button.click(); }
  })()` }, sessionId);
  await sleep(1000);
  const receipt = await cmd('Runtime.evaluate', {
    expression: `(() => { const notice = document.querySelector('#sourcesNotice'); const link = notice.querySelector('a'); return { text: notice.textContent, href: link?.href || '' }; })()`, returnByValue: true,
  }, sessionId);
  const taskId = receipt.result.value.href ? new URL(receipt.result.value.href).searchParams.get('job_id') : '';
  const sourceScreen = await cmd('Page.captureScreenshot', { format: 'png' }, sessionId);
  fs.writeFileSync(`${outputDir}/sources-queued-1440-light.png`, Buffer.from(sourceScreen.data, 'base64'));
  await cmd('Runtime.evaluate', { expression: "document.querySelector('#sourcesNotice a')?.click()" }, sessionId);
  await sleep(1000);
  const selected = async () => {
    const value = await cmd('Runtime.evaluate', { expression: "({id:new URLSearchParams(location.search).get('job_id'),open:[...document.querySelectorAll('.job-detail-row')].filter(row=>!row.hidden).map(row=>row.dataset.jobId)})", returnByValue: true }, sessionId);
    return value.result.value;
  };
  const first = await selected();
  await cmd('Page.reload', {}, sessionId);
  await sleep(1000);
  const reloaded = await selected();
  queuedSync = { taskId, queued: receipt.result.value.text.includes('Queued'), viewTask: receipt.result.value.text.includes('View task'), requests: queuedSyncRequests,
    opened: !!taskId && first.id === taskId && first.open.includes(taskId),
    reloadPreserved: !!taskId && reloaded.id === taskId && reloaded.open.includes(taskId) };
  queuedSync.ok = queuedSync.queued && queuedSync.viewTask && queuedSync.opened && queuedSync.reloadPreserved && queuedSync.requests === 1;
  const taskScreen = await cmd('Page.captureScreenshot', { format: 'png' }, sessionId);
  fs.writeFileSync(`${outputDir}/queued-task-1440-light.png`, Buffer.from(taskScreen.data, 'base64'));
  console.log((queuedSync.ok ? 'PASS ' : 'FAIL ') + 'Sync queues once, links its task, and retains selection after reload ' + JSON.stringify(queuedSync));
}
const summary = {
  count: results.length, passed: results.filter(result => result.ok).length,
  failed: results.filter(result => !result.ok).length,
  apiRequests: statuses.total, api2xx: statuses.ok, api3xx: statuses.redirects,
  api4xx: statuses.clientError, api5xx: statuses.serverError, imageResponsesReplaced: statuses.images, consoleErrors,
  apiPaths: [...apiPaths].sort(), scheduleEditor,
  removedViewRequests: [...apiPaths].filter(path => /\/(?:audits|activity|linkcheck|playback-diagnostics|quality\/(?!proposals(?:\/|$))|metadata\/enrichment|enrichment\/progress)(?:\/|$)/.test(path)),
  writeMethods: [...methods].filter(method => method !== 'GET'), queuedSync, modalKeyboard, libraryHistory, results,
};
fs.writeFileSync(`${outputDir}/live-results.json`, JSON.stringify(summary, null, 2));
console.log('SUMMARY ' + summary.passed + '/' + summary.count + '; API=' + statuses.total +
  ' (2xx=' + statuses.ok + ', 3xx=' + statuses.redirects + ', 4xx=' + statuses.clientError + ', 5xx=' + statuses.serverError +
  '); image responses replaced=' + statuses.images + '; writes=' + summary.writeMethods.length);
ws.close();
chrome.kill('SIGTERM');
server.close();
if (summary.failed || summary.consoleErrors || !modalKeyboard.ok || !libraryHistory.ok || !scheduleEditor.visible || !scheduleEditor.open || summary.removedViewRequests.length || summary.writeMethods.some(method => method !== 'POST' || !allowQueueSync) || (allowQueueSync && !queuedSync?.ok) || summary.count !== routes.length * widths.length * themes.length) process.exitCode = 1;
