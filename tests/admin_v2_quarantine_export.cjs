const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor(tag = 'div') { this.tagName = tag; this.children = []; this.listeners = {}; this.textContent = ''; this.disabled = false; }
  addEventListener(type, callback) { this.listeners[type] = callback; }
  append(...nodes) { this.children.push(...nodes); }
  click() { this.clicked = true; }
  remove() { this.removed = true; }
}

async function run(pages) {
  const button = new Element('button');
  const feedback = new Element();
  const links = [];
  const blobs = new Map();
  let serial = 0;
  const document = {
    querySelector: selector => selector === '#quarantineExport' ? button : feedback,
    createElement(tag) { const node = new Element(tag); if (tag === 'a') links.push(node); return node; },
    body: { append(node) { links.push(node); } },
  };
  const URL = {
    createObjectURL(blob) { const value = `blob:quarantine-${++serial}`; blobs.set(value, blob); return value; },
    revokeObjectURL() {},
  };
  const calls = [];
  const window = {
    AdminV2: {
      async getJson(path) { calls.push(path); return pages.shift(); },
      setFeedback(target, message) { if (target) target.textContent = message; },
      downloadBlob(filename, blob) {
        const node = document.createElement('a');
        node.href = URL.createObjectURL(blob);
        node.download = filename;
        document.body.append(node);
        node.click();
      },
    },
    setTimeout() { return 1; },
  };
  const context = { document, URL, Blob, window, JSON, setTimeout: window.setTimeout };
  vm.runInNewContext(fs.readFileSync('resolver/static/admin-v2/quarantine.js', 'utf8'), context);
  await button.listeners.click();
  return { button, feedback, links, blobs, calls };
}

(async () => {
  const exported = await run([
    { available: true, items: [{ id: 'v1', operation_id: 'o1' }], total: 2, truncated: true, offset: 0 },
    { available: true, items: [{ id: 'v2', operation_id: 'o2' }], total: 2, truncated: false, offset: 1 },
  ]);
  assert.deepEqual(exported.calls, [
    '/admin/api/v2/organize/quarantine?limit=100&offset=0',
    '/admin/api/v2/organize/quarantine?limit=100&offset=1',
  ]);
  const link = exported.links.find(item => item.tagName === 'a');
  const report = JSON.parse(await exported.blobs.get(link.href).text());
  assert.deepEqual(report.items.map(item => item.id), ['v1', 'v2']);
  assert.equal(report.total, 2);
  assert.equal(link.download, 'quarantine-report.json');
  assert.match(exported.feedback.textContent, /exported \(2 items\)/);

  const largePages = Array.from({ length: 11 }, (_, pageIndex) => {
    const offset = pageIndex * 100;
    const count = Math.min(100, 1001 - offset);
    return {
      available: true,
      items: Array.from({ length: count }, (_, index) => ({
        id: `video-${offset + index}`, operation_id: `operation-${offset + index}`,
      })),
      total: 1001, truncated: offset + count < 1001, offset,
    };
  });
  const large = await run(largePages);
  assert.equal(large.calls.length, 11);
  assert.equal(large.calls[10], '/admin/api/v2/organize/quarantine?limit=100&offset=1000');
  const largeLink = large.links.find(item => item.tagName === 'a');
  const largeReport = JSON.parse(await large.blobs.get(largeLink.href).text());
  assert.equal(largeReport.items.length, 1001);
  assert.equal(largeReport.total, 1001);
  assert.match(large.feedback.textContent, /exported \(1001 items\)/);

  const incomplete = await run([
    { available: true, items: [{ id: 'v1', operation_id: 'o1' }], total: 3, truncated: false, offset: 0 },
  ]);
  assert.equal(incomplete.links.length, 0);
  assert.match(incomplete.feedback.textContent, /incomplete/);

  const changed = await run([
    { available: true, items: [{ id: 'v1', operation_id: 'o1' }], total: 2, truncated: true, offset: 0 },
    { available: true, items: [{ id: 'v2', operation_id: 'o2' }], total: 3, truncated: true, offset: 1 },
  ]);
  assert.equal(changed.links.length, 0, 'a changing report must never produce a partial download');
  assert.match(changed.feedback.textContent, /changed during export/);
  process.stdout.write('PASS: quarantine export validates complete pagination, including reports over 1000 rows\n');
})().catch(error => { console.error(error); process.exitCode = 1; });
