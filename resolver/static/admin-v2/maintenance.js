(() => {
  'use strict';
  const api = '/admin/api/v2/maintenance';
  const feedback = document.querySelector('#maintenanceFeedback');

  async function run(button, request, message, jobIdForResult) {
    button.disabled = true;
    feedback.textContent = 'Sending request…';
    try {
      const result = await request();
      const jobId = jobIdForResult?.(result);
      feedback.replaceChildren(document.createTextNode(message(result)));
      if (typeof jobId === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(jobId)) {
        const link = document.createElement('a');
        link.href = `/admin/v2/?job_id=${encodeURIComponent(jobId)}#jobs`;
        link.textContent = `Open task ${jobId}`;
        feedback.append(document.createTextNode(' '), link);
      }
    } catch (error) {
      feedback.textContent = `Maintenance action failed. ${error.message}`;
    } finally {
      button.disabled = false;
    }
  }

  const fullSync = document.querySelector('#triggerFullSync');
  fullSync.addEventListener('click', () => {
    if (!window.confirm('Queue a full synchronization for every enabled source? This can make many source requests and create a large number of jobs.')) return;
    return run(fullSync, () => window.AdminV2.postJson(`${api}/sync`, { source: null, sync_mode: 'scheduled' }), result =>
      `Full sync queued as job ${result.job_id}.`, result => result.job_id);
  });

  const clearCache = document.querySelector('#clearResolverCache');
  clearCache.addEventListener('click', () => {
    if (!window.confirm('Clear the resolver cache? The next playback request may need to resolve a fresh stream.')) return;
    return run(clearCache, () => window.AdminV2.postJson(`${api}/cache/clear`, {}), result =>
      `Resolver cache cleared (${result.cleared} entries).`);
  });
})();
