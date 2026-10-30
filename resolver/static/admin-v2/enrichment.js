(() => {
  'use strict';
  const endpoint = '/admin/api/v2/metadata/enrichment';
  const scanEndpoint = `${endpoint}/scan`;
  const $ = selector => document.querySelector(selector);
  let requestSeq = 0;
  let enrichmentActive = false;
  let pollTimer = null;
  let backgroundLoading = false;
  let foregroundLoading = false;
  let preserveScanFeedback = false;
  let lastLoadedAt = null;
  let freshnessState = 'unknown';
  const POLL_INTERVAL_MS = 30_000;

  // Shared helpers live in admin-v2.js and are published through window.AdminV2.
  const helpers = () => window.AdminV2 || {};
  const el = (...args) => helpers().el(...args);

  function label(row) {
    const source = String(row.source || '').trim().toLowerCase();
    // Legacy jobs keep their historical key in storage; present the shared Telegram workflow name.
    const names = {
      studio_scene: 'Studio catalogs + ThePornDB', pbc: 'PBC', theporndb: 'ThePornDB',
      stashdb: 'StashDB', photos: 'Portrait providers',
      telegram_fingerprint: 'Scene fingerprint', telegram_oshash: 'OSHASH fingerprint',
      telegram_phash_scan: 'Visual fingerprint', telegram_phash_scan_batch2: 'Visual fingerprint · batch 2',
      telegram_caption_title_exact_v1: 'Caption title match',
      telegram_split_title_duration_v1: 'Title and duration match',
      telegram_linked_scene_artwork_by_id_v1: 'Artwork from linked scene',
      telegram_linked_scene_artwork_v1: 'Artwork from matched scene',
      lukayan_fingerprint: 'Scene fingerprint · legacy', lukayan_oshash: 'OSHASH fingerprint · legacy'
    };
    const sourceName = names[source] || (/^(?:telegram|lukayan)_/.test(source)
      ? `Telegram ${source.replace(/^(?:telegram|lukayan)_/, '').replaceAll('_', ' ')}` : (source || 'Unknown source'));
    return `${row.entity_type === 'video' ? 'Videos' : 'Actors'} · ${sourceName}`;
  }

  function metric(title, value, note) {
    const card = el('article', 'enrichment-summary-card');
    card.append(el('span', '', title), el('strong', '', Number(value || 0).toLocaleString('en-US')), el('small', '', note));
    return card;
  }

  function renderBatchLimits(limits) {
    const target = $('#enrichmentBatchLimits');
    const values = ['videos_per_run', 'pbc_actors_per_run', 'theporndb_actors_per_run',
      'stashdb_actors_per_run'].map(key => limits?.[key]);
    if (!values.every(value => Number.isSafeInteger(value) && value >= 0)) {
      target.textContent = 'Configured per-run batch limits are unavailable.';
      return;
    }
    target.textContent = `Configured limits per run: ${values[0].toLocaleString('en-US')} videos · PBC ${values[1].toLocaleString('en-US')} actors · ThePornDB ${values[2].toLocaleString('en-US')} actors · StashDB ${values[3].toLocaleString('en-US')} actors.`;
  }

  function estimatedBatches(item, limits) {
    const keys = item.entity_type === 'video'
      ? { studio_scene: 'videos_per_run' }
      : { pbc: 'pbc_actors_per_run', theporndb: 'theporndb_actors_per_run',
        stashdb: 'stashdb_actors_per_run' };
    const key = keys[item.source];
    const cap = key ? limits?.[key] : null;
    if (!Number.isSafeInteger(cap) || cap <= 0 ||
        !Number.isSafeInteger(item.remaining) || item.remaining < 0) return '';
    return ` · about ${Math.ceil(item.remaining / cap).toLocaleString('en-US')} batches at configured limit`;
  }

  function renderSyncSchedule(schedule) {
    const target = $('#enrichmentSchedule');
    let cadence = '';
    if (schedule?.mode === 'cron' && typeof schedule.cron_expression === 'string') {
      cadence = `${schedule.cron_expression} Europe/Warsaw`;
    } else if (schedule?.needs_conversion && Number.isSafeInteger(schedule.legacy_interval_minutes)) {
      cadence = `Every ${schedule.legacy_interval_minutes.toLocaleString('en-US')} minutes · cron conversion required`;
    } else if (schedule?.needs_conversion && typeof schedule.legacy_daily_at === 'string') {
      cadence = `Daily at ${schedule.legacy_daily_at} · cron conversion required`;
    }
    if (!cadence || typeof schedule?.run_on_startup !== 'boolean') {
      target.textContent = 'Configured sync schedule is unavailable.';
      return;
    }
    target.textContent = `Configured sync cadence: ${cadence}. ${schedule.run_on_startup
      ? 'A sync is configured to run on startup.' : 'Startup sync is disabled.'}`;
  }

  function updateFreshness(stale = false) {
    const target = $('#enrichmentFreshness');
    const announcement = $('#enrichmentFreshnessAnnouncement');
    if (stale) {
      target.dataset.state = 'stale';
      target.textContent = lastLoadedAt
        ? `Refresh failed. Showing the last successful update from ${lastLoadedAt.toLocaleTimeString()} local time.`
        : 'Refresh failed. Metadata progress is unavailable.';
      if (freshnessState !== 'stale') {
        announcement.textContent = lastLoadedAt
          ? 'Metadata progress refresh failed. Showing the last successful data.'
          : 'Metadata progress refresh failed. Progress is unavailable.';
      }
      freshnessState = 'stale';
      return;
    }
    lastLoadedAt = new Date();
    target.dataset.state = 'fresh';
    target.textContent = `Last successful progress refresh: ${lastLoadedAt.toLocaleTimeString()} local time.`;
    if (freshnessState === 'stale') {
      announcement.textContent = 'Metadata progress refresh succeeded. Progress is current again.';
    }
    freshnessState = 'fresh';
  }

  function render(payload, { preserveFeedback = false } = {}) {
    const summary = $('#enrichmentSummary');
    const rows = $('#enrichmentRows');
    summary.replaceChildren();
    rows.replaceChildren();
    const totals = payload.totals;
    summary.append(
      metric('Actors in catalog', payload.catalog.performers, 'Distinct names found in video credits'),
      metric('Telegram videos', payload.catalog.videos, 'Video queue currently covers Telegram'),
      metric('Due now', totals.due, 'Source attempts ready for retry or refresh'),
      metric('Lookup errors', totals.errors, 'Current error outcomes across sources')
    );
    renderSyncSchedule(payload.sync_schedule);
    renderBatchLimits(payload.batch_limits);
    if (!payload.sources.length) {
      if (!preserveFeedback) $('#enrichmentFeedback').textContent = 'No external metadata attempts have been recorded yet. The scheduled sync will populate this view.';
      return;
    }
    if (!preserveFeedback) $('#enrichmentFeedback').textContent = `${totals.attempts.toLocaleString('en-US')} source records tracked · ${totals.found.toLocaleString('en-US')} found · ${totals.no_match.toLocaleString('en-US')} no match · ${totals.due.toLocaleString('en-US')} due.`;
    for (const item of payload.sources) {
      const row = el('article', 'enrichment-source-row');
      const header = el('div', 'enrichment-source-heading');
      header.append(el('strong', '', label(item)));
      const checked = Number.isSafeInteger(item.catalog_checked) ? item.catalog_checked : null;
      header.append(el('span', '', checked === null
        ? `${item.attempts.toLocaleString('en-US')} source records`
        : `${checked.toLocaleString('en-US')} / ${item.catalog_total.toLocaleString('en-US')} catalog items checked`));
      const progress = el('progress', 'enrichment-progress');
      progress.max = Math.max(1, item.catalog_total);
      if (checked !== null) progress.value = Math.min(checked, progress.max);
      else progress.hidden = true;
      progress.setAttribute('aria-label', `${label(item)} checked`);
      const detail = el('p', 'enrichment-source-detail',
        `${item.attempts.toLocaleString('en-US')} source records · ${item.found.toLocaleString('en-US')} found · ${item.no_match.toLocaleString('en-US')} no match · ${item.errors.toLocaleString('en-US')} errors · ${item.due.toLocaleString('en-US')} due${checked === null ? '' : ` · ${item.remaining.toLocaleString('en-US')} estimated remaining${estimatedBatches(item, payload.batch_limits)}`}`);
      row.append(header, progress, detail);
      rows.append(row);
    }
  }

  async function load({ silent = false } = {}) {
    if (silent && (backgroundLoading || foregroundLoading)) return;
    if (silent) backgroundLoading = true;
    else foregroundLoading = true;
    const sequence = ++requestSeq;
    if (!silent) {
      preserveScanFeedback = false;
      $('#enrichmentRefresh').disabled = true;
      $('#enrichmentFeedback').textContent = 'Loading metadata progress…';
    }
    try {
      const payload = await window.AdminV2.getJson(endpoint);
      if (!payload || payload.available !== true || !payload.catalog || !payload.totals || !Array.isArray(payload.sources)) {
        throw new Error('The metadata progress response is incomplete.');
      }
      if (sequence !== requestSeq) return;
      render(payload, { preserveFeedback: silent && preserveScanFeedback });
      updateFreshness();
    } catch (error) {
      if (sequence === requestSeq) {
        updateFreshness(true);
        if (!silent) $('#enrichmentFeedback').textContent = `Could not load metadata progress. ${error.message}`;
      }
    } finally {
      if (silent) backgroundLoading = false;
      else {
        foregroundLoading = false;
        if (sequence === requestSeq) $('#enrichmentRefresh').disabled = false;
      }
    }
  }

  $('#enrichmentRefresh').addEventListener('click', load);
  $('#enrichmentScan').addEventListener('click', async event => {
    const button = event.currentTarget;
    if (!window.confirm('Run one bounded metadata scan now? This contacts configured providers and may update catalog metadata, NFO files, or artwork.')) return;
    button.disabled = true;
    preserveScanFeedback = true;
    $('#enrichmentFeedback').textContent = 'Queueing external metadata scan…';
    try {
      const result = await window.AdminV2.postJson(scanEndpoint, {});
      const jobId = String(result?.job_id || '');
      const jobState = ['queued', 'processing', 'done', 'failed', 'cancelled', 'accepted'].includes(result?.state)
        ? result.state : 'accepted';
      const feedback = $('#enrichmentFeedback');
      if (/^[A-Za-z0-9_-]{1,128}$/.test(jobId)) {
        const link = el('a', '', 'Open task details');
        link.href = `/admin/v2/?job_id=${encodeURIComponent(jobId)}#jobs`;
        const stateLabel = { queued: 'is queued', processing: 'is already processing', done: 'has completed',
          failed: 'has failed', cancelled: 'was cancelled', accepted: 'was accepted; current state is unavailable' }[jobState];
        feedback.replaceChildren(el('span', '', `Metadata scan ${stateLabel} (job ${jobId}). `), link);
      } else if (jobState === 'accepted') {
        feedback.textContent = 'Metadata scan request accepted; current task state is unavailable. Open Tasks to follow provider progress.';
      } else {
        feedback.textContent = `Metadata scan request accepted (${jobState}). Open Tasks to follow provider progress.`;
      }
    } catch (error) {
      $('#enrichmentFeedback').textContent = `Could not queue metadata scan. ${error.message}`;
    } finally {
      button.disabled = false;
    }
  });
  function syncEnrichment(route, tab) {
    enrichmentActive = route === 'quality' && tab === 'enrichment';
    if (pollTimer !== null) clearInterval(pollTimer);
    pollTimer = null;
    if (enrichmentActive) {
      const initialLoad = load();
      pollTimer = setInterval(() => {
        if (enrichmentActive && !document.hidden) return load({ silent: true });
      }, POLL_INTERVAL_MS);
      return initialLoad;
    }
    requestSeq += 1;
  }
  document.addEventListener('adminv2:navigate', event => syncEnrichment(event.detail?.route, event.detail?.tab));
  document.addEventListener('visibilitychange', () => {
    if (enrichmentActive && !document.hidden) return load({ silent: true });
  });
  const initialRoute = location.hash.replace(/^#/, '').split('?', 2);
  syncEnrichment(initialRoute[0], new URLSearchParams(initialRoute[1] || '').get('tab'));
})();
