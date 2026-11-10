(() => {
  'use strict';
  // Admin V2 performer directory, person details, and merges.
  const AV = window.AdminV2;
  const { $, V2_API, closeDialog, create, getJson, openDialog, responseError, setText, showQueued, state } = AV;
  async function loadPeople() {
    if (state.route !== 'performers' || $('#performerDirectoryPane').hidden) return;
    const sequence = ++state.peopleRequestSeq;
    state.peopleBusy = true;
    const query = $('#peopleQuery').value.trim(); setText($('#peopleFeedback'), 'Loading people…');
    try {
      const params = new URLSearchParams({ q: query, offset: String(state.peopleOffset), limit: '50' });
      const payload = await getJson(`${V2_API}/organize/people?${params}`);
      if (sequence !== state.peopleRequestSeq || state.route !== 'performers' || $('#performerDirectoryPane').hidden) return;
      if (!payload?.ok) throw new Error(responseError(payload, 'Could not load the people catalog.'));
      if (typeof payload.available !== 'boolean') {
        throw new Error('The people catalog response is incomplete.');
      }
      if (!payload.available) {
        $('#peopleRows').replaceChildren();
        state.peopleTotal = 0;
        $('#peopleState').hidden = false;
        $('#peopleState').textContent = 'People data is unavailable. Check database migrations and refresh.';
        $('#peopleTableWrap').hidden = true;
        setText($('#peopleFeedback'), 'People catalog unavailable');
        $('#peoplePageInfo').textContent = 'Unavailable';
        $('#peoplePrev').disabled = true;
        $('#peopleNext').disabled = true;
        $('#peoplePagination').hidden = true;
        return;
      }
      if (!Array.isArray(payload.items)) {
        throw new Error('The people catalog response is incomplete.');
      }
      const rows = $('#peopleRows'); rows.replaceChildren();
      state.peopleTotal = Number(payload.total) || 0;
      $('#peopleState').hidden = payload.items.length > 0;
      $('#peopleTableWrap').hidden = !payload.items.length;
      if (!payload.items.length) $('#peopleState').textContent = query ? 'No people match this search.' : 'No performers are in the catalog yet.';
      for (const person of payload.items || []) {
        const row = document.createElement('tr');
        const aliases = Array.isArray(person.aliases)
          ? person.aliases.filter(alias => typeof alias === 'string' && alias.trim()).join(', ')
          : '';
        row.append(create('td', 'person-name', person.name), create('td', 'person-aliases', aliases || '—'), create('td', '', person.video_count),
          create('td', '', person.has_bio ? 'Available' : 'Missing'), create('td', '', person.has_photo ? 'Available' : 'Missing'));
        const actionCell = document.createElement('td'); const details = create('button', 'button button-quiet', 'Details'); details.type = 'button'; details.addEventListener('click', () => openPersonDetails(person.name)); actionCell.append(details); row.append(actionCell); rows.append(row);
      }
      setText($('#peopleFeedback'), '');
      $('#peoplePageInfo').textContent = state.peopleTotal ? `${state.peopleOffset + 1}–${Math.min(state.peopleOffset + 50, state.peopleTotal)} of ${state.peopleTotal}` : 'No people';
      $('#peoplePrev').disabled = state.peopleOffset <= 0; $('#peopleNext').disabled = state.peopleOffset + 50 >= state.peopleTotal;
      $('#peoplePagination').hidden = state.peopleTotal <= 50;
    } catch (error) {
      if (sequence !== state.peopleRequestSeq || state.route !== 'performers' || $('#performerDirectoryPane').hidden) return;
      setText($('#peopleFeedback'), error.message); $('#peopleState').hidden = false; $('#peopleState').textContent = 'People could not be loaded. Refresh to try again.';
    } finally { if (sequence === state.peopleRequestSeq) state.peopleBusy = false; }
  }
  async function openPersonDetails(name) {
    const dialog = $('#personDetailDialog'); const content = $('#personDetailContent'); content.replaceChildren();
    state.personMergePreviewSeq += 1;
    state.pendingPersonMerge = null;
    const heading = create('h2', 'detail-title', name); heading.id = 'personDetailTitle'; content.append(create('p', 'detail-kicker', 'FILMOGRAPHY'), heading, create('p', 'catalog-feedback', 'Loading filmography…')); openDialog(dialog);
    $('.performer-detail-drawer .detail-drawer').focus({ preventScroll: true });
    dialog.addEventListener('close', () => {
      state.personFilmographySeq += 1;
      state.personMergePreviewSeq += 1;
      state.pendingPersonMerge = null;
    }, { once: true });
    const mergeSection = create('section', 'person-merge-panel');
    mergeSection.append(create('h3', '', 'Merge this name into another performer'));
    const label = create('label', '', 'Canonical performer name'); label.htmlFor = 'personMergeCanonical';
    const input = document.createElement('input'); input.id = 'personMergeCanonical'; input.type = 'text'; input.maxLength = 200; input.autocomplete = 'off';
    const previewButton = create('button', 'button button-quiet', 'Preview merge'); previewButton.id = 'personMergePreview'; previewButton.type = 'button'; previewButton.addEventListener('click', () => previewPersonMerge(name));
    const feedback = create('p', 'catalog-feedback'); feedback.id = 'personMergeFeedback';
    mergeSection.append(label, input, previewButton, feedback);
    const profileSection = create('section', 'person-profile-panel');
    profileSection.append(create('h3', '', 'Performer profile'));
    const profileStatus = create('p', 'catalog-feedback', 'Loading profile…');
    profileStatus.id = 'personProfileFeedback'; profileStatus.setAttribute('role', 'status'); profileStatus.setAttribute('aria-live', 'polite');
    const profileLinks = create('div', 'detail-links person-profile-links');
    profileLinks.id = 'personProfileLinks';
    profileSection.append(profileStatus, profileLinks);
    const profileFields = create('div', 'person-profile-fields');
    const bioLabel = create('label', '', 'Biography');
    const bio = document.createElement('textarea'); bio.id = 'personProfileBio'; bio.maxLength = 12000; bio.rows = 5; bio.setAttribute('aria-describedby', 'personProfileBioHelp');
    bioLabel.append(bio);
    const bioHelp = create('small', 'detail-muted', 'Manual biographies are protected from provider updates until reset.'); bioHelp.id = 'personProfileBioHelp'; bioLabel.append(bioHelp);
    const photoLabel = create('label', '', 'Artwork URL (HTTPS)');
    const photo = document.createElement('input'); photo.id = 'personProfilePhoto'; photo.type = 'url'; photo.maxLength = 2048; photo.autocomplete = 'url'; photo.placeholder = 'https://…'; photo.setAttribute('aria-describedby', 'personProfilePhotoHelp');
    photoLabel.append(photo);
    const photoHelp = create('small', 'detail-muted', 'Public HTTPS artwork URLs only. Credentials, local hosts, secret query parameters, and URL fragments are rejected.'); photoHelp.id = 'personProfilePhotoHelp'; photoLabel.append(photoHelp);
    const aliasesLabel = create('label', '', 'Aliases (one per line)');
    const aliases = document.createElement('textarea'); aliases.id = 'personProfileAliases'; aliases.maxLength = 24000; aliases.rows = 5; aliases.setAttribute('aria-describedby', 'personProfileAliasesHelp');
    aliasesLabel.append(aliases);
    const aliasesHelp = create('small', 'detail-muted', 'Aliases are used for performer matching. Ambiguous or duplicate identities are rejected.'); aliasesHelp.id = 'personProfileAliasesHelp'; aliasesLabel.append(aliasesHelp);
    profileFields.append(bioLabel, photoLabel, aliasesLabel);
    const profileActions = create('div', 'person-profile-actions');
    const saveProfile = create('button', 'button button-primary', 'Save profile'); saveProfile.id = 'personProfileSave'; saveProfile.type = 'button';
    const resetProfile = create('button', 'button button-quiet', 'Reset to provider data'); resetProfile.id = 'personProfileReset'; resetProfile.type = 'button';
    const reloadProfile = create('button', 'button button-quiet', 'Reload profile'); reloadProfile.id = 'personProfileReload'; reloadProfile.type = 'button';
    profileActions.append(saveProfile, resetProfile, reloadProfile);
    profileSection.append(profileFields, profileActions);
    content.insertBefore(profileSection, heading.nextSibling);
    const profileUrl = `${V2_API}/organize/people/${encodeURIComponent(name)}/profile`;
    const loadProfile = async () => {
      profileStatus.textContent = 'Loading profile…';
      saveProfile.disabled = resetProfile.disabled = reloadProfile.disabled = true;
      try {
        const payload = await getJson(profileUrl);
        if (!dialog.open || !payload?.ok) throw new Error(responseError(payload, 'Could not load performer profile.'));
        const profile = payload.profile;
        bio.value = profile.bio || ''; photo.value = profile.photo || ''; aliases.value = (profile.aliases || []).join('\n');
        profileLinks.replaceChildren();
        if (profile.stashdb_url) {
          const a = create('a', 'detail-link', 'StashDB ↗');
          a.href = profile.stashdb_url;
          a.target = '_blank';
          a.rel = 'noopener noreferrer';
          profileLinks.append(a);
        }
        if (profile.pbc_url) {
          const a = create('a', 'detail-link', 'PBC ↗');
          a.href = profile.pbc_url;
          a.target = '_blank';
          a.rel = 'noopener noreferrer';
          profileLinks.append(a);
        }
        profileSection.dataset.revision = String(profile.revision || 0);
        profileStatus.textContent = profile.overrides?.length
          ? `Revision ${profile.revision}. Manual overrides: ${profile.overrides.join(', ')}.`
          : `Revision ${profile.revision}. Profile values are inherited from provider data.`;
        const capabilities = await getJson(`${V2_API}/capabilities`);
        const canWrite = capabilities.actions_enabled === true;
        saveProfile.disabled = resetProfile.disabled = !canWrite;
        if (!canWrite) profileStatus.textContent += ' Editing is unavailable in READ-ONLY mode.';
      } catch (error) {
        if (!dialog.open) return;
        profileStatus.textContent = `Could not load performer profile. ${error.message}`;
        reloadProfile.disabled = false;
      }
    };
    saveProfile.addEventListener('click', async () => {
      saveProfile.disabled = resetProfile.disabled = true;
      profileStatus.textContent = 'Saving profile…';
      try {
        const result = await window.AdminV2.putJson(profileUrl, {
          expected_revision: Number(profileSection.dataset.revision),
          bio: bio.value,
          photo: photo.value,
          aliases: aliases.value.split(/\r?\n/).map(value => value.trim()).filter(Boolean),
        });
        if (!dialog.open) return;
        profileSection.dataset.revision = String(result.profile.revision);
        profileStatus.textContent = `Saved revision ${result.profile.revision}. Manual overrides: ${result.profile.overrides.join(', ') || 'none'}.`;
        loadPersonFilmography(name, 0, heading, content, mergeSection);
      } catch (error) {
        profileStatus.textContent = error.status === 409
          ? `Profile changed elsewhere. Reload the current profile before saving again. ${error.message}`
          : `Could not save performer profile. ${error.message}`;
        saveProfile.disabled = false;
      } finally {
        if (dialog.open) {
          const capabilities = await getJson(`${V2_API}/capabilities`).catch(() => ({}));
          resetProfile.disabled = capabilities.actions_enabled !== true;
        }
      }
    });
    resetProfile.addEventListener('click', async () => {
      if (!window.confirm('Reset biography, artwork, and aliases to their saved provider values?')) return;
      saveProfile.disabled = resetProfile.disabled = true;
      profileStatus.textContent = 'Restoring provider values…';
      try {
        const result = await window.AdminV2.putJson(profileUrl, {
          expected_revision: Number(profileSection.dataset.revision),
          bio: bio.value, photo: photo.value,
          aliases: aliases.value.split(/\r?\n/).map(value => value.trim()).filter(Boolean),
          reset_fields: ['bio', 'photo', 'aliases'],
        });
        if (!dialog.open) return;
        profileSection.dataset.revision = String(result.profile.revision);
        bio.value = result.profile.bio || ''; photo.value = result.profile.photo || ''; aliases.value = (result.profile.aliases || []).join('\n');
        profileStatus.textContent = `Restored saved provider values at revision ${result.profile.revision}.`;
      } catch (error) {
        profileStatus.textContent = error.status === 409
          ? `Profile changed elsewhere. Reload before resetting. ${error.message}`
          : `Could not reset performer profile. ${error.message}`;
      } finally { if (dialog.open) await loadProfile(); }
    });
    reloadProfile.addEventListener('click', loadProfile);
    try {
      void loadProfile();
      await loadPersonFilmography(name, 0, heading, content, mergeSection);
    } catch (error) { content.append(create('p', 'state-banner', error.message)); }
  }
  async function loadPersonFilmography(name, offset, heading, content, mergeSection) {
    const requestSeq = ++state.personFilmographySeq;
    content.querySelector('.filmography-content')?.remove();
    const status = create('p', 'catalog-feedback', 'Loading filmography…');
    status.className = 'catalog-feedback filmography-content';
    content.insertBefore(status, mergeSection.isConnected ? mergeSection : null);
    try {
      const params = new URLSearchParams({ offset: String(offset), limit: '50' });
      const payload = await getJson(`${V2_API}/organize/people/${encodeURIComponent(name)}?${params}`);
      if (requestSeq !== state.personFilmographySeq) return;
      if (!payload?.ok) throw new Error(responseError(payload, 'Could not load filmography.'));
      const section = create('div', 'filmography-content');
      section.append(create('p', 'detail-description', `${payload.total} catalog videos`));
      const list = create('ul', 'person-filmography');
      for (const video of payload.filmography) {
        const item = create('li'); const link = create('a', 'text-link', video.title || 'Untitled'); link.href = `#film/${encodeURIComponent(video.id)}`; item.append(link);
        if (video.studio) item.append(create('span', 'detail-muted', ` · ${video.studio}`)); list.append(item);
      }
      section.append(list);
      const range = create('p', 'catalog-feedback', payload.total
        ? `${payload.offset + 1}–${Math.min(payload.offset + payload.filmography.length, payload.total)} of ${payload.total}`
        : 'No videos');
      range.setAttribute('aria-live', 'polite'); range.className = 'catalog-feedback filmography-range';
      section.append(range);
      if (payload.total > 50) {
        const pagination = create('div', 'pagination filmography-pagination');
        const previous = create('button', 'button button-quiet', 'Previous'); previous.type = 'button'; previous.disabled = payload.offset <= 0;
        previous.addEventListener('click', () => loadPersonFilmography(name, Math.max(0, payload.offset - payload.limit), heading, content, mergeSection));
        const next = create('button', 'button button-quiet', 'Next'); next.type = 'button'; next.disabled = payload.offset + payload.filmography.length >= payload.total;
        next.addEventListener('click', () => loadPersonFilmography(name, payload.offset + payload.limit, heading, content, mergeSection));
        pagination.append(previous, next); section.append(pagination);
      }
      content.querySelector('.filmography-content')?.remove();
      content.insertBefore(section, mergeSection.isConnected ? mergeSection : null);
      if (!mergeSection.isConnected) content.append(mergeSection);
    } catch (error) {
      if (requestSeq !== state.personFilmographySeq) return;
      const errorState = create('p', 'state-banner filmography-content', error.message);
      content.querySelector('.filmography-content')?.replaceWith(errorState);
      if (!errorState.isConnected) content.append(errorState);
    }
  }
  async function previewPersonMerge(alias) {
    const previewSeq = ++state.personMergePreviewSeq;
    const canonical = $('#personMergeCanonical').value.trim();
    const feedback = $('#personMergeFeedback');
    state.pendingPersonMerge = null;
    const existingConfirmation = $('#personMergeUndoConfirm');
    if (existingConfirmation) existingConfirmation.checked = false;
    const existingQueue = $('#personMergeQueue');
    if (existingQueue) existingQueue.disabled = true;
    if (!canonical || canonical.toLocaleLowerCase() === alias.toLocaleLowerCase()) { feedback.textContent = 'Enter a different canonical performer name.'; return; }
    feedback.textContent = 'Preparing merge preview…';
    try {
      const preview = await window.AdminV2.postJson(`${V2_API}/operations/preview`, { type: 'performer_merge', alias, canonical });
      if (previewSeq !== state.personMergePreviewSeq || !$('#personDetailDialog').open) return;
      const item = preview.items?.[0];
      if (!preview.plan_id || !item) throw new Error('Could not prepare merge preview.');
      state.pendingPersonMerge = { plan_id: preview.plan_id, alias: item.alias, canonical: item.canonical };
      const warnings = Array.isArray(preview.warnings) ? ` ${preview.warnings.join(' ')}` : '';
      feedback.textContent = `Preview: ${Number(item.alias_videos) || 0} videos use “${item.alias}”; ${Number(item.canonical_videos) || 0} use “${item.canonical}”. The worker will merge metadata and then refresh Jellyfin.${warnings}`;
      let confirmationLabel = $('#personMergeUndoWarning');
      if (!confirmationLabel) {
        confirmationLabel = create('label', 'state-banner person-merge-undo-warning');
        confirmationLabel.id = 'personMergeUndoWarning';
        const confirmation = document.createElement('input'); confirmation.id = 'personMergeUndoConfirm'; confirmation.type = 'checkbox';
        confirmation.addEventListener('change', () => { const queueButton = $('#personMergeQueue'); if (queueButton) queueButton.disabled = !confirmation.checked || !state.pendingPersonMerge; });
        confirmationLabel.append(confirmation, document.createTextNode(' I understand this performer merge cannot be undone in Admin v2.'));
        feedback.after(confirmationLabel);
      }
      let queue = $('#personMergeQueue');
      if (!queue) { queue = create('button', 'button button-primary', 'Queue performer merge'); queue.id = 'personMergeQueue'; queue.type = 'button'; queue.disabled = true; queue.addEventListener('click', queuePersonMerge); confirmationLabel.after(queue); }
    } catch (error) {
      if (previewSeq !== state.personMergePreviewSeq || !$('#personDetailDialog').open) return;
      feedback.textContent = error.message; state.pendingPersonMerge = null;
      const queue = $('#personMergeQueue'); if (queue) queue.disabled = true;
    }
  }
  async function queuePersonMerge() {
    const merge = state.pendingPersonMerge; if (!merge || !$('#personMergeUndoConfirm')?.checked) return;
    const requestSeq = state.personMergePreviewSeq;
    const route = state.route;
    const dialog = $('#personDetailDialog');
    if (!dialog.open) return;
    const button = $('#personMergeQueue');
    const restoreQueueFocus = document.activeElement === button;
    button.disabled = true;
    try {
      const result = await window.AdminV2.postJson(`${V2_API}/operations/execute`, { plan_id: merge.plan_id });
      if (state.route !== route) return;
      showQueued(result, 'Performer merge');
      if (requestSeq !== state.personMergePreviewSeq || !dialog.open) { loadPeople(); return; }
      closeDialog(dialog); loadPeople();
    } catch (error) {
      if (requestSeq !== state.personMergePreviewSeq || !dialog.open) return;
      const feedback = $('#personMergeFeedback');
      if (error.status === 409) {
        state.pendingPersonMerge = null;
        const confirmation = $('#personMergeUndoConfirm'); if (confirmation) confirmation.checked = false;
        button.disabled = true;
        feedback.textContent = `The merge preview is stale. ${error.message} Preview the merge again and review it before queueing.`;
        const previewButton = $('#personMergePreview');
        if (restoreQueueFocus && document.activeElement === document.body) previewButton?.focus({ preventScroll: true });
      } else {
        feedback.textContent = error.message; button.disabled = false;
        if (restoreQueueFocus && document.activeElement === document.body) button.focus({ preventScroll: true });
      }
    }
  }
  Object.assign(AV, { loadPeople });
})();
