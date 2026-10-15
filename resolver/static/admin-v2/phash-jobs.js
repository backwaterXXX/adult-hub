(() => {
  'use strict';
  const API = '/admin/api/v2/phash-jobs';
  const $ = (id) => document.getElementById(id);
  const escape = (value) => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  function formatDuration(ms, state) {
    const total = Math.max(0, Math.floor(Number(ms) / 1000));
    if (!total) return state === 'processing' ? '0s' : '—';
    const hours = Math.floor(total / 3600), minutes = Math.floor((total % 3600) / 60), seconds = total % 60;
    if (hours) return `${hours}h ${String(minutes).padStart(2, '0')}m`;
    if (minutes) return `${minutes}m ${String(seconds).padStart(2, '0')}s`;
    return `${seconds}s`;
  }
  let offset = 0, total = 0, timer = null, busy = false;

  async function getJson(url) {
    const response = await fetch(url, { credentials: 'same-origin', headers: { Accept: 'application/json' } });
    const body = await response.json().catch(() => null);
    if (!response.ok || !body) throw new Error(body?.error || `Request failed (HTTP ${response.status}).`);
    return body;
  }
  async function postJson(url) {
    const capabilities = await getJson('/admin/api/v2/capabilities');
    const headers = { Accept: 'application/json' };
    if (capabilities.token_required === true) {
      let token = sessionStorage.getItem('adminWriteToken') || sessionStorage.getItem('duplicateActionToken') || '';
      if (!token) token = prompt('Enter the admin write token:') || '';
      if (!token.trim()) throw new Error('An admin write token is required.');
      sessionStorage.setItem('adminWriteToken', token.trim());
      headers.Authorization = `Bearer ${token.trim()}`;
    }
    const response = await fetch(url, { method: 'POST', credentials: 'same-origin', headers });
    const body = await response.json().catch(() => null);
    if (!response.ok || !body) throw new Error(body?.error || `Request failed (HTTP ${response.status}).`);
    return body;
  }
  function active() {
    const [route, query = ''] = location.hash.replace(/^#\/?/, '').split('?', 2);
    const tab = new URLSearchParams(query).get('tab');
    return route === 'jobs' && !['bulk', 'schedules'].includes(tab);
  }
  async function load() {
    if (busy || !active()) return;
    busy = true;
    const feedback = $('phashJobsFeedback');
    try {
      const state = $('phashJobState').value;
      const query = new URLSearchParams({ limit: '25', offset: String(offset) });
      if (state) query.set('state', state);
      const data = await getJson(`${API}?${query}`);
      const worker = data.worker || {};
      $('phashWorkerStatus').textContent = worker.available
        ? `pHash worker online · ${worker.worker_version} · heartbeat ${worker.age_seconds}s ago`
        : 'pHash worker unavailable. Jobs remain safely queued until it returns.';
      feedback.textContent = `${data.active_total} active · ${data.total} matching jobs`;
      total = data.total;
      $('phashJobsRows').innerHTML = data.jobs.map(job => {
        const actions = ['queued', 'processing'].includes(job.state)
          ? `<button class="button button-quiet" type="button" data-phash-action="cancel" data-job="${escape(job.id)}">Cancel</button>`
          : ['failed', 'cancelled'].includes(job.state)
            ? `<button class="button button-quiet" type="button" data-phash-action="retry" data-job="${escape(job.id)}">Retry</button>` : '';
        return `<tr><td>${escape(job.title || job.video_id)}<small>${escape(job.video_id)}</small></td><td>${escape(job.state)}</td><td>${formatDuration(job.processing_time_ms, job.state)}</td><td>${escape(job.attempts)}</td><td>${job.thumbnail_ready ? 'Ready' : 'Missing'}</td><td>${escape(job.error_code || '—')}</td><td>${actions}</td></tr>`;
      }).join('') || '<tr><td colspan="7">No pHash jobs found.</td></tr>';
      const page = Math.floor(offset / 25) + 1;
      $('phashJobsPageInfo').textContent = `Page ${page} · ${total} jobs`;
      $('phashJobsPagination').hidden = total <= 25;
      $('phashJobsPrev').disabled = offset === 0;
      $('phashJobsNext').disabled = offset + 25 >= total;
    } catch (error) {
      feedback.textContent = error.message || 'Could not load pHash queue.';
    } finally { busy = false; }
  }
  async function action(button) {
    button.disabled = true;
    try {
      await postJson(`${API}/${encodeURIComponent(button.dataset.job)}/${button.dataset.phashAction}`);
      await load();
    } catch (error) {
      $('phashJobsFeedback').textContent = error.message || 'Could not update pHash job.';
      button.disabled = false;
    }
  }
  $('phashJobsRefresh')?.addEventListener('click', () => load());
  $('phashJobState')?.addEventListener('change', () => { offset = 0; load(); });
  $('phashJobsPrev')?.addEventListener('click', () => { offset = Math.max(0, offset - 25); load(); });
  $('phashJobsNext')?.addEventListener('click', () => { if (offset + 25 < total) offset += 25; load(); });
  $('phashJobsRows')?.addEventListener('click', (event) => {
    const button = event.target.closest('[data-phash-action]');
    if (button) action(button);
  });
  function syncPolling() {
    if (timer) clearInterval(timer);
    timer = active() ? setInterval(load, 15_000) : null;
    if (active()) load();
  }
  window.addEventListener('hashchange', syncPolling);
  window.addEventListener('popstate', syncPolling);
  document.addEventListener('adminv2:navigate', syncPolling);
  syncPolling();
})();
