const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase();
    this.listeners = {};
    this.children = [];
    this.hidden = false;
    this._disabled = false;
    this.value = '';
    this.textContent = '';
    this.className = '';
    this.type = '';
    this.dataset = {};
    this.attributes = {};
    this.isConnected = true;
  }
  get disabled() { return this._disabled; }
  set disabled(value) {
    this._disabled = value;
    if (value && this.context?.document.activeElement === this) this.context.document.activeElement = this.context.document.body;
  }
  focus() { this.context.document.activeElement = this; }
  addEventListener(type, callback) { this.listeners[type] = callback; }
  append(...nodes) { this.children.push(...nodes); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  replaceChildren(...nodes) { this.children = nodes; }
  reset() {
    this.context.elements.scheduleName.value = '';
    this.context.elements.scheduleAction.value = 'availability_check';
    this.context.elements.scheduleCron.value = '0 3 * * *';
    this.context.elements.scheduleVideoIds.value = '';
  }
}
function deferred() { let resolve; const promise = new Promise(yes => { resolve = yes; }); return { promise, resolve }; }

const template = fs.readFileSync('resolver/templates/admin_v2.html', 'utf8');
const ids = ['schedulesFeedback', 'schedulesRows', 'scheduleCreateForm', 'scheduleCreate',
  'scheduleCreatePanel', 'scheduleCreatePreview', 'scheduleName', 'scheduleAction', 'scheduleCron',
  'scheduleVideosField', 'scheduleVideoIds', 'scheduleFilter', 'schedulesRefresh'];
const elements = Object.fromEntries(ids.map(id => [id, new Element(id === 'scheduleCreateForm' ? 'form' : 'div')]));
const documentListeners = {};
const document = {
  body: new Element('body'), activeElement: null,
  hidden: false,
  querySelector(selector) { return elements[selector.slice(1)] || null; },
  createElement(tag) { return new Element(tag); },
  addEventListener(type, callback) { documentListeners[type] = callback; },
};
document.activeElement = document.body;
for (const element of Object.values(elements)) element.context = { elements, document };
elements.scheduleAction.value = 'availability_check';
elements.scheduleCron.value = '0 3 * * *';

const labelRefs = [...template.matchAll(/<label\b[^>]*for="([^"]+)"/g)].map(([, id]) => id);
for (const id of ['scheduleName', 'scheduleAction', 'scheduleCron', 'scheduleVideoIds']) {
  assert.ok(labelRefs.includes(id), `Schedule field ${id} must have an associated label`);
}

const calls = [];
const intervals = new Map();
let nextIntervalId = 1;
let failNextPost = false;
let failNextGet = false;
let releasePost;
let holdNextPost = false;
const context = {
  document,
  location: { hash: '#schedules' },
  window: {
    AdminV2: {
      async getJson(path) {
        calls.push({ method: 'GET', path });
        if (failNextGet) { failNextGet = false; throw new Error('The schedule service is unavailable.'); }
        if (path.includes('cron-preview?')) return { next_runs: [
          '2026-11-01T02:00:00Z', '2026-11-02T02:00:00Z', '2026-11-03T02:00:00Z',
        ] };
        return { items: [] };
      },
      async postJson(path, payload) {
        calls.push({ method: 'POST', path, payload: structuredClone(payload) });
        if (holdNextPost) {
          holdNextPost = false;
          await new Promise(resolve => { releasePost = resolve; });
        }
        if (failNextPost) { failNextPost = false; throw new Error('The runner rejected this schedule.'); }
        return { id: 'schedule-1' };
      },
    },
  },
  Date, Intl, String, Number, Boolean, Object, Array, Promise, RegExp, URLSearchParams,
  setInterval(callback, delay) {
    const id = nextIntervalId++;
    intervals.set(id, { callback, delay });
    return id;
  },
  clearInterval(id) { intervals.delete(id); },
  setTimeout(callback) { return setTimeout(callback, 0); },
  clearTimeout,
};

