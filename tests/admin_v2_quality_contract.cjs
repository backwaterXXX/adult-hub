const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor(tag = 'div') {
    this.tagName = tag; this.children = []; this.listeners = {}; this.attributes = {};
    this.dataset = {}; this.hidden = false; this.disabled = false; this.checked = false;
    this.value = ''; this.textContent = ''; this.className = '';
  }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  querySelectorAll(selector) {
    const all = descendants(this, () => true).slice(1);
    if (selector === 'input,textarea') return all.filter(node => ['input', 'textarea'].includes(node.tagName));
    const match = selector.match(/^\[data-proposal-field="([a-z_]+)"\]$/);
    return match ? all.filter(node => node.dataset.proposalField === match[1]) : [];
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  focus() { this.focused = true; }
  setCustomValidity(value) { this.validationMessage = value; }
  reportValidity() { this.validityReported = true; return false; }
}

function descendants(root, predicate) {
  return [...(predicate(root) ? [root] : []), ...(root.children || []).flatMap(child => descendants(child, predicate))];
}

(async () => {
  const ids = ['qualityRefresh', 'qualityRunAudit', 'qualityAuditMode', 'qualityFeedback', 'qualitySummary', 'qualityTrend', 'qualityHistory', 'qualityKind',
    'qualityField', 'qualityStatus', 'qualityFilters', 'qualityActionFeedback', 'qualityRows', 'qualityPagination',
    'metadataProposalsRefresh', 'metadataProposalsFeedback', 'metadataProposalsRows', 'metadataProposalsPagination'];
  const elements = Object.fromEntries(ids.map(id => [id, new Element(id === 'qualityFilters' ? 'form' : 'div')]));
  elements.qualityKind.value = '';
  elements.qualityAuditMode.value = 'full';
  elements.qualityStatus.value = 'open';
  const gets = [], posts = [], confirmations = [], historyChanges = [];
  let currentUrl = new URL('http://localhost/admin/v2/#quality');
  const location = {
    get href() { return currentUrl.href; },
    get hash() { return currentUrl.hash; },
    get search() { return currentUrl.search; },
  };
  const history = {
    pushState(_state, _title, url) { currentUrl = new URL(url, currentUrl); historyChanges.push('push'); },
    replaceState(_state, _title, url) { currentUrl = new URL(url, currentUrl); historyChanges.push('replace'); },
  };
  let ready = false;
  let proposalPending = false;
  let noReadyReason = 'audit_in_progress';
  const document = {
    querySelector(selector) { return selector.startsWith('#') ? elements[selector.slice(1)] || null : null; },
    querySelectorAll(selector) {
      if (selector === '#qualityRows input[type="checkbox"]') return descendants(elements.qualityRows, node => node.tagName === 'input');
      return [];
    },
    createElement(tag) { return new Element(tag); },
    createTextNode(text) { const node = new Element('#text'); node.textContent = text; return node; },
    addEventListener(name, callback) { this.listeners ||= {}; this.listeners[name] = callback; },
    dispatchEvent(event) { this.dispatched ||= []; this.dispatched.push(event.type); },
  };
  const window = { AdminV2: {
    async getJson(path) {
      gets.push(path);
      if (path.endsWith('/quality/summary')) return ready ? {
        status: 'ready', total: 2, current_audit_id: 'audit-current', current_audit_state: 'done',
        latest_audit: { id: 'audit-current', created_at: '2026-10-08T10:00:00Z', checked: 2, total: 2,
          library_inventory_status: 'partial', library_inventory_entries: 50000 },
        counts: { missing_metadata: 1, missing_nfo: 0, invalid_nfo: 0, nfo_metadata_mismatch: 1, missing_artwork: 0 },
        history: [
          { id: 'audit-current', created_at: '2026-10-08T10:00:00Z', checked: 2, total: 2, open_total: 2,
            library_inventory_status: 'partial', library_inventory_entries: 50000,
            counts: { missing_metadata: 1, missing_nfo: 0, invalid_nfo: 0, nfo_metadata_mismatch: 1, missing_artwork: 0 } },
          { id: 'audit-previous', created_at: '2026-10-01T10:00:00Z', checked: 2, total: 2, open_total: 4,
            counts: { missing_metadata: 1, missing_nfo: 2, invalid_nfo: 0, nfo_metadata_mismatch: 1, missing_artwork: 0 } },
        ],
      } : { status: 'no_complete_audit', status_reason: noReadyReason,
        current_audit_state: noReadyReason === 'audit_in_progress' ? 'processing' : noReadyReason ? 'done' : 'queued', counts: {}, total: 0 };
      if (path.startsWith('/admin/api/v2/quality/items?')) {
        const acknowledgedView = path.includes('status=acknowledged');
        const params = new URLSearchParams(path.split('?')[1]);
        return {
        audit_id: 'audit-current', total: 51, limit: 50, offset: Number(params.get('offset') || 0), items: [{
          video_id: 'telegram:1:item', title: 'Example film', finding_count: 2, findings: [
            { id: 3, kind: 'nfo_metadata_mismatch', status: acknowledgedView ? 'acknowledged' : 'open',
              acknowledgement: acknowledgedView ? { id: 7, note: 'Expected exception for this catalog item.' } : null,
              evidence: 'NFO metadata differs.', details: {
              diffs: [{ field: 'Title', current: 'Old title', expected: 'Example film' }], fields: [],
            } },
            { id: 4, kind: 'missing_metadata', status: 'open', evidence: 'Catalog fields are missing.', details: {
              fields: ['plot', 'performers'], diffs: [],
            } },
          ],
        }],
        };
      }
      if (path.startsWith('/admin/api/v2/quality/proposals?')) return proposalPending ? {
        items: [{ id: 12, video_id: 'telegram:1:item', title: 'Example film', source: 'studio:algolia',
          source_url: 'https://catalog.example/item/1', confidence: 0.92, revision: 0, stale: false,
          current: { plot: 'Current description' }, changes: { plot: 'Suggested description' },
          created_at: '2026-10-12T10:00:00Z' }], total: 1, limit: 30, offset: 0,
      } : { items: [], total: 0, limit: 30, offset: 0 };
      throw new Error(`Unexpected GET ${path}`);
    },
    async postJson(path, payload) {
      posts.push({ path, payload });
      if (path.endsWith('/audits/execute')) return { ok: true, job_id: 'audit-new', job_ids: ['audit-meta', 'audit-files'] };
      if (path.endsWith('/repairs/preview')) return { ok: true, plan_id: 'plan-1', items: [{
        title: 'Example film', file_count: 1, findings: [{ kind: 'nfo_metadata_mismatch' }],
      }] };
      if (path.endsWith('/operations/execute')) {
        if (payload.plan_id === 'metadata-plan') proposalPending = false;
        return { ok: true, job_id: payload.plan_id === 'metadata-plan' ? 'metadata-job' : 'repair-job' };
      }
      if (path.endsWith('/operations/preview')) return { ok: true, plan_id: 'metadata-plan', fields: {
        plot: { before: 'Current description', after: 'Edited suggestion' },
      } };
      if (path.endsWith('/quality/acknowledge')) return { ok: true, acknowledgement_id: 7 };
      if (path.endsWith('/quality/reopen')) return { ok: true, acknowledgement_id: payload.acknowledgement_id };
      throw new Error(`Unexpected POST ${path}`);
    },
  } };
  const context = { document, window, location, history, URL, CustomEvent: class { constructor(type) { this.type = type; } },
    confirm: message => { confirmations.push(message); return true; },
    URLSearchParams, console, setTimeout, clearTimeout };
  vm.runInNewContext(fs.readFileSync('resolver/static/admin-v2/quality.js', 'utf8'), context);
  await new Promise(resolve => setImmediate(resolve));
  assert.match(elements.qualityFeedback.textContent, /No complete catalog audit is available/);
  assert.match(elements.qualityFeedback.textContent, /still in progress; its partial results are not shown/);
  assert.match(elements.qualityFeedback.textContent, /Refresh later to check its result/);
  await elements.qualityRunAudit.listeners.click();
  assert.equal(JSON.stringify(posts[0]), JSON.stringify({ path: '/admin/api/v2/audits/execute', payload: { mode: 'full' } }));
  assert.match(confirmations[0], /Queue a full audit/);
  confirmations.length = 0;
  assert.match(elements.qualityFeedback.textContent, /audit-meta, audit-files/);
  assert.ok(document.dispatched.includes('adminv2:audits-updated'));
  elements.qualityAuditMode.value = 'metadata_quality';
  await elements.qualityRunAudit.listeners.click();
  assert.equal(JSON.stringify(posts[1].payload), JSON.stringify({ mode: 'metadata_quality' }));
  assert.match(confirmations.at(-1), /does not inspect local files or contact Jellyfin/);
  elements.qualityAuditMode.value = 'file_consistency';
  await elements.qualityRunAudit.listeners.click();
  assert.equal(JSON.stringify(posts[2].payload), JSON.stringify({ mode: 'file_consistency' }));
  assert.match(confirmations.at(-1), /orphan inventory/);

  noReadyReason = 'legacy_missing_catalog_total';
  await elements.qualityRefresh.listeners.click();
  assert.match(elements.qualityFeedback.textContent, /previous audit completed, but it did not record a verifiable catalog total/);
  assert.match(elements.qualityFeedback.textContent, /Run a new audit to establish a complete baseline/);

  noReadyReason = 'audit_not_complete';
  await elements.qualityRefresh.listeners.click();
  assert.match(elements.qualityFeedback.textContent, /did not check every catalog item; its partial results are not shown/);

  noReadyReason = 'no_audit';
  await elements.qualityRefresh.listeners.click();
  assert.match(elements.qualityFeedback.textContent, /No audit has been run yet/);
  assert.match(elements.qualityFeedback.textContent, /Run an audit to establish a complete baseline/);

  ready = true;
  await elements.qualityRefresh.listeners.click();
  assert.match(elements.qualityFeedback.textContent, /Latest complete catalog audit/);
  assert.match(elements.qualityFeedback.textContent, /2 of 2 catalog entries checked/);
  assert.match(elements.qualityFeedback.textContent, /catalog check is complete, but the managed file inventory was partial/);
  assert.match(elements.qualityFeedback.textContent, /orphan-file findings are excluded from Quality counts and may be incomplete/i);
  assert.ok(descendants(elements.qualityRows, node => node.textContent.includes('Example film')).length);
  assert.ok(descendants(elements.qualityRows, node => node.textContent.includes('Old title')).length);
  assert.ok(descendants(elements.qualityTrend, node => node.textContent.includes('decreased by 2')).length);
  assert.ok(descendants(elements.qualityTrend, node => node.textContent.includes('-2')).length);
  assert.ok(descendants(elements.qualityHistory, node => node.textContent.includes('4 open findings')).length);
  assert.ok(descendants(elements.qualityHistory, node => node.textContent.includes('50,000 entries were scanned')).length);
  const link = descendants(elements.qualityRows, node => node.tagName === 'a')[0];
  assert.equal(link.href, '#film/telegram%3A1%3Aitem');
  const checkbox = descendants(elements.qualityRows, node => node.tagName === 'input')[0];
  assert.match(checkbox.attributes['aria-label'], /NFO metadata mismatch/);
  checkbox.checked = true;
  checkbox.listeners.change();
  const repair = descendants(elements.qualityRows, node => node.id === 'qualityRepair')[0];
  await repair.listeners.click();
  assert.equal(posts[3].path, '/admin/api/v2/audits/audit-current/repairs/preview');
  assert.equal(JSON.stringify(posts[3].payload), JSON.stringify({ finding_ids: [3] }));
  assert.equal(posts[4].path, '/admin/api/v2/operations/execute');
  assert.equal(JSON.stringify(posts[4].payload), JSON.stringify({ plan_id: 'plan-1' }));
  assert.match(confirmations[2], /Existing artwork will not be overwritten/);
  assert.match(elements.qualityActionFeedback.textContent, /repair-job.*Check Tasks/);

  const openTriage = descendants(elements.qualityRows, node => node.textContent === 'Acknowledge exception')[0];
  await openTriage.listeners.click();
  const note = descendants(elements.qualityRows, node => node.tagName === 'textarea')[0];
  note.value = 'Expected exception for this catalog item.';
  const acknowledge = descendants(elements.qualityRows, node => node.textContent === 'Save accepted exception')[0];
  await acknowledge.listeners.click();
  const acknowledgementPost = posts.find(item => item.path.endsWith('/quality/acknowledge'));
  assert.deepEqual(JSON.parse(JSON.stringify(acknowledgementPost.payload)), {
    finding_id: 3, video_id: 'telegram:1:item', kind: 'nfo_metadata_mismatch',
    note: 'Expected exception for this catalog item.',
  });
  assert.match(elements.qualityActionFeedback.textContent, /acknowledged/);

  elements.qualityStatus.value = 'acknowledged';
  await elements.qualityStatus.listeners.change();
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(descendants(elements.qualityRows, node => node.textContent === 'Reopen finding').length);
  const reopen = descendants(elements.qualityRows, node => node.textContent === 'Reopen finding')[0];
  await reopen.listeners.click();
  const reopenPost = posts.find(item => item.path.endsWith('/quality/reopen'));
  assert.equal(reopenPost.payload.acknowledgement_id, 7);

  elements.qualityKind.value = 'nfo_metadata_mismatch';
  await elements.qualityKind.listeners.change();
  assert.ok(gets.some(path => path.includes('kind=nfo_metadata_mismatch')));
  assert.equal(currentUrl.searchParams.get('quality_kind'), 'nfo_metadata_mismatch');
  assert.equal(historyChanges.at(-1), 'push');
  const nextPage = descendants(elements.qualityPagination, node => node.textContent === 'Next →')[0];
  await nextPage.listeners.click();
  assert.equal(currentUrl.searchParams.get('quality_offset'), '50');
  assert.ok(gets.some(path => path.includes('offset=50')));
  assert.equal(currentUrl.hash, '#quality');

  currentUrl = new URL('http://localhost/admin/v2/?keep=1&quality_kind=missing_nfo&quality_status=acknowledged&quality_offset=50#quality');
  await document.listeners['adminv2:navigate']({ detail: { route: 'quality' } });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(elements.qualityKind.value, 'missing_nfo');
  assert.equal(elements.qualityStatus.value, 'acknowledged');
  assert.ok(gets.at(-1).includes('kind=missing_nfo'));
  assert.ok(gets.at(-1).includes('status=acknowledged'));
  assert.ok(gets.at(-1).includes('offset=50'));
  assert.equal(currentUrl.searchParams.get('keep'), '1');

  currentUrl = new URL('http://localhost/admin/v2/?keep=2&quality_kind=invalid&quality_status=unknown&quality_offset=-1#quality');
  await document.listeners['adminv2:navigate']({ detail: { route: 'quality' } });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(elements.qualityKind.value, '');
  assert.equal(elements.qualityField.value, '');
  assert.equal(elements.qualityStatus.value, 'open');
  assert.equal(currentUrl.searchParams.has('quality_kind'), false);
  assert.equal(currentUrl.searchParams.has('quality_field'), false);
  assert.equal(currentUrl.searchParams.has('quality_status'), false);
  assert.equal(currentUrl.searchParams.has('quality_offset'), false);
  assert.equal(currentUrl.searchParams.get('keep'), '2');

  currentUrl = new URL('http://localhost/admin/v2/?keep=1#quality');
  await document.listeners['adminv2:navigate']({ detail: { route: 'quality' } });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(elements.qualityKind.value, '');
  assert.equal(elements.qualityStatus.value, 'open');
  assert.equal(currentUrl.searchParams.get('keep'), '1');
  assert.equal(currentUrl.searchParams.has('quality_offset'), false);
  assert.ok(gets.at(-1).includes('status=open&limit=50&offset=0') || gets.at(-1).includes('limit=50&offset=0&status=open'));
  currentUrl = new URL('http://localhost/admin/v2/#quality?tab=enrichment');
  const getsBeforeProposalTab = gets.length;
  await document.listeners['adminv2:navigate']({ detail: { route: 'quality', tab: 'enrichment' } });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(gets.length, getsBeforeProposalTab + 1,
    'opening Enrichment loads proposals without refetching the Issues audit');
  assert.match(gets.at(-1), /quality\/proposals\?/);
  proposalPending = true;
  await elements.metadataProposalsRefresh.listeners.click();
  await new Promise(resolve => setImmediate(resolve));
  const proposalInput = descendants(elements.metadataProposalsRows, node => node.dataset.proposalField === 'plot')[0];
  assert.equal(proposalInput.value, 'Suggested description');
  proposalInput.value = 'Edited suggestion';
  proposalInput.listeners.input();
  const previewProposal = descendants(elements.metadataProposalsRows, node => node.textContent === 'Preview suggestion')[0];
  await previewProposal.listeners.click();
  const proposalPreview = posts.find(item => item.path.endsWith('/operations/preview'));
  assert.equal(proposalPreview.payload.proposal_id, 12);
  assert.equal(proposalPreview.payload.changes.plot, 'Edited suggestion');
  const approveProposal = descendants(elements.metadataProposalsRows,
    node => node.textContent === 'Queue approved metadata update')[0];
  await approveProposal.listeners.click();
  assert.ok(posts.some(item => item.path.endsWith('/operations/execute') && item.payload.plan_id === 'metadata-plan'));
  assert.match(elements.metadataProposalsFeedback.textContent, /0 suggestions waiting for review/);
  process.stdout.write('PASS: Quality findings, field filters, metadata proposal edit/preview/approval, repair, triage, and history\n');
})().catch(error => { console.error(error); process.exitCode = 1; });
