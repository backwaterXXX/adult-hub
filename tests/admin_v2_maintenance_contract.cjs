const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor(tagName = 'div') { this.tagName = tagName; this.listeners = {}; this.disabled = false; this.children = []; this.href = ''; this._text = ''; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set textContent(value) { this._text = String(value); this.children = []; }
  addEventListener(type, callback) { this.listeners[type] = callback; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this._text = ''; this.children = [...children]; }
}

(async () => {
  const ids = ['maintenanceFeedback', 'triggerFullSync', 'clearResolverCache',
    'refreshJellyfinLibrary', 'refreshJellyfinCollections'];
  const elements = Object.fromEntries(ids.map(id => [id, new Element()]));
  const calls = [];
  let confirmation = true;
  let failurePath = '';
  let holdNext = false;
  let releaseNext;
  const context = {
    document: {
      querySelector: selector => elements[selector.slice(1)],
      createTextNode: text => ({ textContent: text }),
      createElement: tag => new Element(tag),
    },
    window: {
      confirm: () => confirmation,
      AdminV2: {
        async postJson(path, payload) {
          calls.push({ path, payload });
          if (path === failurePath) throw new Error('The remote service rejected the request.');
          if (holdNext) {
            holdNext = false;
            await new Promise(resolve => { releaseNext = resolve; });
          }
          if (path.endsWith('/cache/clear')) return { ok: true, cleared: 4 };
          if (path.endsWith('/collections/refresh')) return { ok: true, job_id: 'job-1' };
          if (path.endsWith('/sync')) return { ok: true, job_id: 'job-sync' };
          return { ok: true, requested: true };
        },
      },
    },
  };
  vm.runInNewContext(fs.readFileSync('resolver/static/admin-v2/maintenance.js', 'utf8'), context);
  await elements.triggerFullSync.listeners.click();
  assert.equal(elements.triggerFullSync.disabled, false);
  assert.match(elements.maintenanceFeedback.textContent, /Full sync queued as job job-sync/);
  assert.equal(elements.maintenanceFeedback.children[2].href, '/admin/v2/?job_id=job-sync#jobs');
  assert.equal(elements.maintenanceFeedback.children[2].textContent, 'Open task job-sync');
  await elements.clearResolverCache.listeners.click();
  assert.match(elements.maintenanceFeedback.textContent, /Resolver cache cleared \(4 entries\)/);
  await elements.refreshJellyfinLibrary.listeners.click();
  assert.match(elements.maintenanceFeedback.textContent, /Import completion is not confirmed/);
  await elements.refreshJellyfinCollections.listeners.click();
  assert.deepEqual(calls.map(call => call.path), [
    '/admin/api/v2/maintenance/sync',
    '/admin/api/v2/maintenance/cache/clear',
    '/admin/api/v2/maintenance/jellyfin/refresh',
    '/admin/api/v2/maintenance/collections/refresh',
  ]);
  assert.equal(JSON.stringify(calls[0].payload), JSON.stringify({ source: null, sync_mode: 'scheduled' }));
  assert.match(elements.maintenanceFeedback.textContent, /Collections refresh queued as job job-1/);
  assert.equal(elements.maintenanceFeedback.children[2].href, '/admin/v2/?job_id=job-1#jobs');

  const beforeCancel = calls.length;
  confirmation = false;
  await elements.triggerFullSync.listeners.click();
  await elements.clearResolverCache.listeners.click();
  await elements.refreshJellyfinLibrary.listeners.click();
  await elements.refreshJellyfinCollections.listeners.click();
  assert.equal(calls.length, beforeCancel, 'cancelled confirmations must not send maintenance requests');

  confirmation = true;
  failurePath = '/admin/api/v2/maintenance/jellyfin/refresh';
  await elements.refreshJellyfinLibrary.listeners.click();
  assert.match(elements.maintenanceFeedback.textContent, /Maintenance action failed\. The remote service rejected the request\./);
  assert.equal(elements.refreshJellyfinLibrary.disabled, false, 'a failed action must restore its button');

  failurePath = '';
  holdNext = true;
  const pending = elements.triggerFullSync.listeners.click();
  assert.equal(elements.triggerFullSync.disabled, true, 'the action button stays disabled while the request is pending');
  assert.equal(elements.maintenanceFeedback.textContent, 'Sending request…');
  releaseNext();
  await pending;
  assert.equal(elements.triggerFullSync.disabled, false);
  process.stdout.write('PASS: Maintenance reports queued vs requested work and handles confirmation, pending, and error states\n');
})().catch(error => { console.error(error); process.exitCode = 1; });
