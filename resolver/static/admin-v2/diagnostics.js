(() => {
  'use strict';
  const api = '/admin/api/v2/diagnostics/playback';
  const probesApi = '/admin/api/v2/diagnostics/probes';
  const $ = selector => document.querySelector(selector);
  let diagnosticsLoadSeq = 0;
  let diagnosticsDetailSeq = 0;
  let diagnosticsProbeSeq = 0;
  const el = (tag, className, value) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value !== undefined) node.textContent = String(value);
    return node;
  };
  const date = value => value ? new Date(Number(value) * 1000).toLocaleString('en-US') : '—';

  async function loadDiagnostics() {
    const sequence = ++diagnosticsLoadSeq;
    diagnosticsDetailSeq += 1;
    const probeSequence = ++diagnosticsProbeSeq;
    loadServiceProbes(probeSequence);
    const feedback = $('#diagnosticsFeedback'); window.AdminV2.setFeedback(feedback, 'Loading diagnostic status and reports…');
    try {
      const [status, summary, reports] = await Promise.all([
        window.AdminV2.getJson(`${api}/status`), window.AdminV2.getJson(`${api}/summary`),
        window.AdminV2.getJson(`${api}/reports?limit=50`)
      ]);
      if (sequence !== diagnosticsLoadSeq) return null;
      const active = status.enabled === true;
      window.AdminV2.setFeedback(feedback, active
        ? `Diagnostics are enabled${status.enabled_until ? ` until ${status.enabled_until}` : ''}. Saved reports: ${status.saved_count ?? 'unknown'}.`
        : `Diagnostics are disabled. Saved reports: ${status.saved_count ?? 'unknown'}.`);
      const summaryNode = $('#diagnosticsSummary'); summaryNode.replaceChildren();
      for (const [label, value] of [['Requests analyzed', summary.analyzed], ['Analysis limit', summary.analysis_limit], ['Window in days', summary.days], ['Truncated', summary.truncated]]) {
        if (value !== undefined && value !== null) summaryNode.append(el('span', 'status-pill pill-neutral', `${label}: ${value}`));
      }
      (summary.by_provider || []).forEach(provider => summaryNode.append(el('span', 'status-pill pill-neutral', `${provider.provider}: ${provider.requests} requests, ${provider.failures} errors`)));
      const host = $('#diagnosticsRows'); host.replaceChildren();
      $('#diagnosticsClear').disabled = reports.items.length === 0;
      if (!reports.items.length) host.append(el('p', 'catalog-empty', 'No playback reports have been captured.'));
      reports.items.forEach(report => {
        const row = el('div', 'operation-history-row'); const copy = el('div');
        copy.append(el('strong', '', `${report.provider || 'Unknown provider'} · ${report.outcome || 'unknown outcome'}`));
        copy.append(el('p', '', `${report.method || 'Playback request'} · ${report.error_type || 'No error category'} · ${date(report.started_epoch)}`));
        const details = el('button', 'button button-quiet', 'Details'); details.type = 'button';
        details.addEventListener('click', async () => {
          const detailSequence = ++diagnosticsDetailSeq;
          try {
            const detail = await window.AdminV2.getJson(`${api}/reports/${encodeURIComponent(report.report_id)}`);
            if (detailSequence !== diagnosticsDetailSeq || sequence !== diagnosticsLoadSeq) return;
            const safe = detail.report;
            const keys = ['started_at','ended_at','provider','method','range','status_code','outcome','error_type','elapsed_ms','file_size_ms','upstream_first_byte_ms','downstream_first_byte_ms','upstream_bytes','downstream_bytes','upstream_max_gap_ms','downstream_max_gap_ms','phase','phase_idle_ms','samples_truncated'];
            const summary = Object.fromEntries(keys.filter(key => safe[key] !== undefined).map(key => [key, safe[key]]));
            $('#diagnosticsDetail').textContent = JSON.stringify(summary, null, 2);
            $('#diagnosticsDetail').hidden = false;
          } catch (error) { if (detailSequence === diagnosticsDetailSeq && sequence === diagnosticsLoadSeq) window.AdminV2.setFeedback(feedback, `Could not load report details. ${error.message}`, 'error'); }
        });
        const exportOne = el('button', 'button button-quiet', 'Export'); exportOne.type = 'button';
        exportOne.addEventListener('click', () => exportReport(report.report_id));
        row.append(copy, details, exportOne); host.append(row);
      });
      return true;
    } catch (error) {
      if (sequence !== diagnosticsLoadSeq) return null;
      window.AdminV2.setFeedback(feedback, `Playback diagnostics are unavailable. ${error.message}`, 'error');
      return false;
    }
  }

  async function loadServiceProbes(sequence = ++diagnosticsProbeSeq) {
    const host = $('#diagnosticsProbeRows');
    if (!host) return;
    host.replaceChildren(el('p', 'catalog-feedback', 'Checking configured services…'));
    try {
      const payload = await window.AdminV2.getJson(probesApi);
      if (sequence !== diagnosticsProbeSeq || !Array.isArray(payload.services)) return;
      host.replaceChildren();
      const labels = new Map([
        ['postgresql', 'PostgreSQL'], ['resolver', 'Resolver'], ['sync_worker', 'Sync worker'],
        ['phash_worker', 'pHash worker'], ['jellyfin', 'Jellyfin'],
      ]);
      const states = new Map([
        ['healthy', 'Healthy'], ['unavailable', 'Unavailable'], ['not_configured', 'Not configured'], ['unknown', 'Unknown'],
      ]);
      for (const service of payload.services) {
        if (!service || !labels.has(service.service)) continue;
        const status = states.get(service.status) || 'Unknown';
        const row = el('div', 'operation-history-row');
        const copy = el('div');
        copy.append(el('strong', '', labels.get(service.service)));
        const detail = [status];
        if (Number.isInteger(service.elapsed_ms) && service.elapsed_ms >= 0) detail.push(`${service.elapsed_ms} ms`);
        if (Number.isInteger(service.http_status) && service.http_status >= 100 && service.http_status <= 599) {
          detail.push(`HTTP ${service.http_status}`);
        }
        copy.append(el('p', '', detail.join(' · ')));
        row.append(copy, el('span', `status-pill ${status === 'Healthy' ? 'pill-success' : 'pill-neutral'}`, status));
        host.append(row);
      }
      if (!host.children.length) host.append(el('p', 'catalog-empty', 'No service probe results are available.'));
      const checkedAt = typeof payload.checked_at === 'string' ? new Date(payload.checked_at) : null;
      if (checkedAt && !Number.isNaN(checkedAt.getTime())) {
        host.append(el('p', 'detail-muted', `Checked ${checkedAt.toLocaleString('en-US')}${Number.isInteger(payload.cache_age_seconds) && payload.cache_age_seconds > 0 ? ` · cached ${payload.cache_age_seconds}s` : ''}.`));
      }
    } catch {
      if (sequence === diagnosticsProbeSeq) host.replaceChildren(el('p', 'state-banner', 'Service probe diagnostics are unavailable. Refresh to try again.'));
    }
  }

  async function setDiagnostics(action) {
    const feedback = $('#diagnosticsFeedback');
    try { await window.AdminV2.postJson(`${api}/${action}`, {}); await loadDiagnostics(); }
    catch (error) { window.AdminV2.setFeedback(feedback, `Could not ${action} playback diagnostics. ${error.message}`, 'error'); }
  }
  async function exportDiagnostics() {
    const feedback = $('#diagnosticsFeedback');
    const days = Number($('#diagnosticsExportDays').value);
    try {
      const result = await window.AdminV2.getJson(`${api}/export?days=${encodeURIComponent(days)}&limit=100`);
      const blob = new Blob([JSON.stringify(result, null, 2)], { type: 'application/json' });
      window.AdminV2.downloadBlob(`adult-hub-playback-diagnostics-${new Date().toISOString().slice(0, 10)}.json`, blob);
      window.AdminV2.setFeedback(feedback, `Downloaded ${result.reports.length} redacted playback reports from the last ${days} day${days === 1 ? '' : 's'}.`);
    } catch (error) { window.AdminV2.setFeedback(feedback, `Could not export playback diagnostics. ${error.message}`, 'error'); }
  }
  async function exportReport(reportId) {
    const feedback = $('#diagnosticsFeedback');
    try {
      const result = await window.AdminV2.getJson(`${api}/reports/${encodeURIComponent(reportId)}`);
      if (!result.report || typeof result.report !== 'object') throw new Error('The report response is invalid.');
      const blob = new Blob([JSON.stringify({ ok: true, report: result.report }, null, 2)], { type: 'application/json' });
      const safeId = String(result.report.report_id || 'report').replace(/[^A-Za-z0-9_-]/g, '').slice(0, 48) || 'report';
      window.AdminV2.downloadBlob(`adult-hub-playback-report-${safeId}.json`, blob);
      window.AdminV2.setFeedback(feedback, 'Downloaded a redacted playback report.');
    } catch (error) { window.AdminV2.setFeedback(feedback, `Could not export this playback report. ${error.message}`, 'error'); }
  }
  async function clearDiagnostics() {
    const feedback = $('#diagnosticsFeedback');
    if (!window.confirm('Permanently delete all saved playback reports? This does not disable diagnostic capture.')) return;
    $('#diagnosticsClear').disabled = true;
    try {
      const result = await window.AdminV2.deleteJson(`${api}/reports`);
      const success = `Deleted ${result.cleared} saved playback report${result.cleared === 1 ? '' : 's'}. Diagnostic capture settings are unchanged.`;
      if (await loadDiagnostics()) window.AdminV2.setFeedback(feedback, success);
    } catch (error) {
      window.AdminV2.setFeedback(feedback, `Could not clear playback reports. ${error.message}`, 'error');
      $('#diagnosticsClear').disabled = false;
    }
  }
  $('#diagnosticsEnable').addEventListener('click', () => setDiagnostics('enable'));
  $('#diagnosticsDisable').addEventListener('click', () => setDiagnostics('disable'));
  $('#diagnosticsRefresh').addEventListener('click', loadDiagnostics);
  $('#diagnosticsExport').addEventListener('click', exportDiagnostics);
  $('#diagnosticsClear').addEventListener('click', clearDiagnostics);
  document.addEventListener('adminv2:navigate', event => {
    if (event.detail?.route === 'system' && event.detail?.systemTab === 'diagnostics') loadDiagnostics();
  });
  if (location.hash === '#system?tab=diagnostics') loadDiagnostics();
})();
