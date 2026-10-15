(() => {
  'use strict';
  const endpoint = '/admin/api/v2/sync/activity';
  const $ = selector => document.querySelector(selector);
  let requestSeq = 0;

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = String(text);
    return node;
  }

  function displayTime(value) {
    const parsed = new Date(value.replace(' ', 'T'));
    return Number.isNaN(parsed.getTime()) ? 'Time unavailable'
      : new Intl.DateTimeFormat('en-US', { dateStyle: 'medium', timeStyle: 'medium' }).format(parsed);
  }

  function render(payload) {
    const host = $('#syncActivityRows');
    host.replaceChildren();
    if (!payload.available) {
      $('#syncActivityFeedback').textContent = 'The sync activity source is temporarily unavailable. Try refreshing in a moment.';
      return;
    }
    if (!payload.exists) {
      $('#syncActivityFeedback').textContent = 'No sync activity has been recorded yet. Start a sync to see progress here.';
      return;
    }
    if (!payload.entries.length) {
      $('#syncActivityFeedback').textContent = 'The sync log is present, but it contains no recognized progress summaries.';
      return;
    }
    const hidden = Number(payload.omitted_entries) || 0;
    $('#syncActivityFeedback').textContent = `${payload.entries.length} recent progress entr${payload.entries.length === 1 ? 'y' : 'ies'} · ${hidden} detailed entr${hidden === 1 ? 'y was' : 'ies were'} hidden for privacy${payload.updated_at ? ` · Updated ${payload.updated_at}` : ''}.`;
    for (const item of payload.entries) {
      const row = el('article', `operation-history-row sync-log-${item.level}`);
      const copy = el('div');
      copy.append(el('strong', '', item.message));
      copy.append(el('p', '', displayTime(item.timestamp)));
      row.append(copy);
      host.append(row);
    }
  }

  async function load() {
    const sequence = ++requestSeq;
    $('#syncActivityRefresh').disabled = true;
    $('#syncActivityFeedback').textContent = 'Loading sync activity…';
    try {
      const payload = await window.AdminV2.getJson(endpoint);
      if (!Array.isArray(payload.entries) || typeof payload.available !== 'boolean' || typeof payload.exists !== 'boolean') {
        throw new Error('The sync activity response is incomplete.');
      }
      if (sequence !== requestSeq) return;
      render(payload);
    } catch (error) {
      if (sequence === requestSeq) $('#syncActivityFeedback').textContent = `Could not load sync activity. ${error.message}`;
    } finally {
      if (sequence === requestSeq) $('#syncActivityRefresh').disabled = false;
    }
  }

  $('#syncActivityRefresh').addEventListener('click', load);
  document.addEventListener('adminv2:navigate', event => {
    if (event.detail.route === 'sources') load();
  });
})();
