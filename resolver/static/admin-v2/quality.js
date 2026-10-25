(() => {
  'use strict';
  const api = '/admin/api/v2/quality';
  const $ = selector => document.querySelector(selector);
  const labels = {
    missing_metadata: 'Missing catalog fields', missing_nfo: 'Missing NFO', invalid_nfo: 'Invalid NFO',
    nfo_metadata_mismatch: 'NFO metadata mismatch', missing_artwork: 'Missing artwork',
  };
  const statuses = new Set(['open', 'resolved', 'acknowledged', 'all']);
  const fieldLabels = { title: 'Title', performers: 'Performers', studio: 'Studio', plot: 'Description', thumbnail: 'Artwork' };
  const reviewFieldLabels = { title: 'Title', plot: 'Description', studio: 'Studio', performers: 'Performers', tags: 'Tags', date_released: 'Release date' };
  const state = { summary: null, offset: 0, total: 0, requestId: 0, loadSequence: 0, selected: new Set(), busy: false,
    auditNotice: '', auditStale: false, proposalOffset: 0, proposalTotal: 0, proposalRequestId: 0 };
  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = String(text);
    return node;
  };
  const when = value => value ? new Date(value).toLocaleString('en-US') : '—';
  const findingLabel = finding => labels[finding.kind] || 'Catalog issue';

  function readFiltersFromUrl() {
    const params = new URLSearchParams(location.search || '');
    const kind = params.get('quality_kind') || '';
    const field = params.get('quality_field') || '';
    const status = params.get('quality_status') || 'open';
    const offset = Number(params.get('quality_offset'));
    $('#qualityKind').value = Object.hasOwn(labels, kind) ? kind : '';
    $('#qualityField').value = ['title', 'performers', 'studio', 'plot', 'thumbnail'].includes(field) ? field : '';
    $('#qualityStatus').value = statuses.has(status) ? status : 'open';
    state.offset = Number.isSafeInteger(offset) && offset > 0 && offset <= 1000000 ? offset : 0;
    writeFiltersToUrl(true);
  }

  function writeFiltersToUrl(replace = false) {
    const url = new URL(location.href);
    for (const key of ['quality_kind', 'quality_field', 'quality_status', 'quality_offset']) url.searchParams.delete(key);
    const kind = $('#qualityKind').value;
    const status = $('#qualityStatus').value;
    const field = $('#qualityField').value;
    if (Object.hasOwn(labels, kind)) url.searchParams.set('quality_kind', kind);
    if (['title', 'performers', 'studio', 'plot', 'thumbnail'].includes(field)) url.searchParams.set('quality_field', field);
    if (statuses.has(status) && status !== 'open') url.searchParams.set('quality_status', status);
    if (Number.isSafeInteger(state.offset) && state.offset > 0) url.searchParams.set('quality_offset', String(state.offset));
    if (url.href === location.href) return;
    history[replace ? 'replaceState' : 'pushState']({}, '', url);
  }

  function setFeedback(text, kind = '') {
    const feedback = $('#qualityFeedback');
    feedback.textContent = text;
    feedback.dataset.state = kind;
  }

  function renderSummary(summary) {
    const host = $('#qualitySummary');
    host.replaceChildren();
    const coverage = summary.fingerprint_coverage;
    const overview = $('#qualityOverviewCards');
    const statusLine = $('#qualityOverviewStatus');
    overview.replaceChildren();
    if (summary.status !== 'ready') {
      statusLine.textContent = summary.current_audit_state === 'queued' || summary.current_audit_state === 'processing'
        ? 'An audit is running. The previous complete report remains available in audit details.'
        : 'No complete audit is available. Run a full audit to establish the current catalog status.';
    } else {
      const audit = summary.latest_audit;
      statusLine.textContent = `Last complete audit: ${when(audit.created_at)} · checked ${Number(audit.checked || 0).toLocaleString('en-US')} of ${Number(audit.total || 0).toLocaleString('en-US')} videos.`;
      const fileCount = ['missing_nfo', 'invalid_nfo', 'nfo_metadata_mismatch', 'missing_artwork']
        .reduce((total, kind) => total + Number(summary.counts?.[kind] || 0), 0);
      for (const [number, label, note, action] of [
        [fileCount, 'File and artwork findings', 'Missing or inconsistent local files', 'files'],
        [Number(summary.counts?.missing_metadata || 0), 'Optional metadata gaps', 'Missing descriptions and catalog fields', 'metadata'],
      ]) {
        const card = el('button', 'quality-stat quality-overview-stat', '');
        card.type = 'button';
        card.append(el('strong', '', number.toLocaleString('en-US')), el('span', '', label), el('small', '', note));
        card.addEventListener('click', () => {
          $('#qualityAdvanced').open = true;
          $('#qualityEnrichmentPane').hidden = true;
          $('#qualityIssuesPane').hidden = false;
          $('#qualityKind').value = action === 'metadata' ? 'missing_metadata' : '';
          $('#qualityField').value = '';
          $('#qualityStatus').value = 'open'; state.offset = 0; state.selected.clear(); loadItems();
          $('#qualityRows').scrollIntoView({ behavior: 'smooth', block: 'start' });
        });
        overview.append(card);
      }
    }
    if (coverage?.available) {
      const total = Number(coverage.total_videos) || 0;
      for (const [key, label, note] of [
        ['oshash', 'Videos with OSHASH', 'Exact file fingerprint coverage'],
        ['phash', 'Videos with PHASH', 'Computed for a subset of videos'],
      ]) {
        const count = Number(coverage[key]) || 0;
        const card = el('div', 'quality-stat quality-coverage-stat');
        card.setAttribute('aria-label', `${label}: ${count} of ${total}`);
        card.append(el('strong', '', `${count.toLocaleString('en-US')} / ${total.toLocaleString('en-US')}`),
          el('span', '', label), el('small', '', note));
        host.append(card);
      }
    }
    if (summary.status !== 'ready') return;
    for (const [kind, label] of Object.entries(labels)) {
      const button = el('button', 'quality-stat', '');
      button.type = 'button';
      button.setAttribute('aria-label', `${label}: ${Number(summary.counts?.[kind] || 0)}`);
      button.append(el('strong', '', Number(summary.counts?.[kind] || 0).toLocaleString('en-US')),
        el('span', '', label));
      button.addEventListener('click', () => {
        $('#qualityKind').value = kind;
        state.offset = 0;
        loadItems();
      });
      host.append(button);
    }
    for (const [field, label] of Object.entries(fieldLabels)) {
      const count = Number(summary.missing_fields?.[field] || 0);
      const button = el('button', 'quality-stat quality-field-stat', '');
      button.type = 'button';
      button.setAttribute('aria-label', `${label} missing: ${count}`);
      button.append(el('strong', '', count.toLocaleString('en-US')), el('span', '', `${label} missing`));
      button.addEventListener('click', () => {
        $('#qualityKind').value = 'missing_metadata';
        $('#qualityField').value = field;
        $('#qualityStatus').value = 'open';
        state.offset = 0; state.selected.clear(); writeFiltersToUrl(false); loadItems();
      });
      host.append(button);
    }
  }

  function renderHistory(summary) {
    const trend = $('#qualityTrend');
    const historyHost = $('#qualityHistory');
    trend.replaceChildren(); historyHost.replaceChildren();
    const history = Array.isArray(summary.history) ? summary.history : [];
    if (!history.length) {
      trend.append(el('p', 'catalog-empty', 'Run a complete audit to start tracking quality over time.'));
      return;
    }
    if (history.length < 2) {
      trend.append(el('p', 'catalog-empty', 'Run another complete audit to compare changes with this baseline.'));
    } else {
      const latest = history[0];
      const previous = history[1];
      const delta = latest.open_total - previous.open_total;
      const direction = delta < 0 ? 'decreased' : delta > 0 ? 'increased' : 'did not change';
      const amount = Math.abs(delta);
      const summaryLine = el('p', 'quality-trend-summary',
        `Open findings ${direction}${delta ? ` by ${amount}` : ''} since ${when(previous.created_at)}.`);
      trend.append(summaryLine);
      const deltas = el('div', 'quality-trend-deltas');
      for (const [kind, label] of Object.entries(labels)) {
        const change = Number(latest.counts?.[kind] || 0) - Number(previous.counts?.[kind] || 0);
        const card = el('div', `quality-trend-card ${change < 0 ? 'trend-down' : change > 0 ? 'trend-up' : ''}`);
        card.append(el('strong', '', `${change > 0 ? '+' : ''}${change}`), el('span', '', label));
        deltas.append(card);
      }
      trend.append(deltas);
    }
    for (const audit of history) {
      const row = el('article', 'quality-history-item');
      row.append(el('strong', '', when(audit.created_at)),
        el('span', '', `${Number(audit.open_total || 0).toLocaleString('en-US')} open findings`),
        el('span', '', `${Number(audit.checked || 0).toLocaleString('en-US')} of ${Number(audit.total || 0).toLocaleString('en-US')} checked`));
      if (audit.library_inventory_status === 'partial') {
        const entries = Number.isInteger(audit.library_inventory_entries)
          ? ` ${audit.library_inventory_entries.toLocaleString('en-US')} entries were scanned.` : '';
        row.append(el('small', 'catalog-state', `Managed file inventory was partial.${entries} Orphan-file findings are excluded from Quality counts and may be incomplete.`));
      }
      historyHost.append(row);
    }
  }

  function renderDiffs(host, details) {
    const diffs = Array.isArray(details?.diffs) ? details.diffs : [];
    for (const diff of diffs) {
      const row = el('div', 'quality-diff');
      row.append(el('strong', '', diff.field || 'Metadata field'),
        el('span', '', `In NFO: ${diff.current || '—'}`),
        el('span', '', `Catalog: ${diff.expected || '—'}`));
      host.append(row);
    }
  }

  function updateRepairButton() {
    const button = $('#qualityRepair');
    if (button) button.disabled = state.busy || state.selected.size === 0;
    const count = $('#qualitySelectionCount');
    if (count) count.textContent = state.selected.size ? `${state.selected.size} selected (maximum 25)` : 'Select up to 25 NFO or artwork findings to repair.';
  }

  function renderItems(result, requestId) {
    const host = $('#qualityRows');
    host.replaceChildren();
    if (!result.items.length) {
      host.append(el('p', 'catalog-empty', result.total ? 'No issues match these filters.' : 'No matching quality issues were found in the latest complete audit.'));
      $('#qualityPagination').hidden = true;
      return;
    }
    const actionable = result.items.some(item => item.findings.some(finding =>
      finding.status === 'open' && ['missing_nfo', 'invalid_nfo', 'nfo_metadata_mismatch', 'missing_artwork'].includes(finding.kind)));
    if (actionable) {
      const bar = el('section', 'quality-repair-bar');
      const copy = el('div');
      copy.append(el('strong', '', 'Repair selected local files'),
        el('p', '', 'Preview and queue up to 25 selected NFO or artwork findings. Existing artwork is never overwritten.'));
      const count = el('span', 'catalog-feedback', 'Select up to 25 NFO or artwork findings to repair.');
      count.id = 'qualitySelectionCount';
      const button = el('button', 'button button-primary', 'Preview selected repair');
      button.id = 'qualityRepair'; button.type = 'button'; button.disabled = true;
      button.addEventListener('click', repairSelected);
      bar.append(copy, count, button);
      host.append(bar);
    }
    for (const item of result.items) {
      const card = el('article', 'operation-history-row quality-item');
      const heading = el('div', 'quality-item-heading');
      const title = el('strong', '', item.title || 'Untitled');
      heading.append(title, el('span', 'status-pill pill-neutral', `${item.finding_count} issue${item.finding_count === 1 ? '' : 's'}`));
      card.append(heading);
      const link = el('a', 'button button-quiet', 'Open in Library');
      link.href = `#film/${encodeURIComponent(item.video_id)}`;
      card.append(el('p', 'catalog-feedback', `Catalog item ${item.video_id}`), link);
      const hashValues = el('div', 'quality-fingerprints');
      hashValues.append(
        el('p', '', `OSHASH: ${item.oshash || 'Not available'}`),
        el('p', '', `PHASH: ${item.phash || 'Not calculated (computed for a subset)'}`),
      );
      card.append(hashValues);
      const findings = el('div', 'quality-findings');
      for (const finding of item.findings) {
        const row = el('section', 'quality-finding');
        const titleRow = el('div', 'quality-finding-title');
        titleRow.append(el('strong', '', findingLabel(finding)),
          el('span', 'status-pill pill-neutral', finding.status));
        row.append(titleRow);
        row.append(el('p', '', finding.evidence || 'This item needs review.'));
        if (finding.status === 'acknowledged' && finding.acknowledgement) {
          row.append(el('p', 'quality-acknowledgement-note', `Acknowledged: ${finding.acknowledgement.note}`));
          const reopen = el('button', 'button button-quiet', 'Reopen finding');
          reopen.type = 'button';
          reopen.addEventListener('click', () => reopenFinding(finding.acknowledgement.id));
          row.append(reopen);
        }
        const fields = Array.isArray(finding.details?.fields) ? finding.details.fields : [];
        if (fields.length) row.append(el('p', 'quality-fields', `Fields: ${fields.map(field => fieldLabels[field] || field).join(', ')}`));
        renderDiffs(row, finding.details);
        const repairable = finding.status === 'open' &&
          ['missing_nfo', 'invalid_nfo', 'nfo_metadata_mismatch', 'missing_artwork'].includes(finding.kind);
        if (repairable) {
          const label = el('label', 'quality-select');
          const checkbox = document.createElement('input');
          checkbox.type = 'checkbox'; checkbox.checked = state.selected.has(finding.id);
          checkbox.disabled = !checkbox.checked && state.selected.size >= 25;
          checkbox.setAttribute('aria-label', `Select ${findingLabel(finding)} for repair`);
          checkbox.addEventListener('change', () => {
            if (checkbox.checked && state.selected.size < 25) state.selected.add(finding.id);
            else { checkbox.checked = false; state.selected.delete(finding.id); }
            updateRepairButton();
            document.querySelectorAll('#qualityRows input[type="checkbox"]').forEach(control => {
              if (!control.checked) control.disabled = state.selected.size >= 25;
            });
          });
          label.append(checkbox, document.createTextNode(' Select for repair'));
          row.append(label);
        } else if (finding.kind === 'missing_metadata' && fields.includes('performers')) {
          const peopleLink = el('a', 'quality-inline-link', 'Review performer catalog');
          peopleLink.href = '#performers'; row.append(peopleLink);
        }
        if (finding.status === 'open') {
          const triage = el('div', 'quality-triage');
          const openTriage = el('button', 'button button-quiet', 'Acknowledge exception');
          openTriage.type = 'button';
          openTriage.addEventListener('click', () => {
            const note = document.createElement('textarea');
            note.maxLength = 500; note.rows = 2;
            note.setAttribute('aria-label', `Reason for acknowledging ${findingLabel(finding)}`);
            note.placeholder = 'Why is this an accepted exception? (required)';
            const save = el('button', 'button button-quiet', 'Save accepted exception');
            save.type = 'button';
            save.addEventListener('click', () => acknowledgeFinding(item.video_id, finding, note, save));
            triage.replaceChildren(note, save);
            note.focus();
          });
          triage.append(openTriage); row.append(triage);
        }
        findings.append(row);
      }
      card.append(findings);
      host.append(card);
    }
    renderPagination(result, requestId);
    updateRepairButton();
  }

  function renderPagination(result, requestId) {
    const nav = $('#qualityPagination'); nav.replaceChildren();
    nav.hidden = result.total <= result.limit;
    const previous = el('button', 'button button-quiet', '← Previous');
    previous.type = 'button'; previous.disabled = result.offset <= 0;
    previous.addEventListener('click', async () => { state.offset = Math.max(0, state.offset - result.limit); writeFiltersToUrl(false); await loadItems(); $('#qualityPagination button:first-child')?.focus(); });
    const next = el('button', 'button button-quiet', 'Next →');
    next.type = 'button'; next.disabled = result.offset + result.items.length >= result.total;
    next.addEventListener('click', async () => { state.offset += result.limit; writeFiltersToUrl(false); await loadItems(); $('#qualityPagination button:last-child')?.focus(); });
    const start = result.items.length ? result.offset + 1 : 0;
    const end = Math.min(result.offset + result.items.length, result.total);
    const info = el('span', 'catalog-feedback', `Showing ${start}–${end} of ${result.total}`);
    info.setAttribute('role', 'status'); info.setAttribute('aria-live', 'polite');
    nav.append(previous, info, next);
  }

  async function loadItems() {
    const requestId = ++state.requestId;
    const kind = $('#qualityKind').value;
    const status = $('#qualityStatus').value;
    const field = $('#qualityField').value;
    const query = new URLSearchParams({ limit: '50', offset: String(state.offset), status });
    if (kind) query.set('kind', kind);
    if (field) query.set('field', field);
    const host = $('#qualityRows'); host.replaceChildren(el('p', 'catalog-feedback', 'Loading quality findings…'));
    try {
      const result = await window.AdminV2.getJson(`${api}/items?${query}`);
      if (requestId !== state.requestId) return;
      state.total = result.total;
      state.selected.clear();
      renderItems(result, requestId);
      setFeedback(`${state.auditNotice} ${result.total.toLocaleString('en-US')} item${result.total === 1 ? '' : 's'} match these filters.`, state.auditStale ? 'stale' : 'ready');
    } catch (error) {
      if (requestId !== state.requestId) return;
      host.replaceChildren(el('p', 'catalog-state', `Quality findings are unavailable. ${error.message}`));
      $('#qualityPagination').hidden = true;
      setFeedback('Could not load catalog quality data.', 'error');
    }
  }

  async function load() {
    const loadSequence = ++state.loadSequence;
    state.requestId += 1;
    readFiltersFromUrl();
    state.selected.clear();
    setFeedback('Loading the latest complete audit…');
    try {
      const summary = await window.AdminV2.getJson(`${api}/summary`);
      if (loadSequence !== state.loadSequence) return;
      state.summary = summary;
      renderSummary(summary);
      renderHistory(summary);
      if (summary.status !== 'ready') {
        const current = summary.current_audit_state ? ` The latest audit is ${summary.current_audit_state}.` : '';
        const explanations = {
          no_audit: ' No audit has been run yet.',
          file_consistency_only: ' The latest run checked files only and did not create a metadata quality baseline.',
          audit_in_progress: ' The latest audit is still in progress; its partial results are not shown.',
          audit_not_complete: ' The latest audit did not check every catalog item; its partial results are not shown.',
          legacy_missing_catalog_total: ' A previous audit completed, but it did not record a verifiable catalog total.',
        };
        const explanation = explanations[summary.status_reason] || '';
        const nextStep = summary.status_reason === 'audit_in_progress'
          ? ' Refresh later to check its result.'
          : summary.status_reason === 'no_audit' ? ' Run an audit to establish a complete baseline.'
            : summary.status_reason === 'file_consistency_only'
              ? ' Run Metadata quality or Full audit to establish catalog quality counts.'
              : ' Run a new audit to establish a complete baseline.';
        setFeedback(`No complete catalog audit is available.${explanation}${current}${nextStep}`, 'stale');
        $('#qualityRows').replaceChildren(el('p', 'catalog-empty', 'A complete catalog audit must check every registered item. Partial managed-file inventory can leave orphan-file findings incomplete.'));
        $('#qualityPagination').hidden = true;
        return;
      }
      const audit = summary.latest_audit;
      const stale = summary.current_audit_id && summary.current_audit_id !== audit.id;
      state.auditStale = Boolean(stale);
      const checked = Number.isInteger(audit.checked) ? audit.checked.toLocaleString('en-US') : 'Unknown';
      const total = Number.isInteger(audit.total) ? audit.total.toLocaleString('en-US') : 'unknown';
      const partialInventory = audit.library_inventory_status === 'partial'
        ? ` The catalog check is complete, but the managed file inventory was partial${Number.isInteger(audit.library_inventory_entries) ? ` (${audit.library_inventory_entries.toLocaleString('en-US')} entries scanned)` : ''}; orphan-file findings are excluded from Quality counts and may be incomplete.`
        : '';
      state.auditNotice = `Latest complete catalog audit: ${when(audit.created_at)} · ${checked} of ${total} catalog entries checked.${partialInventory}${stale ? ` A newer audit is ${summary.current_audit_state}; this report remains the latest complete catalog result.` : ''}`;
      setFeedback(state.auditNotice, stale ? 'stale' : 'ready');
      await loadItems();
    } catch (error) {
      if (loadSequence !== state.loadSequence) return;
      setFeedback(`Quality data is unavailable. ${error.message}`, 'error');
      $('#qualityRows').replaceChildren(el('p', 'catalog-state', 'Could not load the latest complete audit.'));
    }
  }

  const display = value => Array.isArray(value) ? value.join(', ') : String(value ?? '');
  const editableValue = (field, value) => Array.isArray(value) ? value.join('\n') : String(value ?? '');
  function proposalChanges(card, proposal) {
    const changes = {};
    for (const field of Object.keys(proposal.changes || {})) {
      const input = card.querySelector(`[data-proposal-field="${field}"]`);
      if (!input) continue;
      changes[field] = ['performers', 'tags'].includes(field)
        ? input.value.split(/\r?\n/).map(value => value.trim()).filter(Boolean)
        : input.value.trim();
    }
    return changes;
  }
  function renderProposalPreview(host, preview, onQueue) {
    host.replaceChildren();
    host.append(el('strong', '', 'Review changes before queueing'));
    const list = el('dl', 'metadata-diff-list');
    for (const [field, values] of Object.entries(preview.fields || {})) {
      list.append(el('dt', '', reviewFieldLabels[field] || field));
      const dd = el('dd');
      dd.append(el('span', 'metadata-diff-before', display(values.before) || '(empty)'),
        el('span', 'metadata-diff-arrow', '→'),
        el('strong', 'metadata-diff-after', display(values.after) || '(empty)'));
      list.append(dd);
    }
    host.append(list);
    const queue = el('button', 'button button-primary', 'Queue approved metadata update');
    queue.type = 'button'; queue.addEventListener('click', onQueue); host.append(queue);
  }
  function renderProposal(proposal) {
    const card = el('article', 'operation-history-row metadata-proposal');
    const heading = el('div', 'quality-item-heading');
    heading.append(el('strong', '', proposal.title || 'Untitled'),
      el('span', 'status-pill pill-neutral', `${Math.round(Number(proposal.confidence || 0) * 100)}% match`));
    card.append(heading);
    const open = el('a', 'quality-inline-link', 'Open in Library');
    open.href = `#film/${encodeURIComponent(proposal.video_id)}`;
    card.append(el('p', 'catalog-feedback', `Source: ${proposal.source || 'External catalog'} · ${when(proposal.created_at)}`), open);
    if (proposal.source_url) {
      const sourceLink = el('a', 'quality-inline-link', 'View matched source');
      sourceLink.href = proposal.source_url; sourceLink.target = '_blank'; sourceLink.rel = 'noopener noreferrer';
      card.append(sourceLink);
    }
    if (proposal.stale) card.append(el('p', 'catalog-state', 'This suggestion is out of date because catalog metadata changed. Refresh or reject it.'));
    const form = el('div', 'metadata-proposal-fields');
    for (const [field, suggested] of Object.entries(proposal.changes || {})) {
      const section = el('section', 'metadata-edit-field');
      const label = el('label', '', reviewFieldLabels[field] || field);
      const input = ['plot', 'performers', 'tags'].includes(field) ? document.createElement('textarea') : document.createElement('input');
      input.dataset.proposalField = field;
      input.value = editableValue(field, suggested);
      if (field === 'date_released') input.type = 'date';
      input.setAttribute('aria-label', `${reviewFieldLabels[field] || field} suggestion`);
      const current = el('p', 'catalog-feedback', `Current: ${display(proposal.current?.[field]) || '(empty)'}`);
      section.append(label, current, input); form.append(section);
    }
    card.append(form);
    const previewHost = el('div', 'metadata-proposal-preview');
    const actions = el('div', 'diagnostics-actions');
    const previewButton = el('button', 'button button-primary', 'Preview suggestion');
    previewButton.type = 'button'; previewButton.disabled = Boolean(proposal.stale);
    previewButton.addEventListener('click', async () => {
      previewButton.disabled = true;
      try {
        const result = await window.AdminV2.postJson('/admin/api/v2/operations/preview', {
          type: 'metadata_update', video_id: proposal.video_id, expected_revision: proposal.revision,
          changes: proposalChanges(card, proposal), reset_fields: [], proposal_id: proposal.id,
        });
        renderProposalPreview(previewHost, result, async () => {
          const queueButton = [...previewHost.querySelectorAll('button')].at(-1);
          if (queueButton) queueButton.disabled = true;
          try {
            const queued = await window.AdminV2.postJson('/admin/api/v2/operations/execute', { plan_id: result.plan_id });
            $('#metadataProposalsFeedback').textContent = `Approved metadata update queued (${queued.jobs?.[0]?.job_id || queued.job_id || 'job'}).`;
            await loadProposals();
          } catch (error) {
            $('#metadataProposalsFeedback').textContent = `Could not queue the approved metadata update. ${error.message}`;
            if (queueButton) queueButton.disabled = false;
          }
        });
      } catch (error) {
        $('#metadataProposalsFeedback').textContent = `Could not create a current metadata preview. ${error.message}`;
        previewButton.disabled = false;
        if (error.status === 409) void loadProposals();
      }
    });
    const reject = el('button', 'button button-quiet', 'Reject suggestion');
    reject.type = 'button';
    reject.addEventListener('click', async () => {
      reject.disabled = true;
      try {
        await window.AdminV2.postJson(`/admin/api/v2/quality/proposals/${proposal.id}/reject`, {});
        $('#metadataProposalsFeedback').textContent = 'Suggestion rejected.';
        await loadProposals();
      } catch (error) {
        $('#metadataProposalsFeedback').textContent = `Could not reject this suggestion. ${error.message}`;
        reject.disabled = false;
      }
    });
    for (const input of form.querySelectorAll('input,textarea')) input.addEventListener('input', () => previewHost.replaceChildren());
    actions.append(previewButton, reject); card.append(actions, previewHost);
    return card;
  }
  function renderProposalPagination(result, requestId) {
    const nav = $('#metadataProposalsPagination'); nav.replaceChildren();
    nav.hidden = result.total <= result.limit;
    const previous = el('button', 'button button-quiet', '← Previous'); previous.type = 'button';
    previous.disabled = result.offset <= 0;
    previous.addEventListener('click', () => { state.proposalOffset = Math.max(0, state.proposalOffset - result.limit); loadProposals(); });
    const next = el('button', 'button button-quiet', 'Next →'); next.type = 'button';
    next.disabled = result.offset + result.items.length >= result.total;
    next.addEventListener('click', () => { state.proposalOffset += result.limit; loadProposals(); });
    nav.append(previous, el('span', 'catalog-feedback', `${result.total ? result.offset + 1 : 0}–${Math.min(result.offset + result.items.length, result.total)} of ${result.total}`), next);
  }
  async function loadProposals() {
    const requestId = ++state.proposalRequestId;
    const host = $('#metadataProposalsRows');
    host.replaceChildren(el('p', 'catalog-feedback', 'Loading metadata suggestions…'));
    try {
      const result = await window.AdminV2.getJson(`/admin/api/v2/quality/proposals?limit=30&offset=${state.proposalOffset}`);
      if (requestId !== state.proposalRequestId) return;
      state.proposalTotal = result.total;
      host.replaceChildren();
      for (const proposal of result.items || []) host.append(renderProposal(proposal));
      if (!result.items?.length) host.append(el('p', 'catalog-empty', 'No metadata suggestions are waiting for review.'));
      $('#metadataProposalsFeedback').textContent = `${Number(result.total || 0).toLocaleString('en-US')} suggestion${result.total === 1 ? '' : 's'} waiting for review.`;
      renderProposalPagination(result, requestId);
    } catch (error) {
      if (requestId !== state.proposalRequestId) return;
      host.replaceChildren(el('p', 'catalog-state', `Metadata suggestions are unavailable. ${error.message}`));
      $('#metadataProposalsPagination').hidden = true;
      $('#metadataProposalsFeedback').textContent = 'Could not load metadata suggestions.';
    }
  }

  async function repairSelected() {
    if (!state.selected.size || state.selected.size > 25 || state.busy || !state.summary?.latest_audit?.id) return;
    state.busy = true; updateRepairButton();
    const findingIds = [...state.selected];
    try {
      const preview = await window.AdminV2.postJson(`/admin/api/v2/audits/${encodeURIComponent(state.summary.latest_audit.id)}/repairs/preview`, { finding_ids: findingIds });
      const details = preview.items.map(item => `${item.title} · ${item.findings.map(finding => finding.kind.replaceAll('_', ' ')).join(', ')} · ${item.file_count} registered file(s)`).join('\n');
      if (!confirm(`Repair the selected local NFO and artwork findings?\n\n${details}\n\nNFO files will be generated from current effective catalog metadata. Existing artwork will not be overwritten. Jellyfin refresh acceptance does not confirm import completion.`)) return;
      const queued = await window.AdminV2.postJson('/admin/api/v2/operations/execute', { plan_id: preview.plan_id });
      $('#qualityActionFeedback').hidden = false;
      $('#qualityActionFeedback').textContent = `Local file repair queued (${queued.job_id}). Check Tasks for completion, then run a new audit to confirm the result.`;
      state.selected.clear();
      await loadItems();
    } catch (error) {
      $('#qualityActionFeedback').hidden = false;
      $('#qualityActionFeedback').textContent = `Could not queue the selected repair. ${error.message}`;
    } finally { state.busy = false; updateRepairButton(); }
  }

  async function acknowledgeFinding(videoId, finding, note, button) {
    const reason = note.value.trim();
    if (!reason) { note.focus(); note.setCustomValidity('Enter a reason for this accepted exception.'); note.reportValidity(); return; }
    note.setCustomValidity(''); button.disabled = true;
    try {
      await window.AdminV2.postJson(`${api}/acknowledge`, {
        finding_id: finding.id, video_id: videoId, kind: finding.kind, note: reason,
      });
      const feedback = $('#qualityActionFeedback');
      feedback.hidden = false; feedback.textContent = `${findingLabel(finding)} acknowledged. You can reopen it from the Acknowledged filter.`;
      await load();
    } catch (error) {
      const feedback = $('#qualityActionFeedback');
      feedback.hidden = false; feedback.textContent = `Could not acknowledge this finding. ${error.message}`;
    } finally { button.disabled = false; }
  }

  async function reopenFinding(acknowledgementId) {
    try {
      await window.AdminV2.postJson(`${api}/reopen`, { acknowledgement_id: acknowledgementId });
      const feedback = $('#qualityActionFeedback');
      feedback.hidden = false; feedback.textContent = 'Finding reopened and returned to the active quality list.';
      await load();
    } catch (error) {
      const feedback = $('#qualityActionFeedback');
      feedback.hidden = false; feedback.textContent = `Could not reopen this finding. ${error.message}`;
    }
  }

  $('#qualityRefresh').addEventListener('click', load);
  $('#qualityRunOverviewAudit').addEventListener('click', () => $('#qualityRunAudit').click());
  $('#qualityRunAudit').addEventListener('click', async () => {
    const mode = $('#qualityAuditMode').value;
    const confirmations = {
      metadata_quality: 'Queue a metadata quality audit? It checks effective metadata for every catalog video and does not inspect local files or contact Jellyfin.',
      file_consistency: 'Queue a file consistency audit? It checks registered STRM/NFO/artwork files, orphan inventory, and Jellyfin import state when available.',
      full: 'Queue a full audit? It creates separate metadata quality and file consistency jobs. File inspection and Jellyfin checks may take longer.'
    };
    if (!confirm(confirmations[mode] || confirmations.full)) return;
    const button = $('#qualityRunAudit'); button.disabled = true;
    try {
      const result = await window.AdminV2.postJson('/admin/api/v2/audits/execute', { mode });
      const jobIds = Array.isArray(result.job_ids) ? result.job_ids : [result.job_id];
      setFeedback(`${mode === 'full' ? 'Full audit jobs' : `${mode.replaceAll('_', ' ')} audit`} queued (${jobIds.join(', ')}). The latest complete report remains available until the requested work finishes.`, 'stale');
      document.dispatchEvent(new CustomEvent('adminv2:audits-updated'));
    } catch (error) { setFeedback(`Could not queue the requested audit. ${error.message}`, 'error'); }
    finally { button.disabled = false; }
  });
  $('#qualityKind').addEventListener('change', () => { state.offset = 0; state.selected.clear(); writeFiltersToUrl(false); loadItems(); });
  $('#qualityField').addEventListener('change', () => { state.offset = 0; state.selected.clear(); writeFiltersToUrl(false); loadItems(); });
  $('#qualityStatus').addEventListener('change', () => { state.offset = 0; state.selected.clear(); writeFiltersToUrl(false); loadItems(); });
  $('#qualityFilters').addEventListener('reset', event => { event.preventDefault(); $('#qualityKind').value = ''; $('#qualityField').value = ''; $('#qualityStatus').value = 'open'; state.offset = 0; state.selected.clear(); writeFiltersToUrl(false); loadItems(); });
  $('#metadataProposalsRefresh').addEventListener('click', () => { state.proposalOffset = 0; loadProposals(); });
  function syncQuality(route, tab) {
    if (route !== 'quality') return;
    if (tab === 'enrichment') void loadProposals();
    else void load();
  }
  document.addEventListener('adminv2:navigate', event => syncQuality(event.detail?.route, event.detail?.tab));
  const initialQualityRoute = location.hash.replace(/^#/, '').split('?', 2);
  if (initialQualityRoute[0] === 'quality') {
    syncQuality('quality', new URLSearchParams(initialQualityRoute[1] || '').get('tab'));
  }
})();
