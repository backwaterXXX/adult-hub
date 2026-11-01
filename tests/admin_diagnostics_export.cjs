const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor(tag = 'div') { this.tagName = tag; this.children = []; this.listeners = {}; this.hidden = false; this.textContent = ''; this.className = ''; this.href = ''; }
  addEventListener(type, callback) { this.listeners[type] = callback; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
}
const ids = ['systemProbeRows', 'systemStatusChecked', 'systemRecentFailures', 'systemStatusView', 'systemStatusRefresh'];
const elements = Object.fromEntries(ids.map(id => [id, new Element()]));
elements.systemStatusView.hidden = false;
const calls = [];
const events = {};
const rows = Array.from({ length: 12 }, (_, i) => ({ id: `failed_${i}`, queue: 'jobs', action: 'backup_create', state: 'failed',
  created_at: 1_800_000_000 - i, updated_at: 1_800_000_100 - i, error: 'A required operation timed out.' }));
const document = {
  querySelector: selector => elements[selector.slice(1)] || null,
  createElement: tag => new Element(tag),
  addEventListener(type, callback) { events[type] = callback; },
  dispatchEvent(event) { events[event.type]?.(event); },
};
const window = { AdminV2: {
  currentRoute: () => ({ route: 'system', systemTab: 'status' }),
  async getJson(path) {
    calls.push(path);
    if (path.endsWith('/diagnostics/probes')) return { checked_at: '2026-11-01T10:00:00Z', cache_age_seconds: 0,
      services: [
        { service: 'postgresql', status: 'healthy', elapsed_ms: 12 },
        { service: 'sync_worker', status: 'unknown' },
        { service: 'jellyfin', status: 'unavailable', http_status: 503 },
      ] };
    if (path.includes('/tasks?')) return { available: true, tasks: rows, total: 12 };
    throw new Error(`Unexpected request: ${path}`);
  },
} };
const context = { window, document, Date, Intl, encodeURIComponent, CustomEvent: class { constructor(type) { this.type = type; } } };
(async () => {
  vm.runInNewContext(fs.readFileSync('resolver/static/admin-v2/diagnostics.js', 'utf8'), context);
  await new Promise(resolve => setImmediate(resolve));
  const flatten = node => `${node.textContent || ''} ${(node.children || []).map(flatten).join(' ')}`;
  const probes = flatten(elements.systemProbeRows);
  assert.match(probes, /PostgreSQL.*Healthy/);
  assert.match(probes, /Sync worker.*Unknown/);
  assert.match(probes, /Jellyfin.*Unavailable.*HTTP 503/);
  assert.match(elements.systemStatusChecked.textContent, /Last checked:/);
  assert.equal(elements.systemRecentFailures.children.length, 10, 'only ten task failures are shown');
  const firstLink = elements.systemRecentFailures.children[0].children[1];
  assert.equal(elements.systemRecentFailures.children[0].children[0].children[0].textContent, 'Database backup');
  assert.equal(firstLink.textContent, 'View task');
  assert.equal(firstLink.href, '/admin/v2/?job_id=failed_0&job_queue=jobs#jobs');
  assert.doesNotMatch(probes, /playback report|Catalog Health/);
  assert.deepEqual(calls, ['/admin/api/v2/diagnostics/probes', '/admin/api/v2/tasks?queue=all&state=failed&limit=10&offset=0']);
  assert(calls.every(path => !path.includes('/diagnostics/playback')),
    'System status must never query retired playback diagnostics');
  const initialProbeRows = elements.systemProbeRows.children.length;
  const getJson = window.AdminV2.getJson;
  window.AdminV2.getJson = async path => path.includes('/tasks?') ? Promise.reject(new Error('Queue temporarily unavailable.')) : getJson(path);
  await events['adminv2:refresh-system-status']();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(elements.systemProbeRows.children.length, initialProbeRows, 'partial task API failure does not erase service probes');
  assert.match(elements.systemRecentFailures.children[0].textContent, /Recent task failures are unavailable/);
  process.stdout.write('PASS: System probes preserve unknown versus unavailable, last-check time, and ten task error links\n');
})().catch(error => { console.error(error); process.exitCode = 1; });
