import http from 'node:http';
import fs from 'node:fs';
import { spawn } from 'node:child_process';

const port = 4173;
const routes = [
  { hash: 'start', view: 'start', active: 'start' },
  { hash: 'library', view: 'library', active: 'library' },
  { hash: 'performers', view: 'performers', active: 'performers' },
  { hash: 'performers?tab=directory', view: 'organize', active: 'performers' },
  { hash: 'quality?tab=issues', view: 'operational', active: 'quality' },
  { hash: 'quality?tab=duplicates', view: 'organize', active: 'quality' },
  { hash: 'quality?tab=quarantine', view: 'organize', active: 'quality' },
  { hash: 'quality?tab=enrichment', view: 'operational', active: 'quality', enrichment: true },
  { hash: 'organize', view: 'organize', active: 'quality' },
  { hash: 'sources', view: 'operational', active: 'sources' },
  { hash: 'jobs', view: 'operational', active: 'jobs' },
  { hash: 'system', view: 'operational', active: 'system' },
];
const widths = [390, 768, 1440];
const themes = ['light', 'dark'];
const statuses = { total: 0, ok: 0, redirects: 0, clientError: 0, serverError: 0, images: 0 };
const methods = new Set();
const pixel = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/XioAAAAASUVORK5CYII=', 'base64');
function send(res, code, body, type = 'text/plain; charset=utf-8') {
  res.writeHead(code, { 'content-type': type, 'cache-control': 'no-store' });
  res.end(body);
}

