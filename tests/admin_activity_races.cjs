const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor(tagName = 'div') { this.tagName = tagName.toUpperCase(); this.children = []; this.listeners = {}; this.hidden = false; this.disabled = false; this.textContent = ''; this.value = ''; }
  addEventListener(type, callback) { this.listeners[type] = callback; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
}
const deferred = () => { let resolve; const promise = new Promise(yes => { resolve = yes; }); return { promise, resolve }; };
const textOf = node => `${node.textContent || ''} ${(node.children || []).map(textOf).join(' ')}`;

(async () => {
  const ids = ['activityRows', 'activityFeedback', 'activityPageInfo', 'activityPagination', 'activityPrev', 'activityNext', 'activityRefresh',
    'activityChangeRows', 'activityChangeFeedback', 'activityChangePageInfo', 'activityChangePagination', 'activityChangePrev', 'activityChangeNext', 'activityChangeRefresh',
    'activityFilters', 'activityMethod', 'activityOutcome', 'activityArea', 'activityChangeFilters', 'activityChangeType', 'activityObjectType', 'activityChangeQuery'];
  const elements = Object.fromEntries(ids.map(id => [id, new Element()]));
  const listeners = {};
  const requests = { activity: [deferred(), deferred(), deferred()], changes: [deferred(), deferred(), deferred()] };
  const callCounts = { activity: 0, changes: 0 };
  const paths = [];
  const window = { AdminV2: { getJson(path) {
    paths.push(path);
    const key = path.includes('/changes?') ? 'changes' : 'activity';
    return requests[key][callCounts[key]++].promise;
  } } };
  const document = {
    querySelector: selector => elements[selector.slice(1)],
    createElement: tag => new Element(tag),
    addEventListener: (type, callback) => { listeners[type] = callback; },
  };
  const location = { hash: '#system?tab=activity', href: 'http://localhost/admin/v2/#system?tab=activity' };
  const history = { pushState(_state, _title, value) {
    const url = new URL(String(value), location.href); location.href = url.href; location.hash = url.hash;
  } };
  vm.runInNewContext(fs.readFileSync('resolver/static/admin-v2/activity.js', 'utf8'), {
    window, document, location, history, URL, URLSearchParams, Date, Intl, String, Number, Object, Array, Promise, queueMicrotask,
  });
  listeners['adminv2:navigate']({ detail: { route: 'system', systemTab: 'activity' } });
  const oldActivity = requests.activity[0].promise;
  const oldChanges = requests.changes[0].promise;
  const nextActivity = elements.activityNext.listeners.click();
  const nextChanges = elements.activityChangeNext.listeners.click();
  requests.activity[1].resolve({ items: [{ method: 'POST', route: '/current', created_at: '2026-10-07T10:00:00Z', outcome: 'accepted', status_code: 202 }], total: 41 });
  requests.changes[1].resolve({ items: [{ event_type: 'metadata_update', object_type: 'video', object_id: 'telegram:99:current-item',
    job_id: 'metadata-job', created_at: '2026-10-07T10:00:00Z', changes: { title: { before: 'Old', after: 'Current' } } }], total: 41 });
  await Promise.all([nextActivity, nextChanges]);
  requests.activity[0].resolve({ items: [{ method: 'GET', route: '/stale', created_at: '2026-10-07T09:00:00Z', outcome: 'completed', status_code: 200 }], total: 1 });
  requests.changes[0].resolve({ items: [{ event_type: 'metadata_update', object_id: 'stale-item', created_at: '2026-10-07T09:00:00Z', changes: { title: { before: 'Old', after: 'Stale' } } }], total: 1 });
  await Promise.all([oldActivity, oldChanges]);
  assert.match(textOf(elements.activityRows), /POST \/current/);
  assert.doesNotMatch(textOf(elements.activityRows), /GET \/stale/);
  assert.match(textOf(elements.activityChangeRows), /current-item/);
  const links = elements.activityChangeRows.children[0].children[0].children.filter(child => child.tagName === 'A');
  assert.equal(links[0].href, '#film/telegram%3A99%3Acurrent-item');
  assert.equal(links[1].href, '/admin/v2/?job_id=metadata-job#jobs');
  assert.doesNotMatch(textOf(elements.activityChangeRows), /stale-item/);
  assert.equal(elements.activityPageInfo.textContent, 'Page 2 of 3');
  assert.equal(elements.activityChangePageInfo.textContent, 'Page 2 of 3');
  elements.activityMethod.value = 'POST';
  elements.activityArea.value = 'quality';
  elements.activityFilters.listeners.change();
  assert.match(paths.at(-1), /method=POST/);
  assert.match(paths.at(-1), /area=quality/);
  assert.match(location.hash, /activity_method=POST/);
  requests.activity[2].resolve({ items: [], total: 0 });
  await new Promise(resolve => setImmediate(resolve));
  elements.activityChangeType.value = 'metadata_update';
  elements.activityObjectType.value = 'video';
  elements.activityChangeQuery.value = 'current-item';
  const applied = elements.activityChangeFilters.listeners.submit({ preventDefault() {} });
  assert.match(paths.at(-1), /event_type=metadata_update/);
  assert.match(paths.at(-1), /object_type=video/);
  assert.match(paths.at(-1), /q=current-item/);
  requests.changes[2].resolve({ items: [], total: 0 });
  await applied;
  process.stdout.write('PASS: rapid Admin activity pagination ignores stale list and change-history responses\n');
})().catch(error => { console.error(error); process.exitCode = 1; });
