(() => {
  'use strict';

  function access(capabilities) {
    if (!capabilities || typeof capabilities.actions_enabled !== 'boolean') {
      return { state: 'unknown', label: 'Access unavailable', title: 'Admin access could not be verified.' };
    }
    if (!capabilities.actions_enabled) {
      const reason = capabilities.read_only_reason;
      const title = reason === 'worker_unavailable'
        ? 'Writes are paused because the sync worker is unavailable.'
        : reason === 'worker_incompatible'
          ? 'Writes are paused because the sync worker does not support all required actions.'
          : 'Writes are disabled by the current admin configuration.';
      return { state: 'readonly', label: 'Read-only', title };
    }
    if (capabilities.token_required === true) {
      return { state: 'token', label: 'Writes require token', title: 'Write actions require the configured admin token.' };
    }
    return { state: 'writable', label: 'Read & write', title: 'Read and write actions are enabled.' };
  }

  function activeTasks(payload) {
    const count = payload && payload.active_total;
    if (!payload || !payload.data || payload.data.available !== true
        || !Number.isSafeInteger(count) || count < 0) {
      return { state: 'unknown', label: 'Active tasks unavailable', count: null };
    }
    return { state: 'ready', label: `Active tasks: ${count}`, count };
  }

  function syncStatusText(sync) {
    if (!sync || sync.available === false) return 'No data';
    if (sync.running === true) return 'Sync in progress';
    if (['fresh', 'stale', 'unknown'].includes(sync.freshness)) {
      const outcome = sync.last_success === true ? 'Last run succeeded'
        : sync.last_success === false ? 'Last run failed' : 'No data';
      if (sync.freshness === 'stale') return `Status stale · ${outcome}`;
      if (sync.freshness === 'unknown') {
        return outcome === 'No data' ? outcome : `Freshness unknown · ${outcome}`;
      }
      return outcome;
    }
    if (sync.last_success === true) return 'Last run succeeded';
    if (sync.last_success === false) return 'Last run failed';
    return typeof sync.status_text === 'string' && sync.status_text ? sync.status_text : 'No data';
  }

  function syncFreshness(sync) {
    if (!sync || sync.available !== true) {
      return { state: 'unknown', label: 'Sync status unavailable', title: 'Synchronization freshness could not be verified.' };
    }
    if (sync.running === true) {
      return { state: 'running', label: 'Sync running', title: 'A synchronization run is in progress.' };
    }
    if (sync.freshness === 'stale') {
      return { state: 'stale', label: 'Sync stale', title: 'The last synchronization is older than the configured schedule.' };
    }
    if (sync.freshness === 'fresh' && sync.last_success === false) {
      return { state: 'failed', label: 'Sync failed', title: 'The latest synchronization run failed.' };
    }
    if (sync.freshness === 'fresh' && sync.last_success === true) {
      return { state: 'fresh', label: 'Sync current', title: 'The latest synchronization completed successfully within the configured schedule.' };
    }
    return { state: 'unknown', label: 'Sync freshness unknown', title: 'The synchronization freshness could not be determined.' };
  }

  window.AdminV2HeaderStatus = Object.freeze({ access, activeTasks, syncStatusText, syncFreshness });
})();
