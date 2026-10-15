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
    this.context.elements.scheduleType.value = 'daily';
    this.context.elements.scheduleDailyAt.value = '03:00';
    this.context.elements.scheduleInterval.value = '1440';
    this.context.elements.scheduleVideoIds.value = '';
  }
}
function deferred() { let resolve; const promise = new Promise(yes => { resolve = yes; }); return { promise, resolve }; }

const template = fs.readFileSync('resolver/templates/admin_v2.html', 'utf8');
const ids = ['schedulesFeedback', 'schedulesRows', 'scheduleCreateForm', 'scheduleCreate',
  'scheduleName', 'scheduleAction', 'scheduleType', 'scheduleDailyField', 'scheduleDailyAt',
  'scheduleIntervalField', 'scheduleInterval', 'scheduleVideosField', 'scheduleVideoIds',
  'schedulesRefresh'];
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
elements.scheduleType.value = 'daily';
elements.scheduleDailyAt.value = '03:00';
elements.scheduleInterval.value = '1440';

const labelRefs = [...template.matchAll(/<label\b[^>]*for="([^"]+)"/g)].map(([, id]) => id);
for (const id of ['scheduleName', 'scheduleAction', 'scheduleType', 'scheduleDailyAt', 'scheduleInterval', 'scheduleVideoIds']) {
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
  location: { hash: '#jobs?tab=schedules' },
  window: {
    AdminV2: {
      async getJson(path) {
        calls.push({ method: 'GET', path });
        if (failNextGet) { failNextGet = false; throw new Error('The schedule service is unavailable.'); }
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
};

(async () => {
  vm.runInNewContext(fs.readFileSync('resolver/static/admin-v2/schedules.js', 'utf8'), context);
  assert(calls.some(call => call.method === 'GET' && call.path === '/admin/api/v2/schedules'),
    'opening #jobs?tab=schedules directly loads the existing schedule API');
  assert.equal(intervals.size, 1, 'opening the Schedules tab directly starts its bounded polling timer');
  assert.equal(elements.scheduleDailyField.hidden, false);
  assert.equal(elements.scheduleIntervalField.hidden, true);
  assert.equal(elements.scheduleVideosField.hidden, false);

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
  assert.equal(elements.schedulesRows.children[0].children[0].children[0].textContent,
    'Current schedule · Database backup', 'a stale schedule read must not replace the latest list');
  const currentCopy = elements.schedulesRows.children[0].children[0];
  assert.equal(currentCopy.children[1].children[1].tagName, 'TIME');
  assert.equal(currentCopy.children[1].children[1].dateTime, '2026-10-11T01:30:00.000Z',
    'next run timestamps must use semantic, machine-readable time markup');
  assert.equal(currentCopy.children[2].children[1].tagName, 'TIME');
  assert.equal(currentCopy.children[2].children[1].dateTime, '2026-10-10T01:30:00.000Z',
    'last run timestamps must use semantic, machine-readable time markup');
  assert.match(currentCopy.children[2].children[2].textContent, /Result: Completed/,
    'schedule history must include the last run result');
  assert.equal(currentCopy.children[3].tagName, 'A');
  assert.equal(currentCopy.children[3].textContent, 'Open task');
  assert.equal(currentCopy.children[3].href, '/admin/v2/?job_id=job_123#jobs');
  assert.equal(currentCopy.children[3].attributes['aria-label'], 'Open task for Current schedule');
  assert.equal(elements.schedulesRows.children[1].children[0].children.length, 3,
    'an invalid job ID must not produce a task link');
  assert.equal(elements.schedulesRows.children[1].children[0].children[2].children[1].textContent, '—',
    'an unavailable last-run timestamp must not render Invalid Date');
  context.window.AdminV2.getJson = async path => {
    calls.push({ method: 'GET', path });
    if (failNextGet) { failNextGet = false; throw new Error('The schedule service is unavailable.'); }
    return { items: [] };
  };

  elements.scheduleType.value = 'interval';
  elements.scheduleType.listeners.change();
  elements.scheduleAction.value = 'consistency_audit';
  elements.scheduleAction.listeners.change();
  assert.equal(elements.scheduleDailyField.hidden, true, 'interval frequency hides the daily time');
  assert.equal(elements.scheduleIntervalField.hidden, false, 'interval frequency shows its numeric interval');
  assert.equal(elements.scheduleVideosField.hidden, true, 'non-availability actions hide the video-ID scope');

  elements.scheduleName.value = 'Nightly availability';
  elements.scheduleAction.value = 'availability_check';
  elements.scheduleAction.listeners.change();
  elements.scheduleVideoIds.value = 'video-1\n\nvideo-2 ';
  elements.scheduleInterval.value = '60';
  holdNextPost = true;
  const submit = elements.scheduleCreateForm.listeners.submit;
  let prevented = false;
  const pending = submit({ preventDefault() { prevented = true; } });
  assert.equal(prevented, true);
  assert.equal(elements.scheduleCreate.disabled, true, 'the submit button remains disabled while saving');
  elements.scheduleName.value = 'Weekly audit draft';
  elements.scheduleAction.value = 'consistency_audit';
  elements.scheduleAction.listeners.change();
  elements.scheduleType.value = 'daily';
  elements.scheduleType.listeners.change();
  elements.scheduleDailyAt.value = '04:15';
  elements.scheduleInterval.value = '120';
  elements.scheduleVideoIds.value = 'draft-video';
  elements.scheduleAction.focus();
  releasePost();
  await pending;
  assert.equal(elements.scheduleCreate.disabled, false);
  assert.equal(document.activeElement, elements.scheduleAction,
    'a completion must not steal focus if the operator moved to another field while the request was pending');
  const createCalls = calls.filter(call => call.method === 'POST');
  assert.deepEqual(createCalls[0], {
    method: 'POST',
    path: '/admin/api/v2/schedules',
    payload: {
      name: 'Nightly availability', action: 'availability_check', schedule_type: 'interval',
      timezone: 'Europe/Warsaw', interval_minutes: 60, video_ids: ['video-1', 'video-2'],
    },
  });
  assert.equal(elements.schedulesFeedback.textContent, 'Schedule created. Your newer draft was kept.');
  assert.equal(elements.scheduleCreateForm.context.elements.scheduleName.value, 'Weekly audit draft');
  assert.equal(elements.scheduleAction.value, 'consistency_audit');
  assert.equal(elements.scheduleType.value, 'daily');
  assert.equal(elements.scheduleDailyAt.value, '04:15');
  assert.equal(elements.scheduleInterval.value, '120');
  assert.equal(elements.scheduleVideoIds.value, 'draft-video');
  assert.equal(elements.scheduleVideosField.hidden, true, 'the conditional scope matches the newer action draft');

  elements.scheduleName.value = 'Retried backup';
  elements.scheduleAction.value = 'backup_create';
  elements.scheduleAction.listeners.change();
  elements.scheduleType.value = 'daily';
  elements.scheduleType.listeners.change();
  elements.scheduleDailyAt.value = '02:30';
  elements.scheduleCreate.focus();
  failNextPost = true;
  failNextGet = true;
  await submit({ preventDefault() {} });
  assert.equal(elements.scheduleCreate.disabled, false, 'a failed request must restore the submit button');
  assert.equal(elements.scheduleName.value, 'Retried backup', 'a failed request must preserve the entered name');
  assert.equal(elements.scheduleDailyAt.value, '02:30', 'a failed request must preserve the entered time');
  assert.equal(document.activeElement, elements.scheduleCreate,
    'a keyboard submission must return focus to the re-enabled create button after an error');
  assert.match(elements.schedulesFeedback.textContent, /Could not create schedule\. The runner rejected this schedule\./);

  await submit({ preventDefault() {} });
  const retry = calls.filter(call => call.method === 'POST').at(-1);
  assert.deepEqual(retry.payload, {
    name: 'Retried backup', action: 'backup_create', schedule_type: 'daily',
    timezone: 'Europe/Warsaw', daily_at: '02:30',
  });
  assert.equal(elements.schedulesFeedback.textContent,
    'Schedule created, but the schedule list could not be refreshed.',
    'a successful create must not be reported as a failed create when its follow-up list read fails');

  let refreshCalls = 0;
  context.window.AdminV2.getJson = async () => { refreshCalls += 1; return { items: [] }; };
  documentListeners['adminv2:navigate']({ detail: { route: 'jobs' } });
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
  context.location.hash = '#jobs?tab=schedules';
  documentListeners['adminv2:navigate']({ detail: { route: 'jobs' } });
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
  process.stdout.write('PASS: schedule history links, visible-tab polling, safe stale errors, form validation, and retry\n');
})().catch(error => { console.error(error); process.exitCode = 1; });
