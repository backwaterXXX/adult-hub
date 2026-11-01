(() => {
  'use strict';
  const $ = selector => document.querySelector(selector);
  let backupListRequestId = 0;
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
      feedback.textContent = `Database backup queued (${queued.job_id}). Check Jobs for completion; Backups reloads when you reopen the tab.`;
    } catch (error) {
      feedback.textContent = `Could not queue database backup. ${error.message}`;
    } finally { button.disabled = false; }
  }

  $('#backupCreate')?.addEventListener('click', createBackup);
  document.addEventListener('adminv2:navigate', event => {
    if (event.detail?.route === 'system' && event.detail?.systemTab === 'backups') loadBackups();
  });
  const initial = window.AdminV2.currentRoute();
  if (initial.route === 'system' && initial.systemTab === 'backups') loadBackups();
})();
