const assert = require('node:assert/strict');
const { createAdminV2Helpers } = require('./helpers/admin_v2_shared_helpers.cjs');
class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.textContent = ''; this.hidden = true; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  setAttribute() {}
  contains(node) { return this === node || this.children.some(child => child.contains(node)); }
  querySelector(selector) { return this.children.find(node => node.className?.includes(selector.slice(1))) || this.children.map(node => node.querySelector(selector)).find(Boolean) || null; }
  get firstElementChild() { return this.children[0]; }
}
const receipt = new Element('div'), live = new Element('div');
let currentDialog = null;
const document = {
  createElement: tag => new Element(tag),
  querySelector: selector => selector === '#taskReceipt' ? receipt : selector === '#liveMessage' ? live : selector === 'dialog[open]' ? currentDialog : null,
};
const helpers = createAdminV2Helpers(document);
for (const [queue, suffix] of [['jobs', ''], ['phash', '&job_queue=phash'], ['visual', '&job_queue=visual']]) {
  const link = helpers.taskLink('task-12', queue);
  assert.equal(link.textContent, 'View task');
  assert.equal(link.href, `/admin/v2/?job_id=task-12${suffix}#jobs`);
}
assert.equal(helpers.taskLink('x?token=private'), null);
helpers.showQueued({ jobs: [{ job_id: 'task-a' }, { job_id: 'task-b' }, { job_id: 'task-a' }] }, 'Metadata update');
assert.equal(receipt.hidden, false);
assert.equal(receipt.children[0].textContent, 'Queued · Metadata update');
assert.equal(receipt.children.length, 3, 'each queued task has one durable detail link');
assert.equal(live.textContent, 'Queued · Metadata update');
assert.doesNotMatch(receipt.children[0].textContent, /succeeded|completed/i);
console.log('PASS: queued receipts link all accepted tasks without claiming completion');

const fs = require('node:fs'), vm = require('node:vm');
const jobsSource = fs.readFileSync('resolver/static/admin-v2/jobs.js', 'utf8');
const start = jobsSource.indexOf('  function progressText(');
const end = jobsSource.indexOf('  function appendJobRow(', start);
const progress = vm.runInNewContext(jobsSource.slice(start, end) + '; progressText', { stageLabel: () => 'Progress not reported' });
assert.equal(progress({ state: 'processing', progress: {} }), 'Progress not reported');
assert.doesNotMatch(progress({ state: 'processing', progress: { total: 5 } }), /0%/);
assert.equal(progress({ state: 'processing', progress: { completed: 2, total: 5 } }), '2 / 5 · 40%');
assert.equal(progress({ state: 'processing', progress: { completed: 0, total: 5 } }), '0 / 5 · 0%');

currentDialog = new Element('dialog');
currentDialog.append(new Element('section'));
helpers.showQueued({ job_id: 'inside-dialog' }, 'Artwork');
const modalReceipt = currentDialog.querySelector('.task-receipt');
assert.ok(modalReceipt, 'a queued operation exposes its receipt inside the active modal');
assert.ok(modalReceipt.children.some(node => node.href?.includes('job_id=inside-dialog')), 'View task remains reachable while the modal is open');
