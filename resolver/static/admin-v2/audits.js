(() => {
  'use strict';
  const api = '/admin/api/v2/audits';
  const $ = selector => document.querySelector(selector);
  let auditListRequestId = 0;
  let findingsRequestId = 0;
  let backupListRequestId = 0;
  let auditPollTimer = null;
  let auditListLoading = false;
  let auditViewActive = false;
  const el = (tag, className, value) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value !== undefined) node.textContent = String(value);
    return node;
  };
  const setActionFeedback = message => {
    const feedback = $('#auditActionFeedback');
    feedback.textContent = message;
    feedback.hidden = !message;
  };
  const when = value => value ? new Date(value).toLocaleString('en-US') : '—';
  const importState = value => ({
    checked: 'Jellyfin import hashes checked',
    partial: 'Jellyfin import check partial (ambiguous file mapping)',
    unknown: 'Jellyfin import check unavailable',
  }[value] || 'Jellyfin import check pending');
  const inventoryStatus = audit => {
    if (!['complete', 'partial'].includes(audit?.library_inventory_status)) return '';
    const count = Number.isInteger(audit.library_inventory_entries) ? audit.library_inventory_entries.toLocaleString('en-US') : 'an unknown number of';
    return audit.library_inventory_status === 'complete'
      ? `Managed library inventory complete: ${count} entries scanned.`
      : `Managed library inventory is partial: ${count} entries scanned. Some orphaned files may not appear in this report.`;
  };
  const catalogCoverage = audit => {
    if (['queued', 'processing'].includes(audit?.state)
        && Number.isInteger(audit.progress_completed) && Number.isInteger(audit.progress_total)) {
      const stage = String(audit.stage || 'working').replaceAll('_', ' ');
      return `${stage}: ${audit.progress_completed.toLocaleString('en-US')} of ${audit.progress_total.toLocaleString('en-US')}`;
    }
    if (!Number.isInteger(audit.checked)) return '';
    const checked = audit.checked.toLocaleString('en-US');
    return Number.isInteger(audit.catalog_total)
      ? `${checked} of ${audit.catalog_total.toLocaleString('en-US')} catalog entries checked`
      : `${checked} catalog entries checked`;
  };

  async function loadAudits() {
    if (auditListLoading) return;
    const requestId = ++auditListRequestId;
    auditListLoading = true;
    const feedback = $('#auditFeedback');
    feedback.textContent = 'Loading audit history…';
    try {
      const result = await window.AdminV2.getJson(`${api}?limit=20&offset=0`);
      if (requestId !== auditListRequestId) return;
      const rows = $('#auditRows'); rows.replaceChildren();
      if (!result.items.length) rows.append(el('p', 'catalog-empty', 'No audit runs are available yet.'));
      result.items.forEach(item => {
        const card = el('div', 'operation-history-row');
        const copy = el('div');
        const modeLabel = ({ metadata_quality: 'Metadata quality', file_consistency: 'File consistency', full: 'Full audit' })[item.mode] || 'Legacy full audit';
        const scopeLabel = ({ metadata_quality: 'Metadata job', file_consistency: 'File consistency job', full: 'Combined legacy job' })[item.scope] || '';
        copy.append(el('strong', '', `${modeLabel}${scopeLabel && item.mode === 'full' ? ` · ${scopeLabel}` : ''} · ${item.state} · ${item.finding_count} finding${item.finding_count === 1 ? '' : 's'}`));
        const coverage = catalogCoverage(item);
        copy.append(el('p', '', `Started ${when(item.created_at)} · Job ${item.id}${item.audit_batch_id ? ` · Full audit group ${item.audit_batch_id.slice(0, 8)}` : ''}${coverage ? ` · ${coverage}` : ''}${item.jellyfin_import_state ? ` · ${importState(item.jellyfin_import_state)}` : ''}`));
        const inventory = inventoryStatus(item);
        if (inventory) copy.append(el('small', '', inventory));
        const details = el('button', 'button button-quiet', 'View findings');
        details.type = 'button'; details.addEventListener('click', () => loadFindings(item.id));
        const task = el('a', 'button button-quiet', 'Open task');
        task.href = `/admin/v2/?job_id=${encodeURIComponent(item.id)}#jobs`;
        card.append(copy, details, task); rows.append(card);
      });
      feedback.textContent = `${result.total} audit${result.total === 1 ? '' : 's'} recorded.`;
    } catch (error) {
      if (requestId === auditListRequestId) feedback.textContent = `Could not load audit history. ${error.message}`;
    } finally { auditListLoading = false; }
  }

  function setAuditViewActive(active) {
    auditViewActive = Boolean(active);
    if (auditPollTimer !== null) clearInterval(auditPollTimer);
    auditPollTimer = null;
    if (auditViewActive) {
      void loadAudits();
      auditPollTimer = setInterval(() => {
        if (auditViewActive && !document.hidden && !auditListLoading) void loadAudits();
      }, 15000);
    }
  }

  async function loadFindings(id, offset = 0, focusDirection = '') {
    const requestId = ++findingsRequestId;
    const host = $('#auditFindings'); host.replaceChildren(el('p', 'catalog-feedback', 'Loading findings…'));
    try {
      const limit = 50;
      const result = await window.AdminV2.getJson(`${api}/${encodeURIComponent(id)}?limit=${limit}&offset=${offset}`);
      if (requestId !== findingsRequestId) return;
      const modeLabel = ({ metadata_quality: 'Metadata quality', file_consistency: 'File consistency', full: 'Full audit' })[result.audit.mode] || 'Legacy full audit';
      const scopeLabel = ({ metadata_quality: 'Metadata quality job', file_consistency: 'File consistency job', full: 'Combined legacy job' })[result.audit.scope] || '';
      host.replaceChildren(el('h3', '', `${modeLabel}${scopeLabel && result.audit.mode === 'full' ? ` · ${scopeLabel}` : ''} findings · ${result.total}`));
      const coverage = catalogCoverage(result.audit);
      if (coverage) host.append(el('p', 'catalog-feedback', coverage));
      const evidenceByFinding = new Map(result.items.map(item => [item.id, item.evidence]));
      if (result.audit.jellyfin_import_state) {
        host.append(el('p', 'catalog-feedback', importState(result.audit.jellyfin_import_state)));
      }
      const inventory = inventoryStatus(result.audit);
      if (inventory) host.append(el('p', result.audit.library_inventory_status === 'partial' ? 'catalog-state' : 'catalog-feedback', inventory));
      if (!result.items.length) host.append(el('p', 'catalog-empty', 'No findings were recorded for this audit run.'));
      const eligible = result.items.filter(item => item.status === 'open' &&
        ['missing_stream', 'missing_nfo', 'invalid_nfo', 'nfo_metadata_mismatch', 'missing_artwork'].includes(item.kind) && item.video_id);
      const orphanEligible = result.items.filter(item => item.status === 'open' && item.kind === 'orphan_file'
        && Array.isArray(item.details?.files) && item.details.files.length > 0);
      const importEligible = result.items.filter(item => item.status === 'open' &&
        ['nfo_import_state_missing', 'nfo_import_state_mismatch'].includes(item.kind) && item.video_id);
      const selectedNfo = new Set(), selectedImport = new Set(), selectedOrphans = new Set();
      const repairChecks = [], importChecks = [], orphanChecks = [];
      const repairBar = el('div', 'operation-history-row');
      const repairButton = el('button', 'button button-primary', 'Preview local file repair');
      repairButton.type = 'button'; repairButton.disabled = true;
      repairButton.addEventListener('click', () => previewRepair(id, [...selectedNfo], repairButton, 'nfo', evidenceByFinding));
      const importButton = el('button', 'button button-quiet', 'Request Jellyfin reimport');
      importButton.type = 'button'; importButton.disabled = true;
      importButton.addEventListener('click', () => previewRepair(id, [...selectedImport], importButton, 'import', evidenceByFinding));
      const orphanButton = el('button', 'button button-quiet', 'Preview orphan quarantine');
      orphanButton.type = 'button'; orphanButton.disabled = true;
      orphanButton.addEventListener('click', () => previewOrphanQuarantine(id, [...selectedOrphans], orphanButton));
      repairBar.append(el('p', '', 'Restore selected registered stream files, regenerate selected NFO files, or restore missing artwork from the current catalog thumbnail. Existing artwork is never overwritten. Repair up to 25 findings per request. You can also request a Jellyfin refresh for selected import findings; a refresh request does not confirm that Jellyfin completed the import.'), repairButton, importButton);
      const updateSelection = (selection, controls, button) => {
        button.disabled = selection.size === 0;
        controls.forEach(([control, id]) => { control.disabled = selection.size >= 25 && !selection.has(id); });
      };
      if (orphanEligible.length) {
        repairBar.append(el('p', '', 'Move selected unregistered STRM, NFO, and JPG files into reversible quarantine. Same-directory, same-basename files move together. Paths are shown relative to the media library; files are never permanently deleted. Only complete inventory audits can be used.'));
        repairBar.append(orphanButton);
      }
      if (eligible.length || importEligible.length || orphanEligible.length) host.append(repairBar);
      result.items.forEach(item => {
        const card = el('div', 'operation-history-row');
        if (item.status === 'open' && eligible.includes(item)) {
          const check = document.createElement('input'); check.type = 'checkbox'; check.setAttribute('aria-label', `Select ${item.kind.replaceAll('_', ' ')} repair`);
          repairChecks.push([check, item.id]);
          check.addEventListener('change', () => { check.checked ? selectedNfo.add(item.id) : selectedNfo.delete(item.id); updateSelection(selectedNfo, repairChecks, repairButton); });
          card.append(check);
        } else if (item.status === 'open' && importEligible.includes(item)) {
          const check = document.createElement('input'); check.type = 'checkbox'; check.setAttribute('aria-label', `Select ${item.kind.replaceAll('_', ' ')} reimport`);
          importChecks.push([check, item.id]);
          check.addEventListener('change', () => { check.checked ? selectedImport.add(item.id) : selectedImport.delete(item.id); updateSelection(selectedImport, importChecks, importButton); });
          card.append(check);
        } else if (item.status === 'open' && orphanEligible.includes(item)) {
          const check = document.createElement('input'); check.type = 'checkbox'; check.setAttribute('aria-label', `Select orphan file family ${item.id} for quarantine`);
          orphanChecks.push([check, item.id]);
          check.addEventListener('change', () => { check.checked ? selectedOrphans.add(item.id) : selectedOrphans.delete(item.id); updateSelection(selectedOrphans, orphanChecks, orphanButton); });
          card.append(check);
        }
        card.append(el('strong', '', item.kind.replaceAll('_', ' ')));
        card.append(el('p', '', `${item.status} · ${item.video_id || 'Catalog reference'} · ${item.evidence}`));
        if (item.kind === 'orphan_file' && Array.isArray(item.details?.files)) {
          const visiblePaths = item.details.files.map(file => `${file.path} (${file.size.toLocaleString('en-US')} bytes)`);
          if (item.details.file_count > item.details.files.length) visiblePaths.push(`${item.details.file_count - item.details.files.length} more file(s)`);
          card.append(el('small', '', visiblePaths.join(' · ')));
        }
        card.append(el('small', '', when(item.checked_at))); host.append(card);
      });
      if (result.total > limit) {
        const start = result.items.length ? offset + 1 : 0;
        const end = Math.min(offset + result.items.length, result.total);
        const nav = el('div', 'audit-pagination');
        const previous = el('button', 'button button-quiet', 'Previous');
        previous.type = 'button'; previous.disabled = offset === 0;
        previous.setAttribute('aria-label', 'Previous findings page');
        previous.addEventListener('click', () => loadFindings(id, Math.max(0, offset - limit), 'previous'));
        const status = el('span', 'catalog-feedback', `Showing ${start}–${end} of ${result.total} findings`);
        status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
        const next = el('button', 'button button-quiet', 'Next');
        next.type = 'button'; next.disabled = offset + result.items.length >= result.total;
        next.setAttribute('aria-label', 'Next findings page');
        next.addEventListener('click', () => loadFindings(id, offset + limit, 'next'));
        nav.append(previous, status, next); host.append(nav);
        if (focusDirection) nav.querySelector(`[aria-label="${focusDirection === 'next' ? 'Next' : 'Previous'} findings page"]`).focus();
      }
      host.scrollIntoView({ block: 'nearest' });
    } catch (error) {
      if (requestId === findingsRequestId) host.replaceChildren(el('p', 'catalog-state', `Could not load audit findings. ${error.message}`));
    }
  }

  async function previewRepair(auditId, findingIds, button, action, evidenceByFinding) {
    button.disabled = true;
    try {
      const endpoint = action === 'nfo' ? 'repairs' : 'reimports';
      const preview = await window.AdminV2.postJson(`${api}/${encodeURIComponent(auditId)}/${endpoint}/preview`, { finding_ids: findingIds });
      const itemSummary = preview.items.map(item => {
        const findings = [...new Set((item.findings || []).map(finding => {
          const label = finding.kind.replaceAll('_', ' ');
          const evidence = evidenceByFinding.get(finding.finding_id);
          return evidence ? `${label}: ${evidence}` : label;
        }))].join(', ');
        const files = `${item.file_count} registered file${item.file_count === 1 ? '' : 's'}`;
        return `${item.title} · ${findings || `${item.finding_count} selected findings`} · ${files}`;
      }).join('\n');
      const confirmation = action === 'nfo'
        ? `Repair the selected registered local files for these catalog items? This may restore a missing STRM from its catalog registration or regenerate NFO sidecars.\n\n${itemSummary}\n\nThis also requests a Jellyfin library refresh.`
        : `Request a Jellyfin library refresh for these import findings?\n\n${itemSummary}\n\nThe audit finding remains open until a later audit confirms the import.`;
      if (!window.confirm(confirmation)) return;
      const queued = await window.AdminV2.postJson('/admin/api/v2/operations/execute', { plan_id: preview.plan_id });
      const job = queued.jobs?.[0]?.job_id || 'queued';
      await loadAudits();
      setActionFeedback(action === 'nfo'
        ? `Local file repair queued (${job}). The worker will recheck the selected findings and current metadata before writing.`
        : `Jellyfin refresh queued (${job}). Import confirmation is not implied; run a new audit after Jellyfin finishes.`);
    } catch (error) {
      setActionFeedback(`Could not queue ${action === 'nfo' ? 'local file repair' : 'Jellyfin reimport'}. ${error.message}`);
    } finally { button.disabled = false; }
  }

  async function previewOrphanQuarantine(auditId, findingIds, button) {
    button.disabled = true;
    try {
      const preview = await window.AdminV2.postJson('/admin/api/v2/operations/preview',
        { type: 'orphan_file_quarantine', audit_id: auditId, finding_ids: findingIds });
      const summary = preview.items.map(item => item.files.map(file => file.path).join('\n')).join('\n\n');
      if (!window.confirm(`Move ${preview.items.length} selected orphan file famil${preview.items.length === 1 ? 'y' : 'ies'} (${preview.items.reduce((sum, item) => sum + item.files.length, 0)} files) to reversible quarantine?\n\n${summary}\n\nNo files will be permanently deleted.`)) return;
      const queued = await window.AdminV2.postJson('/admin/api/v2/operations/execute', { plan_id: preview.plan_id });
      const job = queued.jobs?.[0]?.job_id || 'queued';
      setActionFeedback(`Orphan files are being moved to quarantine (${job}). Refresh Organize → Quarantine after the worker finishes.`);
      await loadAudits();
    } catch (error) {
      setActionFeedback(`Could not quarantine orphan files. ${error.message}`);
    } finally { button.disabled = false; }
  }

  async function loadBackups() {
    const requestId = ++backupListRequestId;
    const feedback = $('#backupFeedback'); feedback.textContent = 'Loading backup inventory…';
    try {
      const result = await window.AdminV2.getJson('/admin/api/v2/backups');
      if (requestId !== backupListRequestId) return;
      const host = $('#backupRows'); host.replaceChildren();
      if (result.inventory_available === false) {
        host.append(el('p', 'catalog-state', 'Backup inventory is unavailable. Check the configured backup directory and its permissions.'));
        feedback.textContent = 'Could not read the configured backup inventory. No backup freshness conclusion is available.';
        return;
      }
      const restoreTestAvailable = result.restore_test_available === true;
      if (!result.items.length) host.append(el('p', 'catalog-empty', 'No database backups were found.'));
      result.items.forEach((item, index) => {
        const card = el('div', 'operation-history-row'); const copy = el('div');
        copy.append(el('strong', '', item.name));
        if (index === 0 && result.latest && result.latest.name === item.name) {
          copy.append(el('span', 'status-pill pill-neutral', 'Latest snapshot'));
        }
        copy.append(el('p', '', `${item.backup_type.replaceAll('_', ' ')} · ${(item.size_bytes / (1024 * 1024)).toFixed(1)} MB · ${when(item.modified_at * 1000)}`));
        copy.append(el('small', '', `${item.scope} · checksum ${item.checksum_status}`));
        const verify = el('button', 'button button-quiet', 'Verify'); verify.type = 'button';
        verify.addEventListener('click', async () => {
          verify.disabled = true;
          try {
            const checked = await window.AdminV2.getJson(`/admin/api/v2/backups/verify?filename=${encodeURIComponent(item.name)}`);
            copy.append(el('p', '', `Archive ${checked.archive_status}; checksum ${checked.checksum_status}. SHA-256: ${checked.checksum}`));
          } catch (error) { copy.append(el('p', 'catalog-state', `Verification failed. ${error.message}`)); }
          finally { verify.disabled = false; }
        });
        card.append(copy, verify);
        if (item.backup_type === 'postgres_custom_dump') {
          const restore = el('button', 'button button-quiet', 'Test restore'); restore.type = 'button';
          restore.disabled = item.checksum_status !== 'valid' || !restoreTestAvailable;
          if (!restoreTestAvailable || item.checksum_status !== 'valid') {
            copy.append(el('small', 'restore-test-reason', !restoreTestAvailable
              ? 'Restore test unavailable: configure a dedicated disposable database.'
              : 'Restore test unavailable: verify a valid backup checksum first.'));
          }
          restore.title = !restoreTestAvailable
            ? 'A dedicated disposable restore-test database must be configured.'
            : item.checksum_status !== 'valid'
              ? 'A valid checksum is required.'
              : 'Restore into a temporary database and remove it after validation.';
          restore.addEventListener('click', async () => {
            if (!window.confirm(`Test restoring ${item.name} into a temporary disposable database? The configured source database will not be changed; a temporary database will be created and removed after validation.`)) return;
            restore.disabled = true;
            try {
              const queued = await window.AdminV2.postJson('/admin/api/v2/backups/restore-test', { filename: item.name });
              feedback.textContent = `Restore test queued (${queued.job_id}). It uses a temporary database and does not change the source database.`;
            } catch (error) { feedback.textContent = `Could not queue restore test. ${error.message}`; }
            finally { restore.disabled = false; }
          });
          card.append(restore);
        }
        host.append(card);
      });
      const count = Number.isSafeInteger(result.count) && result.count >= 0 ? result.count : result.items.length;
      const total = Number.isSafeInteger(result.total_bytes) && result.total_bytes >= 0
        ? ` · ${(result.total_bytes / (1024 * 1024)).toFixed(1)} MB total` : '';
      const capped = count > result.items.length ? ` · showing the newest ${result.items.length}` : '';
      feedback.textContent = `${count} backup${count === 1 ? '' : 's'} found${total}${capped}. Verification reads the archive without restoring it.${result.items.some(item => item.backup_type === 'postgres_custom_dump') && !restoreTestAvailable ? ' Restore tests are disabled until a dedicated disposable database is configured.' : ''}`;
    } catch (error) {
      if (requestId === backupListRequestId) feedback.textContent = `Could not load backup inventory. ${error.message}`;
    }
  }

  async function createBackup() {
    const button = $('#backupCreate');
    if (!window.confirm('Create a database backup now? This saves a PostgreSQL database snapshot to the configured backup directory. It does not include media files, artwork, or application settings.')) return;
    button.disabled = true;
    const feedback = $('#backupFeedback');
    feedback.textContent = 'Queueing database backup…';
    try {
      const queued = await window.AdminV2.postJson('/admin/api/v2/backups/create', {});
      feedback.textContent = `Database backup queued (${queued.job_id}). Check Jobs for completion, then refresh this inventory.`;
    } catch (error) {
      feedback.textContent = `Could not queue database backup. ${error.message}`;
    } finally { button.disabled = false; }
  }

  $('#auditRefresh').addEventListener('click', loadAudits);
  $('#backupRefresh').addEventListener('click', loadBackups);
  $('#backupCreate').addEventListener('click', createBackup);
  document.addEventListener('adminv2:navigate', event => {
    setAuditViewActive(event.detail?.route === 'quality' && (event.detail?.tab || 'issues') === 'issues');
    if (event.detail?.route === 'system' && event.detail?.systemTab === 'backups') loadBackups();
  });
  document.addEventListener('adminv2:audits-updated', () => { if (auditViewActive) void loadAudits(); });
  const initialHashParts = location.hash.replace(/^#/, '').split('?', 2);
  const initialQualityTab = new URLSearchParams(initialHashParts[1] || '').get('tab') || 'issues';
  if (initialHashParts[0] === 'quality' && initialQualityTab === 'issues') setAuditViewActive(true);
  const initialSystemTab = new URLSearchParams(location.hash.split('?')[1] || '').get('tab') || 'maintenance';
  if (location.hash.startsWith('#system') && initialSystemTab === 'backups') loadBackups();
})();
