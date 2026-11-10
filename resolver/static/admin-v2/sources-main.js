(() => {
  'use strict';
  // Sources event wiring and start-up.
  const SRC = window.AdminV2Sources;
  const { $, itemKey, setNotice, state } = SRC;
  const checkSourceAccess = (...args) => SRC.checkSourceAccess(...args);
  const closeSourceDetail = (...args) => SRC.closeSourceDetail(...args);
  const closeTelegramAccountActionDialog = (...args) => SRC.closeTelegramAccountActionDialog(...args);
  const closeTelegramAuthWizard = (...args) => SRC.closeTelegramAuthWizard(...args);
  const commitSourceUrl = (...args) => SRC.commitSourceUrl(...args);
  const loadSources = (...args) => SRC.loadSources(...args);
  const openAddChannel = (...args) => SRC.openAddChannel(...args);
  const openSourceDetail = (...args) => SRC.openSourceDetail(...args);
  const openTelegramAddAccountWizard = (...args) => SRC.openTelegramAddAccountWizard(...args);
  const previewNewChannel = (...args) => SRC.previewNewChannel(...args);
  const previewSourceEditor = (...args) => SRC.previewSourceEditor(...args);
  const removeSourceChannel = (...args) => SRC.removeSourceChannel(...args);
  const render = (...args) => SRC.render(...args);
  const renderChannelNameEditor = (...args) => SRC.renderChannelNameEditor(...args);
  const renderSourceDetail = (...args) => SRC.renderSourceDetail(...args);
  const renderSourceEditor = (...args) => SRC.renderSourceEditor(...args);
  const renderTelegramAccountCard = (...args) => SRC.renderTelegramAccountCard(...args);
  const restoreSourceStateFromHistory = (...args) => SRC.restoreSourceStateFromHistory(...args);
  const saveChannelName = (...args) => SRC.saveChannelName(...args);
  const saveNewChannel = (...args) => SRC.saveNewChannel(...args);
  const saveSourceEditor = (...args) => SRC.saveSourceEditor(...args);
  const scheduleSourceUrlUpdate = (...args) => SRC.scheduleSourceUrlUpdate(...args);
  const sync = (...args) => SRC.sync(...args);
  const syncNow = (...args) => SRC.syncNow(...args);
  function setupEvents() {
    const form = $('#sourcesFilters');
    form.addEventListener('submit', event => event.preventDefault());
    form.addEventListener('input', event => {
      if (event.target.name === 'q') {
        state.filter.q = event.target.value;
        state.filter.sourceKey = '';
        state.page = 0;
        state.accessFeedback = '';
        render();
        scheduleSourceUrlUpdate();
      }
    });
    form.addEventListener('change', event => {
      if (event.target.name === 'status') { state.filter.status = event.target.value; state.page = 0; render(); commitSourceUrl(); }
    });
    $('#sourcesClearFilters').addEventListener('click', () => {
      form.reset();
      state.filter = { q: '', status: '', sourceKey: '' };
      state.page = 0;
      render();
      commitSourceUrl();
    });
    $('#sourcesRefresh').addEventListener('click', () => { setNotice('', ''); return loadSources(); });
    $('#sourcesPagination').addEventListener('click', event => {
      const button = event.target.closest('[data-source-page]');
      if (!button || button.disabled) return;
      const direction = button.dataset.sourcePage;
      state.page += direction === 'next' ? 1 : -1;
      render();
      commitSourceUrl();
      const preferred = $(`[data-source-page="${direction}"]`);
      const fallback = $(`[data-source-page="${direction === 'next' ? 'previous' : 'next'}"]`);
      (preferred && !preferred.disabled ? preferred : fallback && !fallback.disabled ? fallback : $('#sourcesPageInfo'))
        ?.focus({ preventScroll: true });
    });
    $('#sourcesTableWrap').addEventListener('click', event => {
      const button = event.target.closest('[data-source-sort]');
      if (!button) return;
      const key = button.dataset.sourceSort;
      if (state.sort.key === key) {
        state.sort.direction = state.sort.direction === 'asc' ? 'desc' : 'asc';
      } else {
        state.sort = { key, direction: 'asc' };
      }
      state.page = 0;
      render();
      commitSourceUrl();
      button.focus({ preventScroll: true });
    });
    window.addEventListener('popstate', restoreSourceStateFromHistory);
    const telegramLoginBtn = $('#telegramLoginButton');
    if (telegramLoginBtn) {
      telegramLoginBtn.addEventListener('click', event => {
        return openTelegramAddAccountWizard(event.currentTarget);
      });
    }
    const accountsRefreshBtn = $('#telegramAccountsRefreshBtn');
    if (accountsRefreshBtn) {
      accountsRefreshBtn.addEventListener('click', () => renderTelegramAccountCard());
    }
    const telegramAuthClose = $('#telegramAuthClose');
    if (telegramAuthClose) {
      telegramAuthClose.addEventListener('click', () => closeTelegramAuthWizard());
    }
    const telegramAuthDialog = $('#telegramAuthDialog');
    if (telegramAuthDialog) {
      telegramAuthDialog.addEventListener('cancel', event => {
        event.preventDefault();
        closeTelegramAuthWizard();
      });
      telegramAuthDialog.addEventListener('click', event => {
        if (event.target === telegramAuthDialog) {
          closeTelegramAuthWizard();
        }
      });
    }
    const telegramAccountActionClose = $('#telegramAccountActionClose');
    if (telegramAccountActionClose) {
      telegramAccountActionClose.addEventListener('click', () => closeTelegramAccountActionDialog());
    }
    const telegramAccountActionDialog = $('#telegramAccountActionDialog');
    if (telegramAccountActionDialog) {
      telegramAccountActionDialog.addEventListener('cancel', event => {
        event.preventDefault();
        closeTelegramAccountActionDialog();
      });
      telegramAccountActionDialog.addEventListener('click', event => {
        if (event.target === telegramAccountActionDialog) {
          closeTelegramAccountActionDialog();
        }
      });
    }
    $('#sourcesAddChannel').addEventListener('click', event => openAddChannel(event.currentTarget));
    $('#sourcesRows').addEventListener('click', event => {
      const detail = event.target.closest('[data-source-detail]');
      if (detail) {
        const item = state.items.find(candidate => itemKey(candidate) === detail.dataset.sourceDetail);
        if (item) openSourceDetail(item, detail);
        return;
      }
      const button = event.target.closest('[data-sync-source]');
      if (button) syncNow(button.dataset.syncSource, button.dataset.syncMode || 'scheduled', button);
      const check = event.target.closest('[data-check-source]');
      if (check) checkSourceAccess(check.dataset.checkSource, check);
    });
    const dialog = $('#sourceDetailDialog');
    if (dialog) {
      dialog.addEventListener('click', event => {
        if (event.target === dialog) { closeSourceDetail(); return; }
        const sync = event.target.closest('[data-sync-source]');
        if (sync) { syncNow(sync.dataset.syncSource, sync.dataset.syncMode || 'scheduled', sync); return; }
        const check = event.target.closest('[data-check-source]');
        if (check) { checkSourceAccess(check.dataset.checkSource, check); return; }
        if (event.target.closest('[data-source-create-cancel]')) { state.editorCandidate = null; closeSourceDetail(); return; }
        const createPreview = event.target.closest('[data-source-create-preview]'); if (createPreview) { previewNewChannel(createPreview); return; }
        const createSave = event.target.closest('#sourceCreateSave'); if (createSave) { saveNewChannel(createSave); return; }
        const remove = event.target.closest('[data-source-remove]'); if (remove) { removeSourceChannel(remove); return; }
        if (event.target.closest('[data-source-edit-name]') && state.detailItem) { renderChannelNameEditor(state.detailItem); return; }
        if (event.target.closest('[data-source-edit-name-cancel]')) {
          if (state.detailItem) renderSourceDetail(state.detailItem);
          else closeSourceDetail();
          return;
        }
        const editNameSave = event.target.closest('#sourceEditNameSave');
        if (editNameSave) { saveChannelName(editNameSave); return; }
        const edit = event.target.closest('[data-source-edit]');
        if (edit && state.detailItem) { renderSourceEditor(state.detailItem); return; }
        if (event.target.closest('[data-source-editor-back]')) {
          state.editorCandidate = null;
          if (state.detailItem) renderSourceDetail(state.detailItem);
          return;
        }
        const preview = event.target.closest('[data-source-editor-preview]');
        if (preview) { previewSourceEditor(preview); return; }
        const save = event.target.closest('#sourceEditorSave');
        if (save) saveSourceEditor(save);
      });
      dialog.querySelectorAll('[data-source-detail-close]').forEach(button => {
        button.addEventListener('click', () => closeSourceDetail());
      });
      dialog.addEventListener('cancel', event => { event.preventDefault(); closeSourceDetail(); });
    }
    document.addEventListener('adminv2:navigate', sync);
  }

  setupEvents();
  sync();
})();
