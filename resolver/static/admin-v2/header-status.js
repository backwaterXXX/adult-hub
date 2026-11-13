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
    if (payload === undefined || (payload && payload.checking === true)) {
      return { state: 'checking', label: 'Checking tasks…', count: null };
    }
    const count = payload && payload.active_total;
    if (!payload || !payload.available || !Number.isSafeInteger(count) || count < 0) {
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

  // The header shows one aggregate control instead of four separate chips, so
  // the combined state must never look healthier than its weakest source.
  function serviceStatus(snapshot) {
    if (!snapshot || snapshot.checking === true) {
      return { state: 'checking', label: 'Checking services…', title: 'Service status is still loading.' };
    }
    const accessState = access(snapshot.capabilities);
    const syncState = syncFreshness(snapshot.sync);
    const taskState = activeTasks(snapshot.activeTasks);
    const details = [accessState, syncState, taskState];
    if (snapshot.connection === false) {
      return {
        state: 'error',
        label: 'Connection unavailable',
        title: 'The admin status endpoints could not be read. Displayed data may be out of date.',
      };
    }
    if (details.every(item => item.state === 'unknown')) {
      return { state: 'unknown', label: 'Status unverified', title: 'No admin status source could be verified.' };
    }
    const attention = details.filter(item => (
      item.state === 'readonly' || item.state === 'stale' || item.state === 'failed'));
    if (attention.length) {
      return { state: 'attention', label: 'Attention needed', title: attention.map(item => item.title).join(' ') };
    }
    if (details.some(item => item.state === 'checking')) {
      return { state: 'checking', label: 'Checking services…', title: 'Service status is still loading.' };
    }
    if (details.some(item => item.state === 'unknown')) {
      const missing = details.filter(item => item.state === 'unknown').map(item => item.label);
      return { state: 'partial', label: 'Partial status', title: `${missing.join(', ')} could not be verified.` };
    }
    return { state: 'ok', label: 'All services available', title: details.map(item => item.label).join(' · ') };
  }

  window.AdminV2HeaderStatus = Object.freeze({ access, activeTasks, syncStatusText, syncFreshness, serviceStatus });
})();
