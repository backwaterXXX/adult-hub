(() => {
  'use strict';
  const $ = selector => document.querySelector(selector);
  let requestSeq = 0;
  const labels = new Map([
    ['postgresql', 'PostgreSQL'], ['resolver', 'Resolver'], ['sync_worker', 'Sync worker'],
    ['phash_worker', 'pHash worker'], ['jellyfin', 'Jellyfin'],
  ]);
  const actions = new Map([
    ['sync', 'Source synchronization'], ['backup_create', 'Database backup'],
    ['backup_restore_test', 'Backup restore test'], ['metadata_quality_audit', 'Metadata quality audit'],
    ['consistency_audit', 'File consistency audit'], ['catalog_full_audit', 'Full catalog audit'],
    ['duplicate_reconcile', 'Duplicate reconciliation'], ['performer_merge', 'Merge performers'],
    ['availability_check', 'Check availability'], ['metadata_update', 'Update metadata'],
    ['metadata_backfill', 'External metadata scan'], ['collections_refresh', 'Update collections'],
    ['audit_repair_nfo', 'Repair catalog files'], ['audit_import_refresh', 'Request Jellyfin refresh'],
  ]);
  const failureMessages = new Map([
    ['operation_timeout', 'A required operation timed out.'], ['connection_failed', 'Could not connect to a required service.'],
    ['permission_denied', 'Access to a required resource was denied.'], ['file_missing', 'A required file was not found.'],
    ['stale_operation', 'The operation no longer matches current data.'], ['remote_service_failed', 'A required service returned an unsuccessful response.'],
  ]);
  const states = new Map([
    ['healthy', 'Healthy'], ['unavailable', 'Unavailable'], ['not_configured', 'Not configured'], ['unknown', 'Unknown'],
  ]);
  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = String(text);
    return node;
  };
  const date = value => {
    const numeric = typeof value === 'number' && value < 100000000000 ? value * 1000 : value;
    const parsed = value === null || value === undefined ? null : new Date(numeric);
    return parsed && !Number.isNaN(parsed.getTime()) ? parsed.toLocaleString('en-US') : 'Unavailable';
  };
  async function loadSystemStatus() {
    const seq = ++requestSeq;
    const host = $('#systemProbeRows');
    if (!host) return;
    host.replaceChildren(el('p', 'catalog-feedback', 'Checking services…'));
    try {
      const [payload, taskResult] = await Promise.all([
        window.AdminV2.getJson('/admin/api/v2/diagnostics/probes').then(value => ({ value }), error => ({ error })),
        window.AdminV2.getJson('/admin/api/v2/tasks?queue=all&state=failed&limit=10&offset=0').then(value => ({ value }), error => ({ error })),
      ]);
      if (seq !== requestSeq || !$('#systemStatusView') || $('#systemStatusView').hidden) return;
      host.replaceChildren();
      if (payload.error || !Array.isArray(payload.value?.services)) {
        host.append(el('p', 'catalog-state', 'Service status is unknown. The latest check could not be completed.'));
      } else {
        for (const service of payload.value.services) {
          if (!service || !labels.has(service.service)) continue;
          const status = states.get(service.status) || 'Unknown';
          const row = el('div', 'operation-history-row');
          const copy = el('div'); copy.append(el('strong', '', labels.get(service.service)));
          const detail = [status];
          if (Number.isInteger(service.elapsed_ms) && service.elapsed_ms >= 0) detail.push(`${service.elapsed_ms} ms`);
          if (Number.isInteger(service.http_status) && service.http_status >= 100 && service.http_status <= 599) detail.push(`HTTP ${service.http_status}`);
          copy.append(el('p', '', detail.join(' · ')));
          row.append(copy, el('span', `status-pill ${status === 'Healthy' ? 'pill-success' : status === 'Unavailable' ? 'pill-warning' : 'pill-neutral'}`, status));
          host.append(row);
        }
        if (!host.children.length) host.append(el('p', 'catalog-empty', 'Service status is unknown. No probe results were returned.'));
      }
      const checkedAt = payload.value?.checked_at;
      $('#systemStatusChecked').textContent = `Last checked: ${date(checkedAt)}${Number.isInteger(payload.value?.cache_age_seconds) && payload.value.cache_age_seconds > 0 ? ` · cached ${payload.value.cache_age_seconds}s` : ''}.`;
      const failures = $('#systemRecentFailures'); failures.replaceChildren();
      if (taskResult.error || !Array.isArray(taskResult.value?.tasks)) {
        failures.append(el('p', 'catalog-state', 'Recent task failures are unavailable.'));
      } else {
        const rows = taskResult.value.tasks.slice(0, 10);
        if (!rows.length) failures.append(el('p', 'catalog-empty', 'No unresolved task failures.'));
        for (const task of rows) {
          const row = el('div', 'operation-history-row');
          const copy = el('div');
          const action = String(task.action || 'task');
          const title = task.display_name || task.title || actions.get(action) || action.replaceAll('_', ' ').replace(/^./, char => char.toUpperCase());
          const error = task.error || task.error_message || failureMessages.get(task.error_code) || task.error_code || 'Failure details unavailable';
          copy.append(el('strong', '', title));
          copy.append(el('p', '', `${error} · ${date(task.updated_at || task.created_at)}`));
          const link = el('a', 'button button-quiet', 'View task');
          link.href = `/admin/v2/?job_id=${encodeURIComponent(task.id)}&job_queue=${encodeURIComponent(task.queue || 'jobs')}#jobs`;
          row.append(copy, link); failures.append(row);
        }
      }
    } catch {
      if (seq === requestSeq) host.replaceChildren(el('p', 'catalog-state', 'Service status is unknown. The latest check could not be completed.'));
    }
  }
  $('#systemStatusRefresh')?.addEventListener('click', () => {
    document.dispatchEvent(new CustomEvent('adminv2:refresh-system-status'));
  });
  document.addEventListener('adminv2:refresh-system-status', () => { void loadSystemStatus(); });
  document.addEventListener('adminv2:navigate', event => {
    if (event.detail?.route === 'system' && event.detail?.systemTab === 'status') void loadSystemStatus();
    else requestSeq += 1;
  });
  if (window.AdminV2.currentRoute().route === 'system' && window.AdminV2.currentRoute().systemTab === 'status') void loadSystemStatus();
})();
