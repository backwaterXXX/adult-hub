const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor(tag = 'div') { this.tagName = tag; this.children = []; this.listeners = {}; this.hidden = false; this.textContent = ''; this.value = '7'; }
  addEventListener(type, callback) { this.listeners[type] = callback; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  remove() { this.removed = true; }
  click() { this.clicked = true; }
}
function deferred() { let resolve; const promise = new Promise(yes => { resolve = yes; }); return { promise, resolve }; }
function textOf(node) { return `${node.textContent || ''} ${(node.children || []).map(textOf).join(' ')}`; }

(async () => {
  const ids = ['diagnosticsFeedback', 'diagnosticsProbeRows', 'diagnosticsSummary', 'diagnosticsRows', 'diagnosticsDetail',
    'diagnosticsExportDays', 'diagnosticsExport', 'diagnosticsEnable', 'diagnosticsDisable', 'diagnosticsClear', 'diagnosticsRefresh'];
  const elements = Object.fromEntries(ids.map(id => [id, new Element()]));
  const calls = [];
  const links = [];
  const blobs = new Map();
  let nextBlob = 0;
  let confirmResult = true;
  let deleteCalls = 0;
  let reportItems = [{ report_id: '../private/report 1', provider: 'telegram', outcome: 'error', started_epoch: 1700000000 }];
  const document = {
    querySelector: selector => elements[selector.slice(1)],
    createElement: tag => { const element = new Element(tag); if (tag === 'a') links.push(element); return element; },
    addEventListener() {},
    body: { append(element) { links.push(element); } },
  };
  const URL = {
    createObjectURL(blob) { const value = `blob:report-${++nextBlob}`; blobs.set(value, blob); return value; },
    revokeObjectURL() {},
  };
  const window = {
    AdminV2: {
      async getJson(path) {
        calls.push(path);
        if (path.endsWith('/status')) return { enabled: false, saved_count: 1 };
        if (path.endsWith('/summary')) return { analyzed: 1, by_provider: [] };
        if (path.endsWith('/diagnostics/probes')) return {
          checked_at: new Date().toISOString(), cache_age_seconds: 0,
          services: [
            { service: 'postgresql', status: 'healthy', elapsed_ms: 4 },
            { service: 'jellyfin', status: 'unavailable', http_status: 503, elapsed_ms: 22, url: 'https://secret.example/?token=secret' },
            { service: 'unknown-injected', label: 'Injected', status: 'healthy' },
          ],
        };
        if (path.endsWith('/reports?limit=50')) return { items: reportItems };
        if (path.includes('/reports/')) return { report: { report_id: '../private/report 1', provider: 'telegram', outcome: 'error', error_type: 'TimeoutError', safe_metric: 19 } };
        throw new Error(`Unexpected request: ${path}`);
      },
      async postJson() { return { ok: true }; },
      async deleteJson(path) { calls.push(`DELETE ${path}`); deleteCalls += 1; reportItems = []; return { ok: true, cleared: 2 }; },
      setFeedback(target, message) { if (target) target.textContent = message; },
      downloadBlob(filename, blob) {
        const link = document.createElement('a');
        link.href = URL.createObjectURL(blob);
        link.download = filename;
        document.body.append(link);
        link.click();
      },
    },
    setTimeout() { return 1; },
    confirm() { return confirmResult; },
  };
  const context = { window, document, URL, Blob, Date, location: { hash: '#system?tab=diagnostics' }, encodeURIComponent,
    console, setTimeout: window.setTimeout };
  vm.runInNewContext(fs.readFileSync('resolver/static/admin-v2/diagnostics.js', 'utf8'), context);
  await new Promise(resolve => setImmediate(resolve));
  assert.match(textOf(elements.diagnosticsProbeRows), /PostgreSQL/);
  assert.match(textOf(elements.diagnosticsProbeRows), /HTTP 503/);
  assert.doesNotMatch(textOf(elements.diagnosticsProbeRows), /secret|Injected|secret\.example/,
    'probe rendering uses known service labels and never exposes URLs or injected fields');
  const row = elements.diagnosticsRows.children[0];
  await row.children[2].listeners.click();
  const link = links.find(element => element.tagName === 'a');
  assert.equal(calls.at(-1), '/admin/api/v2/diagnostics/playback/reports/..%2Fprivate%2Freport%201');
  assert.equal(link.clicked, true);
  assert.match(link.download, /^adult-hub-playback-report-[A-Za-z0-9_-]+\.json$/);
  const payload = JSON.parse(await blobs.get(link.href).text());
  assert.equal(payload.report.safe_metric, 19);
  assert.equal(elements.diagnosticsFeedback.textContent, 'Downloaded a redacted playback report.');
  assert.equal(elements.diagnosticsClear.disabled, false);
  const staleReports = deferred(), currentReports = deferred();
  let reportListRequest = 0;
  window.AdminV2.getJson = async path => {
    if (path.endsWith('/status')) return { enabled: false, saved_count: 1 };
    if (path.endsWith('/summary')) return { analyzed: 0, by_provider: [] };
    if (path.endsWith('/reports?limit=50')) return (reportListRequest++ === 0 ? staleReports.promise : currentReports.promise);
    throw new Error(`Unexpected race-test request: ${path}`);
  };
  const oldRefresh = elements.diagnosticsRefresh.listeners.click();
  const latestRefresh = elements.diagnosticsRefresh.listeners.click();
  currentReports.resolve({ items: [] }); await latestRefresh;
  staleReports.resolve({ items: [{ report_id: 'stale', provider: 'telegram', outcome: 'error', started_epoch: 1700000000 }] }); await oldRefresh;
  assert.match(elements.diagnosticsRows.children[0].textContent, /No playback reports/,
    'a stale report-list response must not repopulate the UI after a newer refresh');
  assert.equal(elements.diagnosticsClear.disabled, true);
  let failListRefresh = false;
  window.AdminV2.getJson = async path => {
    calls.push(path);
    if (path.endsWith('/status')) return { enabled: false, saved_count: 1 };
    if (path.endsWith('/summary')) return { analyzed: 1, by_provider: [] };
    if (path.endsWith('/reports?limit=50')) {
      if (failListRefresh) throw new Error('Fixture refresh failure.');
      return { items: reportItems };
    }
    if (path.includes('/reports/')) return { report: { report_id: '../private/report 1', provider: 'telegram', outcome: 'error', error_type: 'TimeoutError', safe_metric: 19 } };
    throw new Error(`Unexpected request: ${path}`);
  };
  confirmResult = false;
  await elements.diagnosticsClear.listeners.click();
  assert.equal(deleteCalls, 0, 'cancelled confirmation must not delete any reports');
  confirmResult = true;
  await elements.diagnosticsClear.listeners.click();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(deleteCalls, 1);
  assert.equal(calls.at(-1), '/admin/api/v2/diagnostics/playback/reports?limit=50');
  assert.match(elements.diagnosticsRows.children[0].textContent, /No playback reports have been captured/);
  assert.match(elements.diagnosticsFeedback.textContent, /Deleted 2 saved playback reports/,
    'a successful refresh after clearing must preserve the deletion confirmation');
  assert.equal(elements.diagnosticsClear.disabled, true);

  reportItems = [{ report_id: 'remaining', provider: 'telegram', outcome: 'error', started_epoch: 1700000000 }];
  await elements.diagnosticsRefresh.listeners.click();
  assert.equal(elements.diagnosticsClear.disabled, false);
  failListRefresh = true;
  await elements.diagnosticsClear.listeners.click();
  assert.match(elements.diagnosticsFeedback.textContent, /Playback diagnostics are unavailable\. Fixture refresh failure\./,
    'a failed post-clear refresh error must remain visible instead of being replaced by a success message');
  process.stdout.write('PASS: report export is redacted; clear confirms success and preserves refresh errors\n');
})().catch(error => { console.error(error); process.exitCode = 1; });
