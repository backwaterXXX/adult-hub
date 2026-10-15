const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor(tag = 'div') { this.tagName = tag; this.children = []; this.listeners = {}; this.hidden = false; this.value = '7'; }
  addEventListener(type, callback) { this.listeners[type] = callback; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  remove() { this.removed = true; }
  click() { this.clicked = true; }
}

(async () => {
  const ids = ['diagnosticsFeedback', 'diagnosticsSummary', 'diagnosticsRows', 'diagnosticsDetail',
    'diagnosticsExportDays', 'diagnosticsExport', 'diagnosticsEnable', 'diagnosticsDisable', 'diagnosticsClear', 'diagnosticsRefresh'];
  const elements = Object.fromEntries(ids.map(id => [id, new Element(id === 'diagnosticsExportDays' ? 'select' : 'div')]));
  const links = [];
  const calls = [];
  const document = {
    querySelector: selector => elements[selector.slice(1)],
    createElement: tag => { const node = new Element(tag); if (tag === 'a') links.push(node); return node; },
    addEventListener() {},
    body: { append(node) { links.push(node); } },
  };
  const blobUrls = new Map();
  let nextUrl = 0;
  const urlApi = {
    createObjectURL(blob) { const url = `blob:test-${++nextUrl}`; blobUrls.set(url, blob); return url; },
    revokeObjectURL(url) { blobUrls.delete(url); },
  };
  const window = {
    AdminV2: {
      async getJson(path) {
        calls.push(path);
        if (path.endsWith('/reports/safe')) return { ok: true, report: { report_id: 'safe', outcome: 'success', provider: 'telegram' } };
        return { ok: true, scope: { days: 7, limit: 100 }, reports: [{ report_id: 'safe', outcome: 'success' }] };
      },
      async postJson() { return { ok: true }; },
      async deleteJson(path) { calls.push(`DELETE ${path}`); return { ok: true, cleared: 2 }; },
      setFeedback(target, message) { if (target) target.textContent = message; },
      downloadBlob(filename, blob) {
        const link = document.createElement('a');
        link.href = urlApi.createObjectURL(blob);
        link.download = filename;
        document.body.append(link);
        link.click();
      },
    },
    confirm() { return true; },
    setTimeout() { return 1; },
  };
  const context = { window, document, URL: urlApi, Blob, Date, location: { hash: '' }, encodeURIComponent,
    console, setTimeout: window.setTimeout };
  vm.runInNewContext(fs.readFileSync('resolver/static/admin-v2/diagnostics.js', 'utf8'), context);
  await elements.diagnosticsExport.listeners.click();
  const link = links.find(node => node.tagName === 'a');
  assert.equal(calls[0], '/admin/api/v2/diagnostics/playback/export?days=7&limit=100');
  assert.equal(link.download, `adult-hub-playback-diagnostics-${new Date().toISOString().slice(0, 10)}.json`);
  assert.equal(link.clicked, true);
  const downloaded = JSON.parse(await blobUrls.get(link.href).text());
  assert.equal(downloaded.reports[0].report_id, 'safe');
  assert.match(elements.diagnosticsFeedback.textContent, /Downloaded 1 redacted playback reports from the last 7 days/);
  process.stdout.write('PASS: bounded diagnostics export request, JSON download, and user feedback\n');
})().catch(error => { console.error(error); process.exitCode = 1; });