(async () => {
  vm.runInNewContext(fs.readFileSync('resolver/static/admin-v2/schedules.js', 'utf8'), context);
  assert(calls.some(call => call.method === 'GET' && call.path === '/admin/api/v2/schedules'),
    'opening #schedules directly loads the existing schedule API');
  assert.equal(intervals.size, 1, 'opening the Schedules tab directly starts its bounded polling timer');
  assert.equal(elements.scheduleVideosField.hidden, false);
  await new Promise(resolve => setTimeout(resolve, 20));

  const staleList = deferred(), currentList = deferred();
  let listCalls = 0;
  context.window.AdminV2.getJson = async () => (listCalls++ === 0 ? staleList.promise : currentList.promise);
  const oldRefresh = elements.schedulesRefresh.listeners.click();
  const latestRefresh = elements.schedulesRefresh.listeners.click();
  currentList.resolve({ items: [
    { id: 'current', name: 'Current schedule', action: 'backup_create', enabled: true,
      next_run_at: '2026-10-11T01:30:00+00:00', last_run_at: '2026-10-10T01:30:00+00:00',
      last_result: 'done', last_job_id: 'job_123' },
    { id: 'invalid-job', name: 'Invalid job reference', action: 'backup_create', enabled: true,
      last_run_at: null, last_result: 'done', last_job_id: '<script>' },
  ] });
  await latestRefresh;
  staleList.resolve({ items: [{ id: 'stale', name: 'Stale schedule', action: 'backup_create', enabled: true }] });
  await oldRefresh;
  function currentBody() { return elements.schedulesRows.children[0].children[0].children[1]; }
  assert.equal(currentBody().children[0].children[0].children[0].textContent, 'Current schedule',
    'a stale schedule read must not replace the latest list');
  assert.equal(currentBody().children[0].children[0].children[1].textContent, 'Database backup');
  const currentCopy = currentBody().children[0];
  assert.equal(currentCopy.children[3].children[0].tagName, 'TIME');
  assert.equal(currentCopy.children[3].children[0].dateTime, '2026-10-11T01:30:00.000Z',
    'next run timestamps must use semantic, machine-readable time markup');
  assert.equal(currentCopy.children[4].children[0].tagName, 'TIME');
  assert.equal(currentCopy.children[4].children[0].dateTime, '2026-10-10T01:30:00.000Z',
    'last run timestamps must use semantic, machine-readable time markup');
  assert.match(currentCopy.children[4].children[1].textContent, /Completed/,
    'schedule history must include the last run result');
  assert.equal(currentCopy.children[0].children[2].tagName, 'A');
  assert.equal(currentCopy.children[0].children[2].textContent, 'Open latest task');
  assert.equal(currentCopy.children[0].children[2].href, '/admin/v2/?job_id=job_123#jobs');
  assert.equal(currentCopy.children[0].children[2].attributes['aria-label'], 'Open task for Current schedule');
  assert.equal(currentBody().children[2].children[0].children.length, 2,
    'an invalid job ID must not produce a task link');
  assert.equal(currentBody().children[2].children[4].children[0].textContent, '—',
    'an unavailable last-run timestamp must not render Invalid Date');
  context.window.AdminV2.getJson = async path => path.includes('cron-preview?')
    ? { next_runs: ['2026-11-01T02:00:00Z', '2026-11-02T02:00:00Z', '2026-11-03T02:00:00Z'] }
    : ({ items: [{ id: 'duplicate-reconcile', name: 'Duplicate reconciliation',
      action: 'duplicate_reconcile', enabled: false, schedule_type: 'interval', interval_minutes: 15, revision: 4 },
    { id: 'catalog-health-full', name: 'Catalog health: daily full audit', action: 'catalog_full_audit',
      enabled: true, schedule_type: 'daily', daily_at: '03:00', revision: 2 }] });
  await elements.schedulesRefresh.listeners.click();
  const body = currentBody();
  const currentRow = body.children[0];
  const wrap = body.children[1];
  const editor = wrap.children[0].children[0];
  assert.equal(wrap.hidden, true, 'editor is compact until opened');
  currentRow.children[5].children[0].listeners.click();
  const frequency = editor.children[1].children[0].children[0];
  const cronInput = editor.children[1].children[3].children[0];
  assert.equal(cronInput.value, '*/15 * * * *');
  frequency.value = 'custom'; frequency.listeners.change();
  cronInput.value = '0 3 * * *';
  cronInput.listeners.input();
  await new Promise(resolve => setTimeout(resolve, 20));
  await editor.children[4].children[0].listeners.click();
  assert.deepEqual(calls.filter(call => call.method === 'POST').at(-1), { method: 'POST', path: '/admin/api/v2/schedules/duplicate-reconcile/cron',
    payload: { cron_expression: '0 3 * * *', revision: 4 } });
  const auditRow = currentBody().children[2];
  const auditWrap = currentBody().children[3];
  auditRow.children[5].children[0].listeners.click();
  const auditEditor = auditWrap.children[0].children[0];
  assert.equal(auditEditor.children[1].children[0].children[0].value, 'daily');
  assert.equal(auditEditor.children[1].children[1].children[0].value, '03:00');
  auditEditor.children[4].children[1].listeners.click();
  context.window.AdminV2.getJson = async path => {
    calls.push({ method: 'GET', path });
    if (failNextGet) { failNextGet = false; throw new Error('The schedule service is unavailable.'); }
    if (path.includes('cron-preview?')) return { next_runs: ['2026-11-01T02:00:00Z', '2026-11-02T02:00:00Z', '2026-11-03T02:00:00Z'] };
    return { items: [] };
  };

  elements.scheduleAction.value = 'consistency_audit';
  elements.scheduleAction.listeners.change();
  assert.equal(elements.scheduleVideosField.hidden, true, 'non-availability actions hide the video-ID scope');

  elements.scheduleName.value = 'Nightly availability';
  elements.scheduleAction.value = 'availability_check';
  elements.scheduleAction.listeners.change();
  elements.scheduleVideoIds.value = 'video-1\n\nvideo-2 ';
  elements.scheduleCron.value = '0 3 * * *';
  elements.scheduleCron.listeners.input();
  await new Promise(resolve => setTimeout(resolve, 20));
  holdNextPost = true;
  const submit = elements.scheduleCreateForm.listeners.submit;
  let prevented = false;
  const pending = submit({ preventDefault() { prevented = true; } });
  assert.equal(prevented, true);
  assert.equal(elements.scheduleCreate.disabled, true, 'the submit button remains disabled while saving');
  elements.scheduleName.value = 'Weekly audit draft';
  elements.scheduleAction.value = 'consistency_audit';
  elements.scheduleAction.listeners.change();
  elements.scheduleCron.value = '15 4 * * 1';
  elements.scheduleCron.listeners.input();
  elements.scheduleVideoIds.value = 'draft-video';
  elements.scheduleAction.focus();
  releasePost();
  await pending;
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.equal(elements.scheduleCreate.disabled, false);
  assert.equal(document.activeElement, elements.scheduleAction,
    'a completion must not steal focus if the operator moved to another field while the request was pending');
  const createCalls = calls.filter(call => call.method === 'POST' && call.path === '/admin/api/v2/schedules');
  assert.deepEqual(createCalls[0], {
    method: 'POST',
    path: '/admin/api/v2/schedules',
    payload: {
      name: 'Nightly availability', action: 'availability_check', schedule_type: 'cron',
      timezone: 'Europe/Warsaw', cron_expression: '0 3 * * *', video_ids: ['video-1', 'video-2'],
    },
  });
  assert.equal(elements.schedulesFeedback.textContent, 'Schedule created. Your newer draft was kept.');
  assert.equal(elements.scheduleCreateForm.context.elements.scheduleName.value, 'Weekly audit draft');
  assert.equal(elements.scheduleAction.value, 'consistency_audit');
  assert.equal(elements.scheduleCron.value, '15 4 * * 1');
  assert.equal(elements.scheduleVideoIds.value, 'draft-video');
  assert.equal(elements.scheduleVideosField.hidden, true, 'the conditional scope matches the newer action draft');

  elements.scheduleName.value = 'Retried backup';
  elements.scheduleAction.value = 'backup_create';
  elements.scheduleAction.listeners.change();
  elements.scheduleCron.value = '30 2 * * *';
  elements.scheduleCron.listeners.input();
  await new Promise(resolve => setTimeout(resolve, 20));
  elements.scheduleCreate.focus();
  failNextPost = true;
  failNextGet = true;
  await submit({ preventDefault() {} });
  assert.equal(elements.scheduleCreate.disabled, false, 'a failed request must restore the submit button');
  assert.equal(elements.scheduleName.value, 'Retried backup', 'a failed request must preserve the entered name');
  assert.equal(elements.scheduleCron.value, '30 2 * * *', 'a failed request must preserve the entered cron expression');
  assert.equal(document.activeElement, elements.scheduleCreate,
    'a keyboard submission must return focus to the re-enabled create button after an error');
  assert.match(elements.schedulesFeedback.textContent, /Could not create schedule\. The runner rejected this schedule\./);

  await submit({ preventDefault() {} });
  const retry = calls.filter(call => call.method === 'POST').at(-1);
  assert.deepEqual(retry.payload, {
    name: 'Retried backup', action: 'backup_create', schedule_type: 'cron',
    timezone: 'Europe/Warsaw', cron_expression: '30 2 * * *',
  });
  assert.equal(elements.schedulesFeedback.textContent,
    'Schedule created, but the list could not be refreshed.',
    'a successful create must not be reported as a failed create when its follow-up list read fails');

  await new Promise(resolve => setTimeout(resolve, 20));
  let refreshCalls = 0;
  context.window.AdminV2.getJson = async () => { refreshCalls += 1; return { items: [] }; };
  documentListeners['adminv2:navigate']({ detail: { route: 'schedules' } });
  assert.equal(refreshCalls, 1, 'entering System loads schedules immediately');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(intervals.size, 1, 'System starts one schedule poll');
  const [[pollId, poll]] = intervals;
  assert.equal(poll.delay, 30000, 'schedule polling must remain low frequency');
  document.hidden = true;
  poll.callback();
  assert.equal(refreshCalls, 1, 'a hidden browser tab must not poll schedules');
  document.hidden = false;
  documentListeners.visibilitychange();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(refreshCalls, 2, 'returning to a visible tab refreshes schedules');
  const retainedRow = new Element('article');
  elements.schedulesRows.replaceChildren(retainedRow);
  context.window.AdminV2.getJson = async () => {
    refreshCalls += 1;
    throw new Error('The schedule service is unavailable.');
  };
  documentListeners.visibilitychange();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(refreshCalls, 3);
  assert.equal(elements.schedulesRows.children[0], retainedRow,
    'a failed background refresh must preserve the last rendered schedule data');
  assert.match(elements.schedulesFeedback.textContent, /Schedules may be out of date/);
  context.location.hash = '#system';
  documentListeners['adminv2:navigate']({ detail: { route: 'system' } });
  assert.equal(intervals.has(pollId), false, 'leaving System clears the schedule timer');
  poll.callback();
  assert.equal(refreshCalls, 3, 'a stale timer cannot poll after leaving System');
  document.hidden = true;
  context.window.AdminV2.getJson = async () => { refreshCalls += 1; return { items: [] }; };
  context.location.hash = '#schedules';
  documentListeners['adminv2:navigate']({ detail: { route: 'schedules' } });
  assert.equal(refreshCalls, 3, 'entering System in a hidden tab waits for visibility');
  const [[hiddenPollId, hiddenPoll]] = intervals;
  hiddenPoll.callback();
  assert.equal(refreshCalls, 3, 'hidden polling remains idle');
  document.hidden = false;
  documentListeners.visibilitychange();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(refreshCalls, 4, 'visibility restores one immediate refresh');
  context.location.hash = '#jobs?tab=tasks';
  documentListeners['adminv2:navigate']({ detail: { route: 'jobs' } });
  assert.equal(intervals.has(hiddenPollId), false);
  context.window.AdminV2.getJson = async () => ({ items: [], system_items: [
    { id: 'library-sync', name: 'Library sync', group: 'system', enabled: false,
      cron_expression: '0 * * * *', revision: 1 },
    { name: 'Scene identification', group: 'dependent', parent_id: 'library-sync', source: 'Library sync' }
  ] });
  await elements.schedulesRefresh.listeners.click();
  const grouped = currentBody();
  assert.equal(grouped.children[0].children[0].children[0].textContent, 'Library sync');
  assert.equal(grouped.children[2].className, 'schedule-dependent-row', 'dependent tasks follow their parent');
  assert.equal(grouped.children[2].children[0].children[0].children[0].children[0].children[0].textContent,
    'Scene identification');
  const runButton = grouped.children[0].children[5].children[2];
  assert.equal(runButton.textContent, 'Run');
  await runButton.listeners.click();
  assert.deepEqual(calls.filter(call => call.method === 'POST').at(-1), {
    method: 'POST', path: '/admin/api/v2/schedules/system/library-sync/run', payload: {}
  });
  process.stdout.write('PASS: schedule history links, visible-tab polling, safe stale errors, form validation, and retry\n');
})().catch(error => { console.error(error); process.exitCode = 1; });