const server = http.createServer((req, res) => {
  methods.add(req.method || 'GET');
  if (req.method !== 'GET') return send(res, 405, 'Writes blocked by live visual smoke.');
  if (/^image\//.test(req.headers.accept || '')) {
    statuses.images += 1;
    return send(res, 200, pixel, 'image/png');
  }
  const upstream = http.request({
    host: 'adult-resolver', port: 8090, path: req.url, method: 'GET',
    headers: { 'x-forwarded-host': '127.0.0.1:4173', accept: req.headers.accept || '*/*' },
  }, up => {
    if ((req.url || '').startsWith('/admin/api/')) {
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

const chrome = spawn('/ms-playwright/chromium-1228/chrome-linux64/chrome', [
  '--headless=new', '--no-sandbox', '--disable-dev-shm-usage', '--disable-background-networking',
  '--disable-extensions', '--no-first-run', '--no-default-browser-check', '--remote-debugging-port=9222',
  '--remote-allow-origins=*', '--user-data-dir=/tmp/chrome-live-smoke-profile', 'about:blank',
], { stdio: 'ignore' });
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
async function cdpSocket() {
  for (let i = 0; i < 80; i += 1) {
    try {
      const response = await fetch('http://127.0.0.1:9222/json/version');
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
    const timer = setTimeout(() => { pending.delete(id); reject(new Error('CDP timeout: ' + method)); }, 10000);
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
await cmd('Fetch.enable', { patterns: [{ urlPattern: 'http://127.0.0.1:4173/admin/api/*', requestStage: 'Request' }] }, sessionId);
let jsErrors = 0;
let loadResolve = null;
on('Runtime.exceptionThrown', () => { jsErrors += 1; }, sessionId);
on('Fetch.requestPaused', async paused => {
  if (paused.request.method !== 'GET') {
    methods.add(paused.request.method);
    await cmd('Fetch.failRequest', { requestId: paused.requestId, errorReason: 'BlockedByClient' }, sessionId);
  } else if (paused.resourceType === 'Image') {
    statuses.images += 1;
    await cmd('Fetch.fulfillRequest', {
      requestId: paused.requestId, responseCode: 200,
      responseHeaders: [{ name: 'Content-Type', value: 'image/png' }, { name: 'Cache-Control', value: 'no-store' }],
      body: pixel.toString('base64'),
    }, sessionId);
  } else {
    await cmd('Fetch.continueRequest', { requestId: paused.requestId }, sessionId);
  }
}, sessionId);
on('Page.loadEventFired', () => { if (loadResolve) { loadResolve(); loadResolve = null; } }, sessionId);

const results = [];
for (const route of routes) for (const width of widths) for (const theme of themes) {
  const errorsBefore = jsErrors;
  const height = width === 390 ? 844 : width === 768 ? 1024 : 1000;
  await cmd('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile: width <= 600 }, sessionId);
  await cmd('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-color-scheme', value: theme }] }, sessionId);
  const loaded = new Promise((resolve, reject) => {
    const timer = setTimeout(() => { loadResolve = null; reject(new Error('page load timeout')); }, 15000);
    loadResolve = () => { clearTimeout(timer); resolve(); };
  });
  await cmd('Page.navigate', { url: 'http://127.0.0.1:' + port + '/admin/v2/?qaTheme=' + theme + '#' + route.hash }, sessionId);
  let loadError = '';
  try { await loaded; } catch (error) { loadError = error.name; }
  await sleep(800);
  const evaluated = await cmd('Runtime.evaluate', {
    expression: "JSON.stringify({visible:[...document.querySelectorAll('.view')].filter(v=>!v.hidden).map(v=>v.dataset.view),activeRoute:document.querySelector('.primary-nav a[aria-current=page]')?.dataset.route||'',innerWidth,documentWidth:document.documentElement.scrollWidth,adminAccessPresent:!!document.querySelector('#adminAccessMode'),activeCountPresent:!!document.querySelector('#activeTaskCount'),enrichmentRows:[...document.querySelectorAll('#enrichmentRows .enrichment-source-heading strong')].map(node=>node.textContent),enrichmentFreshness:document.querySelector('#enrichmentFreshness')?.dataset.state||'',enrichmentFeedback:document.querySelector('#enrichmentFeedback')?.textContent||''})",
    returnByValue: true,
  }, sessionId);
  let view = {};
  try { view = JSON.parse(evaluated.result.value || '{}'); } catch {}
  if (width !== 768) {
    const screenshot = await cmd('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false }, sessionId);
    const routeName = route.hash.replace(/[^A-Za-z0-9_-]+/g, '-').replace(/^-|-$/g, '');
    fs.writeFileSync(`/output/${routeName}-${width}-${theme}.png`, Buffer.from(screenshot.data, 'base64'));
  }
  const expectedEmptyEnrichment = view.enrichmentRows?.length === 0 &&
    view.enrichmentFeedback.includes('No external metadata attempts have been recorded yet.');
  const enrichmentLabelsSafe = Array.isArray(view.enrichmentRows) && view.enrichmentRows.every(label =>
    !/(?:lukayan|telegram)_/i.test(label));
  const enrichmentLoaded = !route.enrichment || (view.enrichmentFreshness === 'fresh' &&
    (view.enrichmentRows.length > 0 ? enrichmentLabelsSafe : expectedEmptyEnrichment));
  const result = {
    route: route.hash, width, theme,
    ok: !loadError && view.visible?.length === 1 && view.visible[0] === route.view &&
      view.activeRoute === route.active && view.innerWidth === width && view.documentWidth <= width &&
      jsErrors === errorsBefore && view.adminAccessPresent && view.activeCountPresent &&
      enrichmentLoaded && enrichmentLabelsSafe,
    overflow: view.documentWidth > width,
    errors: jsErrors - errorsBefore,
    ...(route.enrichment ? {
      enrichmentRows: view.enrichmentRows?.length || 0,
      enrichmentLabelsSafe,
      expectedEmptyEnrichment,
      enrichmentLoaded,
    } : {}),
  };
  results.push(result);
  console.log((result.ok ? 'PASS ' : 'FAIL ') + route.hash + '-' + width + '-' + theme +
    ' overflow=' + result.overflow + ' jsErrors=' + result.errors + (loadError ? ' load=' + loadError : ''));
}
// Exercise native modal focus behavior in a real Chromium runtime, not a
// showModal()-only unit stub: Tab/Shift+Tab remain inside and Escape restores
// focus to the control that opened the Command Palette.
await cmd('Page.navigate', { url: 'http://127.0.0.1:' + port + '/admin/v2/?qaTheme=light#start' }, sessionId);
await sleep(800);
await cmd('Runtime.evaluate', { expression: "document.querySelector('#commandPaletteTrigger')?.click()" }, sessionId);
await sleep(100);
const focusableCountEval = await cmd('Runtime.evaluate', {
  expression: "document.querySelectorAll('#commandPalette button:not([disabled]),#commandPalette input:not([disabled]),#commandPalette select:not([disabled]),#commandPalette textarea:not([disabled]),#commandPalette a[href],[tabindex]:not([tabindex=\\\"-1\\\"])').length",
  returnByValue: true,
}, sessionId);
const paletteFocusableCount = Number(focusableCountEval.result.value || 0);
const focusInPalette = async () => {
  const evaluated = await cmd('Runtime.evaluate', {
    expression: "!!document.querySelector('#commandPalette')?.contains(document.activeElement)", returnByValue: true,
  }, sessionId);
  return evaluated.result.value === true;
};
const tabResults = [];
for (const modifiers of [0, 0, 8, 8]) {
  await cmd('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Tab', code: 'Tab', windowsVirtualKeyCode: 9, modifiers }, sessionId);
  await cmd('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Tab', code: 'Tab', windowsVirtualKeyCode: 9, modifiers }, sessionId);
  tabResults.push(await focusInPalette());
}
await cmd('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 }, sessionId);
await cmd('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 }, sessionId);
const paletteCloseEval = await cmd('Runtime.evaluate', {
  expression: "({closed:!document.querySelector('#commandPalette')?.open,restored:document.activeElement?.id==='commandPaletteTrigger'})",
  returnByValue: true,
}, sessionId);
const paletteKeyboard = {
  focusableCount: paletteFocusableCount,
  tabStayedInDialog: tabResults.every(Boolean),
  escapeClosed: paletteCloseEval.result.value?.closed === true,
  focusRestored: paletteCloseEval.result.value?.restored === true,
};
paletteKeyboard.ok = paletteKeyboard.focusableCount > 0 && paletteKeyboard.tabStayedInDialog &&
  paletteKeyboard.escapeClosed && paletteKeyboard.focusRestored;
console.log((paletteKeyboard.ok ? 'PASS ' : 'FAIL ') + 'Command Palette modal keyboard focus ' + JSON.stringify(paletteKeyboard));
const summary = {
  count: results.length, passed: results.filter(result => result.ok).length,
  failed: results.filter(result => !result.ok).length,
  apiRequests: statuses.total, api2xx: statuses.ok, api3xx: statuses.redirects,
  api4xx: statuses.clientError, api5xx: statuses.serverError, imageResponsesReplaced: statuses.images,
  writeMethods: [...methods].filter(method => method !== 'GET'), paletteKeyboard, results,
};
fs.writeFileSync('/output/live-results.json', JSON.stringify(summary, null, 2));
console.log('SUMMARY ' + summary.passed + '/' + summary.count + '; API=' + statuses.total +
  ' (2xx=' + statuses.ok + ', 3xx=' + statuses.redirects + ', 4xx=' + statuses.clientError + ', 5xx=' + statuses.serverError +
  '); image responses replaced=' + statuses.images + '; writes=' + summary.writeMethods.length);
ws.close();
chrome.kill('SIGTERM');
server.close();
if (summary.failed || !paletteKeyboard.ok || summary.writeMethods.length || summary.count !== routes.length * widths.length * themes.length) process.exitCode = 1;
