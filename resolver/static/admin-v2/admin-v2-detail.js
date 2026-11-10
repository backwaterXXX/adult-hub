(() => {
  'use strict';
  // Admin V2 video detail panel and metadata editors.
  const AV = window.AdminV2;
  const { $, V2_API, announce, closeDialog, create, first, getJson, libraryHash, openDialog, postJson,
    renderLocation, safeText, showQueued, state, urlFor } = AV;
  const availability = (...args) => AV.availability(...args);
  const displayTime = (...args) => AV.displayTime(...args);
  const loadAvailabilityHistory = (...args) => AV.loadAvailabilityHistory(...args);
  const reviewAvailability = (...args) => AV.reviewAvailability(...args);
  const thumbnail = (...args) => AV.thumbnail(...args);
  const unavailableReason = (...args) => AV.unavailableReason(...args);
  const updateBulkSelection = (...args) => AV.updateBulkSelection(...args);
  function closeDetailVisual() {
    state.availabilityHistorySeq += 1;
    state.sideDetailId = null;
    stopDetailPlayback();
    if (state.detailAbort) {
      state.detailAbort.abort();
      state.detailAbort = null;
    }
    const dialog = $('#detailOverlay');
    if (dialog.open) closeDialog(dialog);
  }
  function stopDetailPlayback() {
    const player = $('#detailContent video');
    if (!player) return;
    player.pause();
    player.removeAttribute('src');
    player.load();
  }
  function safeExternalUrl(value) {
    if (typeof value !== 'string' || !value.trim()) return null;
    try { const parsed = new URL(value, location.origin); return ['http:', 'https:'].includes(parsed.protocol) ? parsed.href : null; } catch { return null; }
  }
  // Detail fields that accept DOM nodes keep their own builder; window.AdminV2.detailField serves the plain-text variant.
  function detailFieldValue(label, value) {
    const wrapper = create('div', 'detail-field');
    const dd = create('dd');
    if (value instanceof Node) dd.append(value);
    else dd.textContent = safeText(value);
    wrapper.append(create('dt', '', label), dd);
    return wrapper;
  }
  function localFileLabel(value) {
    return ({ present: 'Present', missing: 'Missing', unknown: 'Unknown' })[value] || 'Unknown';
  }
  function renderDetail(video) {
    const isCurrentDetail = () => state.detailVideo === video && (state.detailId === video.id || state.sideDetailId === video.id);
    state.availabilityHistorySeq += 1;
    state.detailVideo = video;
    stopDetailPlayback();
    const host = $('#detailContent'); host.replaceChildren();
    const title = safeText(first(video, ['title', 'name']), 'Untitled');
    const heading = create('h2', 'detail-title', title); heading.id = 'detailTitle';
    host.append(create('p', 'detail-kicker', 'VIDEO DETAILS'), heading);
    const [availabilityLabel, availabilityClass] = availability(video); host.append(create('span', `status-pill detail-state ${availabilityClass}`, availabilityLabel));
    const artwork = thumbnail(video);
    if (artwork) {
      const art = create('div', 'detail-art');
      const img = document.createElement('img');
      img.src = artwork;
      img.alt = `Artwork: ${title}`;
      img.loading = 'lazy';
      img.onerror = () => {
        art.replaceChildren(create('span', 'video-thumb-placeholder', 'No artwork preview available'));
      };
      art.append(img);
      host.append(art);
    }
    const preview = create('section', 'detail-preview');
    const play = create('button', 'button button-primary', '▶ Play preview');
    const playableId = first(video, ['id', 'video_id', 'videoId']);
    play.type = 'button';
    play.disabled = !playableId;
    const playerHost = create('div', 'detail-preview-player');
    play.addEventListener('click', async () => {
      if (!playerHost.querySelector('video')) {
        const player = document.createElement('video');
        player.className = 'detail-video-player';
        player.controls = true;
        player.playsInline = true;
        player.preload = 'metadata';
        if (artwork) player.poster = artwork;
        player.src = `/admin/play/${encodeURIComponent(String(playableId))}/stream?mode=proxy`;
        player.addEventListener('error', () => {
          player.pause();
          player.removeAttribute('src');
          player.load();
          playerHost.replaceChildren();
          play.textContent = '▶ Retry preview';
        }, { once: true });
        playerHost.replaceChildren(player);
        play.textContent = '■ Stop preview';
        try { await player.play(); } catch { /* Native controls remain available when autoplay is blocked. */ }
        return;
      }
      stopDetailPlayback();
      playerHost.replaceChildren();
      play.textContent = '▶ Play preview';
    });
    preview.append(play, playerHost);
    host.append(preview);
    const overview = create('section', 'detail-section');
    overview.setAttribute('aria-labelledby', 'detailOverviewHeading');
    const overviewHeading = create('h3', '', 'Overview'); overviewHeading.id = 'detailOverviewHeading';
    overview.append(overviewHeading);
    const fields = [
      ['ID', first(video, ['id', 'video_id', 'videoId'])], ['Studio', first(video, ['studio', 'studio_name'])],
      ['Channel', first(video, ['channel_name', 'channel_id', 'source_name'])], ['Added', displayTime(first(video, ['first_seen', 'added_at', 'created_at', 'date_added']))],
      ['Availability reason', unavailableReason(video)], ['Unavailable since', displayTime(video.unavailable_detected_at)],
      ['Duration', duration(first(video, ['duration_seconds', 'duration']))], ['Missing data', first(video, ['missing', 'missing_fields'])],
      ['Scene match', video.scene_match_source ? `${video.scene_match_source}${video.scene_match_external_id ? ` · ${video.scene_match_external_id}` : ''}` : '']
    ];
    const overviewFields = create('dl', 'detail-fields');
    fields.forEach(([label, value]) => overviewFields.append(detailFieldValue(label, value)));
    overview.append(overviewFields);
    const people = first(video, ['performers', 'people', 'cast']);
    if (people) overview.append(create('h4', 'detail-subheading', 'Performers'), create('p', 'detail-description', safeText(people)));
    const tags = first(video, ['tags', 'genres']);
    if (tags) overview.append(create('h4', 'detail-subheading', 'Tags'), create('p', 'detail-description', safeText(tags)));
    const description = first(video, ['description', 'overview', 'plot']);
    if (description) overview.append(create('h4', 'detail-subheading', 'Description'), create('p', 'detail-description', safeText(description)));
    const sourceRecords = Array.isArray(video.sources) ? video.sources : [];
    if (sourceRecords.length) {
      overview.append(create('h4', 'detail-subheading', 'Source records'));
      sourceRecords.forEach(source => {
        const line = create('p', 'detail-description');
        const sourceLabel = `${safeText(source.provider, 'Source')}${source.channel_id ? ` · Channel ${safeText(source.channel_id)}` : ''}`;
        line.textContent = `${sourceLabel} · ${source.active ? 'Active' : 'Inactive'} · Priority ${safeText(source.priority, '—')}`;
        overview.append(line);
      });
    }
    const links = [];
    for (const [label, keys] of [
      ['Open Telegram post', ['telegram_url', 'source_url', 'post_url']],
      ['Open in Jellyfin', ['jellyfin_url']],
      ['Open in StashDB', ['scene_match_url']],
    ]) {
      const url = safeExternalUrl(first(video, keys)); if (url) links.push([label, url]);
    }
    if (links.length) {
      const row = create('div', 'detail-links');
      links.forEach(([label, href]) => { const link = create('a', 'detail-link', `${label} ↗`); link.href = href; link.target = '_blank'; link.rel = 'noopener noreferrer'; row.append(link); });
      overview.append(create('h4', 'detail-subheading', 'Related links'), row);
    }
    host.append(overview);

    const metadata = create('section', 'detail-section');
    metadata.setAttribute('aria-labelledby', 'detailMetadataHeading');
    const metadataHeading = create('h3', '', 'Metadata'); metadataHeading.id = 'detailMetadataHeading';
    const revision = Number(video.metadata_revision);
    const revisionLabel = video.metadata_revision !== null && video.metadata_revision !== undefined
      && video.metadata_revision !== '' && Number.isInteger(revision) && revision >= 0 ? revision : 'Unavailable';
    metadata.append(metadataHeading, create('p', 'detail-description', `Current metadata revision: ${revisionLabel}. Manual overrides are compared against this revision before they are queued.`));
    const edit = create('button', 'button button-quiet', 'Edit metadata');
    edit.type = 'button'; edit.addEventListener('click', () => openMetadataEditor());
    metadata.append(edit);
    host.append(metadata);

    const files = create('section', 'detail-section');
    files.setAttribute('aria-labelledby', 'detailFilesHeading');
    const filesHeading = create('h3', '', 'Files'); filesHeading.id = 'detailFilesHeading';
    files.append(filesHeading);
    let artworkValue = localFileLabel(video.local_files?.artwork);
    if (video.artwork_url && video.local_files?.artwork === 'present') {
      const span = document.createElement('span');
      span.textContent = `${artworkValue} · `;
      const a = create('a', 'text-link', 'Open artwork ↗');
      a.href = video.artwork_url;
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      span.append(a);
      artworkValue = span;
    }
    const fileFields = create('dl', 'detail-fields');
    [
      ['OSHASH', video.oshash || 'Not available'],
      ['pHash', video.phash || 'Not calculated (available for a subset of videos)'],
      ['Video size', fileSize(video.file_size_bytes)],
      ['Stream file', localFileLabel(video.local_files?.stream)],
      ['NFO file', localFileLabel(video.local_files?.nfo)],
      ['Artwork file', artworkValue]
    ].forEach(([label, value]) => fileFields.append(detailFieldValue(label, value)));
    files.append(fileFields);

    const genArtwork = create('button', 'button button-quiet', 'Generate artwork');
    genArtwork.id = 'detailGenerateArtwork';
    genArtwork.type = 'button';
    genArtwork.addEventListener('click', async () => {
      if (genArtwork.disabled) return;
      genArtwork.disabled = true;
      const prevText = genArtwork.textContent;
      genArtwork.textContent = 'Queueing artwork generation…';
      try {
        const result = await postJson(`${V2_API}/library/videos/${encodeURIComponent(video.id)}/generate-artwork`, { offset_seconds: 60 });
        genArtwork.textContent = 'Artwork generation queued';
        if (isCurrentDetail()) showQueued(result, 'Artwork generation');
      } catch (err) {
        alert(`Failed to generate artwork: ${err.message || err}`);
        genArtwork.disabled = false;
        genArtwork.textContent = prevText;
      }
    });

    const downloadArtwork = create('button', 'button button-quiet', 'Download artwork');
    downloadArtwork.id = 'detailDownloadArtwork';
    downloadArtwork.type = 'button';
    downloadArtwork.addEventListener('click', async () => {
      if (downloadArtwork.disabled) return;
      downloadArtwork.disabled = true;
      const prevText = downloadArtwork.textContent;
      downloadArtwork.textContent = 'Queueing artwork download…';
      try {
        const result = await postJson(`${V2_API}/library/videos/${encodeURIComponent(video.id)}/download-artwork`, {});
        downloadArtwork.textContent = 'Artwork download queued';
        if (isCurrentDetail()) showQueued(result, 'Artwork download');
      } catch (err) {
        alert(`Failed to download artwork: ${err.message || err}`);
        downloadArtwork.disabled = false;
        downloadArtwork.textContent = prevText;
      }
    });

    const genPhash = create('button', 'button button-quiet', video.phash ? 'Regenerate pHash' : 'Generate pHash');
    genPhash.id = 'detailGeneratePhash';
    genPhash.type = 'button';
    genPhash.addEventListener('click', async () => {
      if (genPhash.disabled) return;
      genPhash.disabled = true;
      const prevText = genPhash.textContent;
      genPhash.textContent = 'Queueing pHash…';
      try {
        const payload = video.phash ? { force: true } : {};
        const res = await postJson(`${V2_API}/library/videos/${encodeURIComponent(video.id)}/generate-phash`, payload);
        if (res.already_computed) {
          announce('pHash is already computed.');
        } else if (res.job_id) {
          if (isCurrentDetail()) showQueued(res, 'pHash generation', undefined, 'phash');
        } else {
          announce('pHash task queued.');
        }
        if (res.already_computed) {
          genPhash.textContent = 'pHash already computed';
          await loadDetail(video.id);
        } else {
          genPhash.textContent = 'pHash queued';
        }
      } catch (err) {
        alert(`Failed to generate pHash: ${err.message || err}`);
        genPhash.disabled = false;
        genPhash.textContent = prevText;
      }
    });

    const filesActions = create('div', 'detail-actions');
    filesActions.append(genArtwork, downloadArtwork, genPhash);
    files.append(filesActions);
    host.append(files);

    const checkAvailability = create('button', 'button button-quiet', 'Check availability');
    checkAvailability.id = 'detailCheckAvailability';
    checkAvailability.type = 'button'; checkAvailability.addEventListener('click', () => reviewAvailability([String(video.id)]));
    const availabilityActions = create('div', 'detail-actions'); availabilityActions.append(checkAvailability); host.append(availabilityActions);
    loadAvailabilityHistory(String(video.id));
  }
  function fileSize(value) {
    const bytes = Number(value);
    if (value === null || value === undefined || !Number.isFinite(bytes) || bytes <= 0) return 'Not available';
    return bytes >= 1e9 ? `${(bytes / 1e9).toFixed(2)} GB` : `${(bytes / 1e6).toFixed(2)} MB`;
  }
  function duration(value) {
    if (value === null || value === undefined || value === '') return '—';
    const n = Number(value); if (!Number.isFinite(n) || n < 0) return safeText(value);
    const hours = Math.floor(n / 3600); const minutes = Math.floor((n % 3600) / 60);
    return hours ? `${hours} hour${hours === 1 ? '' : 's'}${minutes ? ` ${minutes} min` : ''}` : `${minutes} min`;
  }
  async function loadDetail(id) {
    // An action button refreshes the drawer after its own request finishes, and
    // the drawer may have been closed while that request was in flight. Bailing
    // out before touching the dialog keeps a finished task from resurrecting a
    // detail view the user dismissed, where its loading banner would be stranded
    // by the stale-response guard below.
    const isCurrentDetail = () => state.detailId === id || state.sideDetailId === id;
    if (!isCurrentDetail()) return;
    if (state.detailAbort) state.detailAbort.abort();
    const controller = new AbortController(); state.detailAbort = controller;
    const dialog = $('#detailOverlay');
    if (!dialog.open) openDialog(dialog);
    const host = $('#detailContent');
    const heading = create('h2', 'detail-title', 'Loading…'); heading.id = 'detailTitle';
    host.replaceChildren(create('p', 'detail-kicker', 'VIDEO DETAILS'), heading, create('div', 'state-banner state-loading', 'Loading video details…'));
    try {
      const payload = await getJson(`${V2_API}/library/videos/${encodeURIComponent(id)}`, controller.signal);
      if (!isCurrentDetail()) return;
      const video = payload.item || payload.video || payload.data || payload;
      renderDetail(video);
      $('.detail-drawer').focus({ preventScroll: true });
    } catch (error) {
      if (error.name === 'AbortError' || !isCurrentDetail()) return;
      const errorHeading = create('h2', 'detail-title', 'Could not open video'); errorHeading.id = 'detailTitle';
      host.replaceChildren(create('p', 'detail-kicker', 'VIDEO DETAILS'), errorHeading, create('p', 'detail-description', error.message));
      $('.detail-drawer').focus({ preventScroll: true });
    }
  }
  function closeDetail() {
    if (state.sideDetailId !== null) {
      state.sideDetailId = null;
      closeDetailVisual();
      return;
    }
    const url = new URL(location.href);
    if (url.hash.startsWith('#film/')) {
      if (history.state && history.state.detailOpener) history.back();
      else { history.replaceState({}, '', urlFor(libraryHash(state.libraryTab, state.duplicateMatch))); renderLocation(); }
    }
  }
  function openSideDetail(id) {
    if (!id) return;
    state.sideDetailId = String(id);
    loadDetail(state.sideDetailId);
  }
  function metadataPayloadFromEditor() {
    const video = state.detailVideo;
    const changes = {}, reset_fields = [];
    for (const field of ['title', 'plot', 'studio', 'performers', 'tags', 'date_released']) {
      const enabled = $(`[data-metadata-override="${field}"]`).checked;
      const input = $(`[data-metadata-value="${field}"]`);
      const wasOverridden = Boolean(video.metadata_fields?.[field]?.overridden);
      if (enabled) {
        let value = input.value;
        if (field === 'performers' || field === 'tags') value = value.split('\n').map(item => item.trim()).filter(Boolean);
        if (!wasOverridden || JSON.stringify(value) !== JSON.stringify(video[field] ?? (Array.isArray(value) ? [] : ''))) changes[field] = value;
      } else if (wasOverridden) reset_fields.push(field);
    }
    return { video_id: video.id, expected_revision: Number(video.metadata_revision) || 0, changes, reset_fields };
  }
  function openMetadataEditor(restoreDraft = false) {
    const video = state.detailVideo;
    if (!video?.id) return;
    state.metadataDialogSeq += 1;
    state.metadataBulkVideoIds = null;
    const draft = restoreDraft ? state.metadataRequest : null;
    if (!restoreDraft) state.metadataRequest = null;
    state.metadataPlanId = null;
    const dialog = $('#metadataDialog'); const content = $('#metadataDialogContent'); content.replaceChildren();
    const heading = create('h2', '', 'Edit metadata'); heading.id = 'metadataDialogTitle';
    content.append(create('p', 'eyebrow', 'MANUAL OVERRIDES'), heading,
      create('p', 'detail-description', 'Manual values stay in place while source metadata continues to update. Clear an override to use the latest source value.'));
    for (const [field, label] of [['title', 'Title'], ['plot', 'Description'], ['studio', 'Studio'], ['performers', 'Performers'], ['tags', 'Tags'], ['date_released', 'Release date']]) {
      const stateField = video.metadata_fields?.[field] || { source: video[field] ?? '', effective: video[field] ?? '', overridden: false };
      const hasDraftChange = Object.hasOwn(draft?.changes || {}, field);
      const draftReset = draft?.reset_fields?.includes(field) === true;
      const section = create('section', 'metadata-edit-field');
      const inputLabel = create('label', '', label); inputLabel.htmlFor = `metadata-${field}`;
      const overrideLabel = create('label', 'metadata-override-toggle');
      const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.dataset.metadataOverride = field;
      checkbox.checked = hasDraftChange || (!draftReset && Boolean(stateField.overridden));
      overrideLabel.append(checkbox, create('span', '', 'Use a manual value'));
      const input = ['plot', 'performers', 'tags'].includes(field) ? document.createElement('textarea') : document.createElement('input');
      if (field === 'date_released') input.type = 'date';
      const value = hasDraftChange ? draft.changes[field] : draftReset ? stateField.source : stateField.effective;
      input.id = `metadata-${field}`; input.dataset.metadataValue = field;
      input.value = Array.isArray(value) ? value.join('\n') : String(value ?? '');
      input.maxLength = field === 'title' ? 500 : field === 'studio' ? 300 : field === 'plot' ? 10000 : 10000; input.disabled = !checkbox.checked;
      if (field === 'plot') input.rows = 5;
      if (field === 'performers' || field === 'tags') { input.rows = 4; input.maxLength = 20000; input.placeholder = 'One value per line'; }
      checkbox.addEventListener('change', () => { input.disabled = !checkbox.checked; if (checkbox.checked) input.focus(); });
      section.append(inputLabel, overrideLabel, input,
        create('small', 'metadata-source-value', `Current source: ${stateField.source === '' || stateField.source === null || (Array.isArray(stateField.source) && !stateField.source.length) ? '(empty)' : safeText(Array.isArray(stateField.source) ? stateField.source.join(', ') : stateField.source)}`));
      content.append(section);
    }
    const actions = $('#metadataDialogActions'); actions.replaceChildren();
    const cancel = create('button', 'button button-quiet', 'Cancel'); cancel.type = 'button'; cancel.addEventListener('click', () => closeDialog(dialog));
    const preview = create('button', 'button button-primary', 'Preview changes'); preview.type = 'button'; preview.addEventListener('click', previewMetadataChanges);
    actions.append(cancel, preview);
    openDialog(dialog);
  }
  async function previewMetadataChanges() {
    const sequence = state.metadataDialogSeq;
    const videoId = String(state.detailVideo?.id ?? '');
    if (!videoId || !$('#metadataDialog').open) return;
    const request = metadataPayloadFromEditor();
    if (!Object.keys(request.changes).length && !request.reset_fields.length) { announce('No metadata changes to preview.'); return; }
    const actions = $('#metadataDialogActions');
    actions.querySelectorAll('button').forEach(button => { button.disabled = true; });
    try {
      const payload = await window.AdminV2.postJson(`${V2_API}/operations/preview`, { type: 'metadata_update', ...request });
      if (sequence !== state.metadataDialogSeq || String(state.detailVideo?.id ?? '') !== videoId || !$('#metadataDialog').open) return;
      state.metadataRequest = request; state.metadataPlanId = payload.plan_id; renderMetadataPreview(payload);
    } catch (error) {
      if (sequence !== state.metadataDialogSeq || String(state.detailVideo?.id ?? '') !== videoId || !$('#metadataDialog').open) return;
      announce(error.message);
      actions.querySelectorAll('button').forEach(button => { button.disabled = false; });
    }
  }
  function renderMetadataPreview(preview) {
    const content = $('#metadataDialogContent'); content.replaceChildren();
    const heading = create('h2', '', 'Review metadata changes'); heading.id = 'metadataDialogTitle';
    content.append(create('p', 'eyebrow', 'PREVIEW'), heading);
    const list = create('dl', 'metadata-diff-list');
    for (const [field, values] of Object.entries(preview.fields || {})) {
      const labels = { title: 'Title', plot: 'Description', studio: 'Studio', performers: 'Performers', tags: 'Tags', date_released: 'Release date' };
      const displayValue = value => Array.isArray(value) ? value.join(', ') : value;
      list.append(create('dt', '', labels[field] || field));
      const valuesNode = create('dd');
      valuesNode.append(create('span', 'metadata-diff-before', values.before === '' || values.before === null || (Array.isArray(values.before) && !values.before.length) ? '(empty)' : String(displayValue(values.before))),
        create('span', 'metadata-diff-arrow', '→'),
        create('strong', 'metadata-diff-after', values.after === '' || values.after === null || (Array.isArray(values.after) && !values.after.length) ? '(empty)' : String(displayValue(values.after))));
      list.append(valuesNode);
    }
    content.append(list, create('p', 'detail-description', 'The worker saves the local override, writes updated NFO files atomically, then requests a Jellyfin refresh.'));
    const actions = $('#metadataDialogActions'); actions.replaceChildren();
    const back = create('button', 'button button-quiet', 'Back to editor'); back.type = 'button'; back.addEventListener('click', () => openMetadataEditor(true));
    const queue = create('button', 'button button-primary', 'Queue metadata update'); queue.type = 'button'; queue.addEventListener('click', queueMetadataUpdate);
    actions.append(back, queue);
  }
  async function queueMetadataUpdate() {
    const planId = state.metadataPlanId;
    if (!planId) return;
    const sequence = state.metadataDialogSeq;
    const videoId = String(state.detailVideo?.id ?? '');
    if (!videoId || !$('#metadataDialog').open) return;
    const actions = $('#metadataDialogActions');
    const focusedAction = actions.contains(document.activeElement) ? document.activeElement : null;
    actions.querySelectorAll('button').forEach(button => { button.disabled = true; });
    try {
      const payload = await window.AdminV2.postJson(`${V2_API}/operations/execute`, { plan_id: planId });
      if (sequence !== state.metadataDialogSeq || String(state.detailVideo?.id ?? '') !== videoId || !$('#metadataDialog').open) return;
      closeDialog($('#metadataDialog'));
      showQueued(payload, 'Metadata update');
    } catch (error) {
      if (sequence !== state.metadataDialogSeq || String(state.detailVideo?.id ?? '') !== videoId || !$('#metadataDialog').open) return;
      announce(error.message);
      actions.querySelectorAll('button').forEach(button => { button.disabled = false; });
      if (error.status === 409 && state.metadataRequest) {
        state.metadataPlanId = null;
        const queue = [...actions.querySelectorAll('button')].find(button => button.textContent === 'Queue metadata update');
        if (queue) queue.disabled = true;
        const refresh = create('button', 'button button-primary', 'Refresh preview'); refresh.type = 'button';
        refresh.addEventListener('click', async () => {
          const refreshSequence = state.metadataDialogSeq;
          const refreshVideoId = String(state.detailVideo?.id ?? '');
          const request = state.metadataRequest;
          if (!refreshVideoId || !request || !$('#metadataDialog').open) return;
          const restoreRefreshFocus = document.activeElement === refresh;
          refresh.disabled = true;
          try {
            const refreshed = await window.AdminV2.postJson(`${V2_API}/operations/preview`, {
              type: 'metadata_update', ...request
            });
            if (refreshSequence !== state.metadataDialogSeq || String(state.detailVideo?.id ?? '') !== refreshVideoId || !$('#metadataDialog').open) return;
            state.metadataPlanId = refreshed.plan_id;
            renderMetadataPreview(refreshed);
            const newQueue = [...$('#metadataDialogActions').querySelectorAll('button')].find(button => button.textContent === 'Queue metadata update');
            if (restoreRefreshFocus && document.activeElement === document.body) newQueue?.focus({ preventScroll: true });
          } catch (refreshError) {
            if (refreshSequence !== state.metadataDialogSeq || String(state.detailVideo?.id ?? '') !== refreshVideoId || !$('#metadataDialog').open) return;
            announce(refreshError.message); refresh.disabled = false;
            if (restoreRefreshFocus && document.activeElement === document.body) refresh.focus({ preventScroll: true });
          }
        });
        if (queue) queue.before(refresh); else actions.append(refresh);
        if (focusedAction && document.activeElement === document.body) refresh.focus({ preventScroll: true });
      } else if (focusedAction?.isConnected && document.activeElement === document.body) {
        focusedAction.focus({ preventScroll: true });
      }
    }
  }
  function openBulkMetadataEditor(restoreDraft = false) {
    if (state.route !== 'library' || state.isStale || !state.selectedVideos.size || state.selectedVideos.size > 25) return;
    const videoIds = [...state.selectedVideos.keys()];
    state.metadataDialogSeq += 1;
    state.metadataBulkVideoIds = videoIds;
    if (!restoreDraft) state.metadataBulkDraft = null;
    state.metadataRequest = null; state.metadataPlanId = null;
    const dialog = $('#metadataDialog'); const content = $('#metadataDialogContent'); content.replaceChildren();
    const heading = create('h2', '', 'Edit metadata for selected videos'); heading.id = 'metadataDialogTitle';
    content.append(create('p', 'eyebrow', 'BULK METADATA EDIT'), heading,
      create('p', 'detail-description', `Set the same selected fields for ${videoIds.length} videos, or reset fields to each video's latest source value. Each video is checked against its own revision before any task is queued.`));
    for (const [field, label] of [['title', 'Title'], ['plot', 'Description'], ['studio', 'Studio'], ['performers', 'Performers'], ['tags', 'Tags'], ['date_released', 'Release date']]) {
      const section = create('section', 'metadata-edit-field');
      const inputLabel = create('label', '', label); inputLabel.htmlFor = `bulk-metadata-${field}`;
      const setLabel = create('label', 'metadata-override-toggle');
      const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.dataset.bulkMetadataOverride = field;
      checkbox.checked = Object.hasOwn(state.metadataBulkDraft?.changes || {}, field);
      setLabel.append(checkbox, create('span', '', 'Set this value for all selected videos'));
      const input = ['plot', 'performers', 'tags'].includes(field) ? document.createElement('textarea') : document.createElement('input');
      if (field === 'date_released') input.type = 'date';
      input.id = `bulk-metadata-${field}`; input.dataset.bulkMetadataValue = field;
      const draftReset = state.metadataBulkDraft?.reset_fields?.includes(field) === true;
      input.value = checkbox.checked
        ? (Array.isArray(state.metadataBulkDraft.changes[field]) ? state.metadataBulkDraft.changes[field].join('\n') : String(state.metadataBulkDraft.changes[field] ?? ''))
        : '';
      input.disabled = !checkbox.checked; input.maxLength = field === 'title' ? 500 : field === 'studio' ? 300 : field === 'plot' ? 10000 : field === 'date_released' ? 10 : 20000;
      if (field === 'plot') input.rows = 4;
      if (field === 'performers' || field === 'tags') { input.rows = 3; input.placeholder = 'One value per line'; }
      checkbox.addEventListener('change', () => { input.disabled = !checkbox.checked; });
      const resetLabel = create('label', 'metadata-override-toggle');
      const reset = document.createElement('input'); reset.type = 'checkbox'; reset.dataset.bulkMetadataReset = field;
      reset.checked = draftReset;
      reset.addEventListener('change', () => { if (reset.checked) { checkbox.checked = false; input.disabled = true; } });
      checkbox.addEventListener('change', () => { if (checkbox.checked) reset.checked = false; });
      resetLabel.append(reset, create('span', '', 'Reset to each video’s latest source value'));
      section.append(inputLabel, setLabel, input, resetLabel);
      content.append(section);
    }
    const actions = $('#metadataDialogActions'); actions.replaceChildren();
    const cancel = create('button', 'button button-quiet', 'Cancel'); cancel.type = 'button'; cancel.addEventListener('click', () => closeDialog(dialog));
    const preview = create('button', 'button button-primary', 'Preview changes'); preview.type = 'button'; preview.addEventListener('click', previewBulkMetadataChanges);
    actions.append(cancel, preview); openDialog(dialog);
  }
  function bulkMetadataPayloadFromEditor() {
    const changes = {}, reset_fields = [];
    for (const field of ['title', 'plot', 'studio', 'performers', 'tags', 'date_released']) {
      if ($(`[data-bulk-metadata-override="${field}"]`).checked) {
        const input = $(`[data-bulk-metadata-value="${field}"]`);
        changes[field] = ['performers', 'tags'].includes(field)
          ? input.value.split('\n').map(item => item.trim()).filter(Boolean) : input.value;
      } else if ($(`[data-bulk-metadata-reset="${field}"]`).checked) reset_fields.push(field);
    }
    return { type: 'metadata_update_batch', video_ids: [...(state.metadataBulkVideoIds || [])], changes, reset_fields };
  }
  async function previewBulkMetadataChanges() {
    const sequence = state.metadataDialogSeq;
    const videoIds = [...(state.metadataBulkVideoIds || [])];
    const request = bulkMetadataPayloadFromEditor();
    if (!videoIds.length || !$('#metadataDialog').open) return;
    if (!Object.keys(request.changes).length && !request.reset_fields.length) { announce('Choose at least one field to set or reset.'); return; }
    if (JSON.stringify([...state.selectedVideos.keys()]) !== JSON.stringify(videoIds)) { announce('The selection changed. Reopen the bulk editor to review the current selection.'); return; }
    state.metadataBulkDraft = { changes: request.changes, reset_fields: request.reset_fields };
    const actions = $('#metadataDialogActions'); actions.querySelectorAll('button').forEach(button => { button.disabled = true; });
    try {
      const preview = await window.AdminV2.postJson(`${V2_API}/operations/preview`, request);
      if (sequence !== state.metadataDialogSeq || !$('#metadataDialog').open) return;
      if (state.route !== 'library'
          || JSON.stringify([...state.selectedVideos.keys()]) !== JSON.stringify(videoIds)) {
        closeDialog($('#metadataDialog'));
        announce('The selection changed while the preview was loading. Reopen the bulk editor to review the current selection.');
        return;
      }
      state.metadataPlanId = preview.plan_id; renderBulkMetadataPreview(preview);
    } catch (error) {
      if (sequence !== state.metadataDialogSeq || !$('#metadataDialog').open) return;
      announce(error.message); actions.querySelectorAll('button').forEach(button => { button.disabled = false; });
    }
  }
  function renderBulkMetadataPreview(preview) {
    const content = $('#metadataDialogContent'); content.replaceChildren();
    const heading = create('h2', '', 'Review bulk metadata changes'); heading.id = 'metadataDialogTitle';
    content.append(create('p', 'eyebrow', 'PREVIEW'), heading,
      create('p', 'detail-description', `${preview.items.length} videos will receive the selected override changes. Each update runs as a separate task; stale revisions prevent the batch from being queued.`));
    const list = create('ul', 'operation-item-list');
    for (const item of preview.items || []) {
      const row = create('li');
      row.append(create('strong', '', item.title || 'Untitled'), create('code', 'operation-video-id', item.video_id));
      for (const [field, values] of Object.entries(item.fields || {})) {
        const before = Array.isArray(values.before) ? values.before.join(', ') : values.before;
        const after = Array.isArray(values.after) ? values.after.join(', ') : values.after;
        const labels = { title: 'Title', plot: 'Description', studio: 'Studio', performers: 'Performers', tags: 'Tags', date_released: 'Release date' };
        row.append(create('span', '', `${labels[field] || field}: ${before == null || before === '' ? '(empty)' : before} → ${after == null || after === '' ? '(empty)' : after}`));
      }
      list.append(row);
    }
    content.append(list, create('p', 'operation-expiry', `Preview expires ${displayTime(preview.expires_at)}. Any selected video changing its metadata invalidates the batch.`));
    const actions = $('#metadataDialogActions'); actions.replaceChildren();
    const back = create('button', 'button button-quiet', 'Back to editor'); back.type = 'button'; back.addEventListener('click', () => openBulkMetadataEditor(true));
    const queue = create('button', 'button button-primary', `Queue ${preview.items.length} updates`); queue.type = 'button'; queue.addEventListener('click', queueBulkMetadataUpdate);
    actions.append(back, queue);
  }
  async function queueBulkMetadataUpdate() {
    const planId = state.metadataPlanId;
    const sequence = state.metadataDialogSeq;
    const videoIds = [...(state.metadataBulkVideoIds || [])];
    if (!planId || !videoIds.length || !$('#metadataDialog').open) return;
    if (state.route !== 'library' || JSON.stringify([...state.selectedVideos.keys()]) !== JSON.stringify(videoIds)) {
      closeDialog($('#metadataDialog'));
      announce('The selection changed after preview. Reopen the bulk editor to review the current selection.');
      return;
    }
    const actions = $('#metadataDialogActions');
    const focusedAction = actions.contains(document.activeElement) ? document.activeElement : null;
    const queue = [...actions.querySelectorAll('button')].find(button => button.textContent.startsWith('Queue '));
    actions.querySelectorAll('button').forEach(button => { button.disabled = true; });
    try {
      const result = await window.AdminV2.postJson(`${V2_API}/operations/execute`, { plan_id: planId });
      const current = sequence === state.metadataDialogSeq && $('#metadataDialog').open
        && JSON.stringify([...state.selectedVideos.keys()]) === JSON.stringify(videoIds);
      if (current) {
        for (const videoId of videoIds) state.selectedVideos.delete(videoId);
        updateBulkSelection(); closeDialog($('#metadataDialog'));
      }
      if (current) showQueued(result, `Bulk metadata update for ${result.jobs?.length || videoIds.length} videos`);
    } catch (error) {
      if (sequence !== state.metadataDialogSeq || !$('#metadataDialog').open) return;
      announce(error.message); actions.querySelectorAll('button').forEach(button => { button.disabled = false; });
      if (error.status === 409) {
        state.metadataPlanId = null;
        if (queue) queue.disabled = true;
        const back = create('button', 'button button-primary', 'Review current selection'); back.type = 'button';
        back.addEventListener('click', () => openBulkMetadataEditor(true));
        if (queue) queue.before(back); else actions.append(back);
        if (focusedAction && document.activeElement === document.body) back.focus({ preventScroll: true });
      } else if (focusedAction?.isConnected && document.activeElement === document.body) {
        focusedAction.focus({ preventScroll: true });
      }
    }
  }
  Object.assign(AV, { closeDetail, closeDetailVisual, loadDetail, openBulkMetadataEditor, openSideDetail });
})();
