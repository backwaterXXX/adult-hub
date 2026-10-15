const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor(tag = 'div') { this.tagName = tag; this.children = []; this.listeners = {}; this.attributes = {}; this.disabled = false; }
  addEventListener(type, callback) { this.listeners[type] = callback; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  setAttribute(name, value) { this.attributes[name] = value; }
  scrollIntoView() {}
  focus() { this.focused = true; }
  querySelector(selector) {
    const match = selector.match(/^\[aria-label="(.+)"\]$/);
    if (!match) return null;
    return this.children.find(child => child.attributes?.['aria-label'] === match[1]) || null;
  }
}

function descendants(root, predicate) {
  return [
    ...(predicate(root) ? [root] : []),
    ...(root.children || []).flatMap(child => descendants(child, predicate)),
  ];
}

(async () => {
  const ids = ['auditFeedback', 'auditActionFeedback', 'auditRows', 'auditFindings', 'auditRun', 'auditRefresh',
    'backupFeedback', 'backupRows', 'backupRefresh', 'backupCreate'];
  const elements = Object.fromEntries(ids.map(id => [id, new Element(id.includes('Run') || id.includes('Refresh') || id.includes('Create') ? 'button' : 'div')]));
  const calls = [];
  const findingRequests = new Map();
  const backupRequests = [];
  let deferFindings = false;
  let deferBackups = false;
  let inventoryAvailable = true;
  let restoreTestAvailable = false;
  let navigation;
  const document = {
    querySelector: selector => elements[selector.slice(1)],
    createElement: tag => new Element(tag),
    addEventListener: (type, callback) => { if (type === 'adminv2:navigate') navigation = callback; },
  };
  const findings = Array.from({ length: 51 }, (_, index) => ({ id: index + 1, kind: 'missing_stream', status: 'open',
    video_id: `video-${index + 1}`, evidence: 'Stream file is missing.', checked_at: '2026-10-05T00:00:00Z' }));
  findings[0] = { ...findings[0], kind: 'nfo_metadata_mismatch',
    evidence: 'NFO metadata differs from current effective catalog metadata (fields: plot, tags).' };
  const posts = [];
  const confirmations = [];
  let confirmResult = true;
  const window = { confirm(message) { confirmations.push(message); return confirmResult; }, AdminV2: {
    async getJson(path) {
      calls.push(path);
      if (path === '/admin/api/v2/audits?limit=20&offset=0') return { ok: true, total: 1, items: [
        { id: 'audit-1', state: 'done', finding_count: 51, created_at: '2026-10-05T00:00:00Z', checked: 51, catalog_total: 60 },
        { id: 'audit-2', state: 'done', finding_count: 1, created_at: '2026-10-06T00:00:00Z', checked: 1, catalog_total: 1 },
      ] };
      if (path === '/admin/api/v2/backups' && deferBackups) return new Promise((resolve, reject) => backupRequests.push({ resolve, reject }));
      if (path === '/admin/api/v2/backups') return { ok: true, inventory_available: inventoryAvailable, count: 120, total_bytes: 2097152,
        latest: { name: 'adulthub-latest.dump.gz' }, items: [
          { name: 'adulthub-latest.dump.gz', backup_type: 'postgres_custom_dump', size_bytes: 1048576,
            modified_at: 1791367200, checksum_status: 'valid', scope: 'PostgreSQL database only' },
          { name: 'adult_hub-old.db', backup_type: 'legacy_database_snapshot', size_bytes: 1048576,
            modified_at: 1791367100, checksum_status: 'not_recorded', scope: 'Legacy database snapshot' },
        ], restore_test_available: restoreTestAvailable };
      if (/^\/admin\/api\/v2\/audits\/audit-[12]\?/.test(path)) {
        if (deferFindings) return new Promise((resolve, reject) => findingRequests.set(path, { resolve, reject }));
        if (path.startsWith('/admin/api/v2/audits/audit-2?')) return { ok: true, total: 1,
          audit: { id: 'audit-2' }, items: [{ id: 201, kind: 'invalid_nfo', status: 'open', video_id: 'video-newer', evidence: 'Newer audit finding.', checked_at: '2026-10-06T00:00:00Z' }] };
        const params = new URLSearchParams(path.split('?')[1]);
        const offset = Number(params.get('offset'));
        return { ok: true, total: 51, audit: { id: 'audit-1', checked: 51, catalog_total: 60, library_inventory_status: 'partial', library_inventory_entries: 50000 }, items: findings.slice(offset, offset + 50), offset, limit: 50 };
      }
      throw new Error(`Unexpected request ${path}`);
    },
    async postJson(path, payload) {
      posts.push({ path, payload });
      if (path === '/admin/api/v2/backups/create') return { ok: true, job_id: 'backup-job' };
      if (path === '/admin/api/v2/backups/restore-test') return { ok: true, job_id: 'restore-job' };
      if (path === '/admin/api/v2/audits/audit-1/repairs/preview') return { ok: true, plan_id: 'repair-plan', items: [
        { title: 'Catalog video', file_count: 1, findings: [{ finding_id: 1, kind: 'nfo_metadata_mismatch' }] },
      ] };
      if (path === '/admin/api/v2/operations/execute') return { ok: true, jobs: [{ job_id: 'repair-job' }] };
      return { ok: true };
    },
  } };
  const context = { window, document, location: { hash: '' }, Date, URLSearchParams, encodeURIComponent, console,
    setInterval: () => 1, clearInterval: () => {} };
  vm.runInNewContext(fs.readFileSync('resolver/static/admin-v2/audits.js', 'utf8'), context);
  navigation({ detail: { route: 'quality', tab: 'issues' } });
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(descendants(elements.auditRows, node => node.textContent?.includes('51 of 60 catalog entries checked')).length > 0,
    'audit history distinguishes checked catalog items from the catalog total');
  navigation({ detail: { route: 'system', systemTab: 'backups' } });
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
  assert.match(elements.backupFeedback.textContent, /120 backups found · 2\.0 MB total · showing the newest 2/);
  assert.ok(elements.backupRows.children[0].children[0].children.some(node => node.textContent === 'Latest snapshot'));
  const restoreReason = descendants(elements.backupRows, node => node.className === 'restore-test-reason')[0];
  assert.equal(restoreReason?.textContent,
    'Restore test unavailable: configure a dedicated disposable database.',
    'the disabled restore action must explain its missing prerequisite beside the backup');
  const restoreButton = descendants(elements.backupRows, node => node.tagName === 'button' && node.textContent === 'Test restore')[0];
  assert.equal(restoreButton?.disabled, true, 'restore remains disabled when no dedicated target is configured');
  confirmResult = false;
  await elements.backupCreate.listeners.click();
  assert.equal(posts.length, 0, 'cancelling a backup must not send a request');
  assert.equal(elements.backupCreate.disabled, false, 'cancelling leaves the control available');
  confirmResult = true;
  await elements.backupCreate.listeners.click();
  assert.equal(JSON.stringify(posts[0]), JSON.stringify({ path: '/admin/api/v2/backups/create', payload: {} }));
  assert.match(confirmations.at(-1), /does not include media files, artwork, or application settings/);
  assert.match(elements.backupFeedback.textContent, /Database backup queued \(backup-job\).*Check Jobs for completion/);
  inventoryAvailable = false;
  await elements.backupRefresh.listeners.click();
  assert.match(elements.backupFeedback.textContent, /No backup freshness conclusion is available/);
  assert.match(elements.backupRows.children[0].textContent, /Backup inventory is unavailable/);
  inventoryAvailable = true;
  deferBackups = true;
  const oldBackupLoad = elements.backupRefresh.listeners.click();
  const latestBackupLoad = elements.backupRefresh.listeners.click();
  backupRequests[1].resolve({ ok: true, inventory_available: true, count: 0, total_bytes: 0, items: [], restore_test_available: false });
  await latestBackupLoad;
  backupRequests[0].resolve({ ok: true, inventory_available: true, count: 1, total_bytes: 1048576,
    items: [{ name: 'stale-backup.dump', backup_type: 'postgres_custom_dump', size_bytes: 1048576,
      modified_at: 1791367200, checksum_status: 'valid', scope: 'PostgreSQL database only' }], restore_test_available: false });
  await oldBackupLoad;
  assert.match(elements.backupFeedback.textContent, /^0 backups found/,
    'a delayed older backup inventory must not replace the latest refresh');
  assert.equal(elements.backupRows.children.length, 1);
  assert.match(elements.backupRows.children[0].textContent, /No database backups were found/);
  deferBackups = false;
  restoreTestAvailable = true;
  await elements.backupRefresh.listeners.click();
  const availableRestore = descendants(elements.backupRows, node => node.tagName === 'button' && node.textContent === 'Test restore')[0];
  confirmResult = false;
  const beforeRestoreCancel = posts.length;
  await availableRestore.listeners.click();
  assert.equal(posts.length, beforeRestoreCancel, 'a cancelled isolated restore test is not queued');
  assert.match(confirmations.at(-1), /adulthub-latest.dump.gz.*temporary disposable database/);
  confirmResult = true;
  await availableRestore.listeners.click();
  assert.equal(JSON.stringify(posts.at(-1)), JSON.stringify({ path: '/admin/api/v2/backups/restore-test',
    payload: { filename: 'adulthub-latest.dump.gz' } }));
  assert.match(elements.backupFeedback.textContent, /Restore test queued \(restore-job\).*does not change the source database/);
  posts.length = 0;
  const view = elements.auditRows.children[0].children[1];
  await view.listeners.click();
  assert.ok(elements.auditFindings.children.some(node => node.textContent?.includes('51 of 60 catalog entries checked')),
    'the audit detail reports catalog coverage separately from filesystem inventory');
  assert.ok(elements.auditFindings.children.some(node => node.textContent?.includes('Some orphaned files may not appear in this report.')));
  assert.ok(elements.auditFindings.children.some(node => node.children?.some(child =>
    child.textContent === 'Preview local file repair')));
  assert.ok(elements.auditFindings.children.some(node => node.children?.some(child =>
    child.textContent?.includes('Restore selected registered stream files') &&
    child.textContent?.includes('Repair up to 25 findings per request'))));
  const repairControls = descendants(elements.auditFindings, node => node.tagName === 'input');
  assert.ok(repairControls.length > 0, 'the fixture renders repair selection controls');
  assert.ok(repairControls.every(node => /^Select .+ (repair|reimport)$/.test(node.attributes['aria-label'] || '')),
    'every dynamically rendered audit selection control has an accessible name');
  assert.ok(descendants(elements.auditFindings, node => node.tagName === 'p').some(node =>
    node.textContent.includes('NFO metadata differs from current effective catalog metadata (fields: plot, tags).')),
  'NFO mismatch findings show the specific catalog fields that differ');
  const nfoCheck = repairControls.find(node => node.attributes['aria-label'] === 'Select nfo metadata mismatch repair');
  assert.ok(nfoCheck, 'NFO metadata mismatches are selectable for local repair');
  nfoCheck.checked = true;
  nfoCheck.listeners.change();
  const previewRepair = descendants(elements.auditFindings, node => node.tagName === 'button' && node.textContent === 'Preview local file repair')[0];
  await previewRepair.listeners.click();
  assert.equal(JSON.stringify(posts.slice(0, 2)), JSON.stringify([
    { path: '/admin/api/v2/audits/audit-1/repairs/preview', payload: { finding_ids: [1] } },
    { path: '/admin/api/v2/operations/execute', payload: { plan_id: 'repair-plan' } },
  ]), 'NFO repair must use the selected-finding preview and execute the returned operation plan');
  assert.match(confirmations.at(-1), /Catalog video.*nfo metadata mismatch.*1 registered file/s,
    'repair confirmation must summarize the selected NFO issue and affected catalog item');
  assert.match(confirmations.at(-1), /fields: plot, tags/,
    'repair confirmation must retain the safe field-level NFO mismatch evidence');
  assert.match(elements.auditActionFeedback.textContent, /Local file repair queued \(repair-job\)/,
    'the UI distinguishes queue acceptance from completed repair');
  const nav = elements.auditFindings.children.find(node => node.children?.some(child => child.attributes?.['aria-label'] === 'Next findings page'));
  assert.ok(nav);
  assert.equal(nav.children[0].disabled, true);
  assert.equal(nav.children[1].textContent, 'Showing 1–50 of 51 findings');
  await nav.children[2].listeners.click();
  assert.equal(calls.at(-1), '/admin/api/v2/audits/audit-1?limit=50&offset=50');
  const nextNav = elements.auditFindings.children.find(node => node.children?.some(child => child.attributes?.['aria-label'] === 'Next findings page'));
  assert.equal(nextNav.children[1].textContent, 'Showing 51–51 of 51 findings');
  assert.equal(nextNav.children[2].disabled, true);
  assert.equal(nextNav.children[2].focused, true, 'focus returns to the next-page control after page navigation');
  await nextNav.children[0].listeners.click();
  assert.equal(calls.at(-1), '/admin/api/v2/audits/audit-1?limit=50&offset=0');

  const views = elements.auditRows.children.map(row => row.children[1]);
  deferFindings = true;
  const olderRequest = views[0].listeners.click();
  const olderPath = '/admin/api/v2/audits/audit-1?limit=50&offset=0';
  const newerRequest = views[1].listeners.click();
  const newerPath = '/admin/api/v2/audits/audit-2?limit=50&offset=0';
  const newerResult = { ok: true, total: 1, audit: { id: 'audit-2' }, items: [
    { id: 201, kind: 'invalid_nfo', status: 'open', video_id: 'video-newer', evidence: 'Newer audit finding.', checked_at: '2026-10-06T00:00:00Z' },
  ] };
  findingRequests.get(newerPath).resolve(newerResult);
  await newerRequest;
  assert.ok(descendants(elements.auditFindings, node => node.textContent?.includes('Newer audit finding.')).length > 0,
    'the most recently selected audit renders its findings');
  findingRequests.get(olderPath).resolve({ ok: true, total: 1, audit: { id: 'audit-1' }, items: [
    { id: 101, kind: 'missing_nfo', status: 'open', video_id: 'video-older', evidence: 'Older audit finding.', checked_at: '2026-10-05T00:00:00Z' },
  ] });
  await olderRequest;
  assert.ok(descendants(elements.auditFindings, node => node.textContent?.includes('Newer audit finding.')).length > 0,
    'a late response from an older selection cannot overwrite the current findings');
  assert.equal(descendants(elements.auditFindings, node => node.textContent?.includes('Older audit finding.')).length, 0);

  const failedOlderRequest = views[0].listeners.click();
  const failedOlderPath = olderPath;
  const currentRequest = views[1].listeners.click();
  const currentPath = newerPath;
  findingRequests.get(currentPath).resolve(newerResult);
  await currentRequest;
  findingRequests.get(failedOlderPath).reject(new Error('stale request failed'));
  await failedOlderRequest;
  assert.ok(descendants(elements.auditFindings, node => node.textContent?.includes('Newer audit finding.')).length > 0,
    'a late error from an older selection cannot replace the current findings');
  assert.equal(descendants(elements.auditFindings, node => node.textContent?.includes('stale request failed')).length, 0);
  process.stdout.write('PASS: audit findings page both directions, boundaries, live range, and focus\n');
})().catch(error => { console.error(error); process.exitCode = 1; });
