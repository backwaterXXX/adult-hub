(() => {
  'use strict';
  const $ = selector => document.querySelector(selector);
  const reviewFieldLabels = { title: 'Title', plot: 'Description', studio: 'Studio', performers: 'Performers', tags: 'Tags', date_released: 'Release date' };
  const state = { proposalOffset: 0, proposalTotal: 0, proposalRequestId: 0 };
  let suggestionsActive = false;
  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = String(text);
    return node;
  };
  const when = value => value ? new Date(value).toLocaleString('en-US') : '—';
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
    const requestId = state.proposalRequestId;
    const current = () => requestId === state.proposalRequestId && suggestionsVisible();
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
    if (proposal.stale) card.append(el('p', 'catalog-state', 'Catalog metadata changed since this suggestion was created. Refresh and preview it against current values before approving. The external source is not rechecked.'));
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
      const caption = el('p', 'proposal-suggested-caption', 'Suggested:');
      section.append(label, current, caption, input); form.append(section);
    }
    card.append(form);
    const previewHost = el('div', 'metadata-proposal-preview');
    const actions = el('div', 'diagnostics-actions');
    const previewButton = el('button', 'button button-primary', proposal.stale ? 'Refresh and preview' : 'Preview suggestion');
    previewButton.type = 'button';
    previewButton.addEventListener('click', async () => {
      previewButton.disabled = true;
      try {
        let revision = proposal.revision;
        if (proposal.stale) {
          const refreshed = await window.AdminV2.postJson(`/admin/api/v2/quality/proposals/${proposal.id}/refresh`, {});
          if (!current()) return;
          revision = refreshed.revision;
        }
        const result = await window.AdminV2.postJson('/admin/api/v2/operations/preview', {
          type: 'metadata_update', video_id: proposal.video_id, expected_revision: revision,
          changes: proposalChanges(card, proposal), reset_fields: [], proposal_id: proposal.id,
        });
        if (!current()) return;
        renderProposalPreview(previewHost, result, async () => {
          if (!current()) return;
          const queueButton = [...previewHost.querySelectorAll('button')].at(-1);
          if (queueButton) queueButton.disabled = true;
          try {
            const queued = await window.AdminV2.postJson('/admin/api/v2/operations/execute', { plan_id: result.plan_id });
            if (!current()) return;
            window.AdminV2.showQueued(queued, 'Metadata update');
            await loadProposals();
          } catch (error) {
            if (!current()) return;
            $('#metadataProposalsFeedback').textContent = `Could not queue the approved metadata update. ${error.message}`;
            if (error.status === 409) {
              previewHost.replaceChildren();
              previewButton.disabled = false;
            } else if (queueButton) queueButton.disabled = false;
          }
        });
      } catch (error) {
        if (!current()) return;
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
        if (!current()) return;
        $('#metadataProposalsFeedback').textContent = 'Suggestion rejected.';
        await loadProposals();
      } catch (error) {
        if (!current()) return;
        $('#metadataProposalsFeedback').textContent = `Could not reject this suggestion. ${error.message}`;
        reject.disabled = false;
      }
    });
    for (const input of form.querySelectorAll('input,textarea')) input.addEventListener('input', () => previewHost.replaceChildren());
    actions.append(previewButton, reject); card.append(actions, previewHost);
    return card;
  }
  function renderProposalPagination(result) {
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
  // Suggestions live in the Library tab; a late response must not repaint it
  // once another tab is showing.
  function suggestionsVisible() {
    const pane = $('#librarySuggestionsPane');
    return suggestionsActive && Boolean(pane) && !pane.hidden;
  }
  async function loadProposals() {
    const requestId = ++state.proposalRequestId;
    const host = $('#metadataProposalsRows');
    host.replaceChildren(el('p', 'catalog-feedback', 'Loading metadata suggestions…'));
    try {
      const result = await window.AdminV2.getJson(`/admin/api/v2/quality/proposals?limit=10&offset=${state.proposalOffset}`);
      if (requestId !== state.proposalRequestId || !suggestionsVisible()) return;
      state.proposalTotal = result.total;
      host.replaceChildren();
      for (const proposal of result.items || []) host.append(renderProposal(proposal));
      if (!result.items?.length) host.append(el('p', 'catalog-empty', 'No metadata suggestions are waiting for review.'));
      $('#metadataProposalsFeedback').textContent = `${Number(result.total || 0).toLocaleString('en-US')} suggestion${result.total === 1 ? '' : 's'} waiting for review.`;
      renderProposalPagination(result);
    } catch (error) {
      if (requestId !== state.proposalRequestId || !suggestionsVisible()) return;
      host.replaceChildren(el('p', 'catalog-state', `Metadata suggestions are unavailable. ${error.message}`));
      $('#metadataProposalsPagination').hidden = true;
      $('#metadataProposalsFeedback').textContent = 'Could not load metadata suggestions.';
    }
  }

  function syncSuggestions(detail) {
    if (!detail) return;
    suggestionsActive = detail.route === 'library' && detail.libraryTab === 'suggestions';
    state.proposalRequestId += 1;
    if (suggestionsActive) void loadProposals();
  }
  $('#metadataProposalsRefresh')?.addEventListener('click', () => {
    state.proposalOffset = 0;
    return loadProposals();
  });
  document.addEventListener('adminv2:navigate', event => syncSuggestions(event.detail));
  const initial = window.AdminV2.currentRoute();
  if (initial.route === 'library') syncSuggestions(initial);
})();
