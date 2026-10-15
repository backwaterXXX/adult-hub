(() => {
  'use strict';
  const endpoint = '/admin/api/v2/linkcheck';
  const $ = selector => document.querySelector(selector);
  let requestSeq = 0;

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = String(text);
    return node;
  }

  function displayTime(value) {
    if (!value) return 'Time unavailable';
    const parsed = new Date(value.replace(' ', 'T'));
    return Number.isNaN(parsed.getTime()) ? 'Time unavailable'
      : new Intl.DateTimeFormat('en-US', { dateStyle: 'medium', timeStyle: 'short' }).format(parsed);
  }

  function render(payload) {
    const host = $('#linkcheckRows');
    host.replaceChildren();
    const sampleSection = $('#linkcheckSamples');
    const sampleHost = $('#linkcheckSampleRows');
    const sampleSummary = $('#linkcheckSamplesSummary');
    sampleHost.replaceChildren();
    sampleSection.hidden = true;
    if (!payload.available) {
      $('#linkcheckFeedback').textContent = 'The link-check report source is temporarily unavailable. Try refreshing in a moment.';
      return;
    }
    if (!payload.exists) {
      $('#linkcheckFeedback').textContent = 'No link-check report has been recorded yet.';
      return;
    }
    if (payload.empty || !payload.providers.length) {
      $('#linkcheckFeedback').textContent = 'The latest report is empty or contains no recognized provider summaries.';
      return;
    }
    $('#linkcheckFeedback').textContent = `Latest report summary · ${displayTime(payload.checked_at)}`;
    for (const item of payload.providers) {
      const row = el('article', 'operation-history-row');
      const copy = el('div');
      const state = item.status === 'healthy' ? 'Healthy'
        : item.status === 'broken' ? 'Broken'
          : 'No samples';
      copy.append(el('strong', '', item.provider));
      copy.append(el('p', '', item.status === 'no_samples'
        ? 'No catalog items were sampled for this provider.'
        : `${item.ok} of ${item.total} sampled streams passed.`));
      const failures = Object.entries(item.failure_reasons || {});
      if (failures.length) {
        copy.append(el('p', '', `Failures: ${failures.map(([reason, count]) => `${reason} (${count})`).join(', ')}`));
      }
      copy.append(el('small', '', state));
      row.append(copy);
      host.append(row);
    }
    const samples = Array.isArray(payload.samples) ? payload.samples : [];
    if (samples.length) {
      const total = Number.isSafeInteger(payload.samples_total) && payload.samples_total >= samples.length
        ? payload.samples_total : samples.length;
      sampleSummary.textContent = `Recent sample results · showing ${samples.length} of ${total}`;
      sampleSection.hidden = false;
      for (const sample of [...samples].reverse()) {
        if (!sample || typeof sample.provider !== 'string' || typeof sample.timestamp !== 'string') continue;
        const passed = sample.result === 'passed';
        const failed = sample.result === 'failed';
        const row = el('article', `operation-history-row linkcheck-sample${failed ? ' is-failed' : ''}`);
        const copy = el('div');
        copy.append(el('strong', '', `${sample.provider} · ${passed ? 'Passed' : failed ? 'Failed' : 'Unknown'}`));
        copy.append(el('p', '', displayTime(sample.timestamp)));
        const failure = ['Resolver request failed', 'Playlist request failed', 'Stream check failed']
          .includes(sample.failure_reason) ? sample.failure_reason : null;
        if (failed && failure) copy.append(el('small', '', failure));
        row.append(copy);
        sampleHost.append(row);
      }
      if (payload.samples_truncated === true) {
        $('#linkcheckFeedback').textContent += ' Only the latest 200 individual checks are shown.';
      }
    }
  }

  async function load() {
    const sequence = ++requestSeq;
    $('#linkcheckRefresh').disabled = true;
    $('#linkcheckFeedback').textContent = 'Loading link-check report…';
    try {
      const payload = await window.AdminV2.getJson(endpoint);
      if (!Array.isArray(payload.providers) || typeof payload.available !== 'boolean') {
        throw new Error('The report response is incomplete.');
      }
      if (sequence !== requestSeq) return;
      render(payload);
    } catch (error) {
      if (sequence === requestSeq) $('#linkcheckFeedback').textContent = `Could not load the link-check report. ${error.message}`;
    } finally {
      if (sequence === requestSeq) $('#linkcheckRefresh').disabled = false;
    }
  }

  $('#linkcheckRefresh').addEventListener('click', load);
  document.addEventListener('adminv2:navigate', event => {
    if (event.detail.route === 'quality' && (event.detail.tab || 'issues') === 'issues') load();
  });
})();
