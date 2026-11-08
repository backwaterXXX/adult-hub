const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { createAdminV2Helpers } = require('./helpers/admin_v2_shared_helpers.cjs');

function createDeferred() {
  let resolve, reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

let activeElement = null;
class Element {
  constructor(tag = 'div') {
    this.tagName = tag; this.children = []; this.listeners = {}; this.dataset = {};
    this.hidden = false; this.disabled = false; this.value = ''; this.textContent = '';
    this.className = ''; this.isConnected = true; this.attributes = {}; this.noValidate = false;
  }
  addEventListener(type, callback) { this.listeners[type] = callback; }
  append(...nodes) { nodes.forEach(node => { node.parentElement = this; }); this.children.push(...nodes); }
  appendChild(node) { this.append(node); return node; }
  replaceChildren(...nodes) { nodes.forEach(node => { node.parentElement = this; }); this.children = nodes; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  removeAttribute(name) { delete this.attributes[name]; }
  querySelector(selector) {
    if (selector.startsWith('#')) {
      const id = selector.slice(1);
      return descendants(this, node => node.id === id)[0] || null;
    }
    if (selector.startsWith('.')) return this.children.find(node => (node.className || '').split(/\s+/).includes(selector.slice(1))) || null;
    return null;
  }
  reportValidity() { return true; }
  select() {}
  get elements() {
    const inputs = descendants(this, node => ['input', 'select', 'textarea'].includes(node.tagName));
    const named = {};
    inputs.forEach(input => { if (input.name) named[input.name] = input; });
    return named;
  }
  closest(selector) {
    let curr = this;
    while (curr) {
      if (selector.startsWith('#') && curr.id === selector.slice(1)) return curr;
      if (selector.startsWith('.') && curr.className && curr.className.split(/\s+/).includes(selector.slice(1))) return curr;
      curr = curr.parentElement;
    }
    return null;
  }
  focus() { if (!this.disabled) activeElement = this; }
  querySelectorAll() { return []; }
}

function descendants(root, predicate) {
  return [
    ...(predicate(root) ? [root] : []),
    ...(root.children || []).flatMap(child => descendants(child, predicate)),
  ];
}

(async () => {
  const ids = [
    'sourcesView', 'sourcesFilters', 'sourcesClearFilters', 'sourcesRefresh', 'sourcesAddChannel',
    'sourcesStatusFilter', 'sourcesRows', 'sourcesPagination', 'sourceDetailDialog', 'sourcesNotice',
    'sourcesFeedback', 'sourceDetailContent', 'sourceChannelsPane', 'sourceHistoryPane',
    'sourcesHistoryFeedback', 'sourcesHistoryRows', 'sourcesHistoryPagination', 'sourcesState',
    'sourcesTableWrap', 'sourcesQuery', 'liveMessage', 'crumbCurrent',
    'operationalEyebrow', 'operationalTitle', 'operationalIntro',
    'telegramLoginButton', 'telegramAccountsRefreshBtn', 'telegramAuthClose',
    'telegramAuthDialog', 'telegramAuthTitle', 'telegramAuthBanner', 'telegramAuthBody',
    'telegramAccountIntro', 'telegramAccountsContainer', 'telegramAccountBanner', 'telegramAccountCard'
  ];
  const elements = Object.fromEntries(ids.map(id => [id, new Element(id === 'sourcesFilters' ? 'form' : id === 'telegramAuthDialog' ? 'dialog' : 'div')]));
  const document = {
    get activeElement() { return activeElement; },
    querySelector(selector) {
      if (selector.startsWith('#')) {
        const id = selector.slice(1);
        if (elements[id]) return elements[id];
        for (const root of Object.values(elements)) {
          const match = descendants(root, node => node.id === id)[0];
          if (match) return match;
        }
        return null;
      }
      return null;
    },
    querySelectorAll: () => [],
    createElement: tag => new Element(tag),
    addEventListener() {},
  };

  const postCalls = [];
  let existingAccounts = [{ account_id: 'default', label: 'Default Account', authorized: false }];
  let authStatusResponse = { status: 'unconfigured', message: 'Ready for auth' };

  const window = {
    addEventListener() {},
    prompt() {
      throw new Error('Native prompt() must not be called when adding Telegram account');
    },
    confirm() { return true; },
    AdminV2: {
      ...createAdminV2Helpers(document),
      async getJson(path) {
        if (path.endsWith('/capabilities')) {
          return { token_required: false };
        }
        if (path.endsWith('/sources/list')) {
          return { available: true, managed_revision: 0, applied_revision: 0, sources: [] };
        }
        if (path.endsWith('/sources/settings')) {
          return { revision: 0, overrides: {} };
        }
        if (path.includes('/sync-history')) {
          return { runs: [] };
        }
        if (path.endsWith('/telegram/accounts')) {
          return { accounts: existingAccounts };
        }
        if (path.includes('/telegram/auth/status')) {
          return authStatusResponse;
        }
        throw new Error(`Unexpected GET ${path}`);
      },
      openDialog(dialog) { if (dialog) dialog.open = true; },
      closeDialog(dialog) { if (dialog) dialog.open = false; },
    },
  };

  const location = { hash: '#sources', search: '', href: 'http://localhost/admin/v2/#sources' };
  const history = {
    replaceState() {},
    pushState() {},
  };

  let postFetchHandler = null;
  const customFetch = async (url, options = {}) => {
    const payload = options.body ? JSON.parse(options.body) : null;
    postCalls.push({ path: url, payload, method: options.method });

    if (postFetchHandler) {
      const resp = await postFetchHandler(url, options, payload);
      if (resp) return resp;
    }

    return {
      ok: true,
      status: 200,
      async json() { return { ok: true, account_id: payload?.account_id }; }
    };
  };

  const context = {
    window, document, location, history, URL, URLSearchParams, AbortController,
    Headers, fetch: customFetch,
    setTimeout, clearTimeout, setImmediate, Intl, Date, Map, Set, Array, Object, Number, String, Boolean, Math, Symbol,
  };

  vm.runInNewContext(fs.readFileSync('resolver/static/admin-v2/sources.js', 'utf8'), context);
  await new Promise(resolve => setImmediate(resolve));

  // 1. Verify clicking telegramLoginButton opens telegramAuthDialog with Add Account screen without calling prompt()
  const loginBtn = elements.telegramLoginButton;
  assert.ok(loginBtn.listeners.click, 'telegramLoginButton has click listener');
  await loginBtn.listeners.click({ currentTarget: loginBtn });

  assert.equal(elements.telegramAuthDialog.open, true, 'dialog is opened');
  assert.equal(elements.telegramAuthTitle.textContent, 'Add Telegram Account', 'dialog title reflects Add Telegram Account');

  const idInput = descendants(elements.telegramAuthBody, node => node.id === 'tgAccountIdInput')[0];
  const labelInput = descendants(elements.telegramAuthBody, node => node.id === 'tgAccountLabelInput')[0];
  const continueBtn = descendants(elements.telegramAuthBody, node => node.id === 'tgAddAccountContinue')[0];
  const cancelBtn = descendants(elements.telegramAuthBody, node => node.id === 'tgAddAccountCancel')[0];
  const addForm = descendants(elements.telegramAuthBody, node => node.id === 'tgAddAccountForm')[0];
  const errorEl = descendants(elements.telegramAuthBody, node => node.id === 'tgAddAccountError')[0];

  assert.ok(idInput, 'Account ID input exists');
  assert.ok(labelInput, 'Label input exists');
  assert.ok(continueBtn, 'Continue button exists');
  assert.ok(cancelBtn, 'Cancel button exists');
  assert.ok(addForm, 'Add Account form exists');
  assert.ok(errorEl, 'Inline error element exists');
  assert.equal(errorEl.hidden, true, 'Inline error initially hidden');
  assert.equal(idInput.value, 'account_2', 'Default account ID is prefilled based on existing accounts');
  assert.equal(idInput.getAttribute('aria-invalid'), null, 'aria-invalid not initially set');
  assert.equal(idInput.getAttribute('aria-describedby'), null, 'aria-describedby not initially set');

  // 2. Validate empty and whitespace submission: form has noValidate to avoid browser bubble; inline error displayed
  assert.equal(addForm.noValidate, true, 'Form has noValidate=true to prevent native browser bubble');
  assert.equal(idInput.getAttribute('aria-required'), 'true', 'Account ID input has aria-required="true" for accessibility');

  idInput.value = '';
  let prevented = false;
  await addForm.listeners.submit({ preventDefault() { prevented = true; } });
  assert.equal(prevented, true, 'Form submit prevented default on empty ID');
  assert.equal(postCalls.length, 0, 'No account POST occurred on empty validation failure');
  assert.equal(errorEl.hidden, false, 'Inline error is shown for empty ID');
  assert.match(errorEl.textContent, /Account ID is required/i, 'Error text indicates Account ID is required');
  assert.equal(idInput.getAttribute('aria-invalid'), 'true', 'idInput has aria-invalid="true" on error');
  assert.equal(idInput.getAttribute('aria-describedby'), 'tgAddAccountError', 'idInput has aria-describedby pointing to inline error');
  assert.equal(elements.telegramAuthBanner.hidden, true, 'Single announcement: telegramAuthBanner stays hidden');

  // Verify clearing error when typing
  idInput.value = 'valid_name';
  idInput.listeners.input();
  assert.equal(errorEl.hidden, true, 'Inline error is hidden when user types');
  assert.equal(idInput.getAttribute('aria-invalid'), null, 'aria-invalid is cleared when user types');
  assert.equal(idInput.getAttribute('aria-describedby'), null, 'aria-describedby is cleared when user types');

  idInput.value = '   ';
  prevented = false;
  await addForm.listeners.submit({ preventDefault() { prevented = true; } });
  assert.equal(prevented, true, 'Form submit prevented default on whitespace ID');
  assert.equal(postCalls.length, 0, 'No account POST occurred on whitespace validation failure');
  assert.equal(errorEl.hidden, false, 'Inline error is shown for whitespace ID');
  assert.match(errorEl.textContent, /Account ID is required/i, 'Error text indicates Account ID is required');
  assert.equal(idInput.getAttribute('aria-invalid'), 'true', 'idInput has aria-invalid="true" on error');
  assert.equal(idInput.getAttribute('aria-describedby'), 'tgAddAccountError', 'idInput has aria-describedby on error');
  assert.equal(elements.telegramAuthBanner.hidden, true, 'Single announcement: telegramAuthBanner stays hidden');

  // 3. Validate safe character and length contract before endpoint path (Finding 7)
  for (const invalidId of ['bad/id', 'bad\\id', '../evil', 'bad id', 'worker;drop', 'a'.repeat(65)]) {
    idInput.value = invalidId;
    await addForm.listeners.submit({ preventDefault() {} });
    assert.equal(postCalls.length, 0, `No POST sent for invalid ID ${invalidId}`);
    assert.equal(errorEl.hidden, false, `Inline error shown for invalid ID ${invalidId}`);
    assert.match(errorEl.textContent, /must contain only alphanumeric characters/i, `Validation message matches safe contract for ${invalidId}`);
    assert.equal(idInput.getAttribute('aria-invalid'), 'true', `idInput has aria-invalid for ${invalidId}`);
    assert.equal(idInput.getAttribute('aria-describedby'), 'tgAddAccountError');
    assert.equal(elements.telegramAuthBanner.hidden, true, 'Single announcement: telegramAuthBanner stays hidden');
  }

  // 4. Cancel before submit closes dialog and creates no account
  cancelBtn.listeners.click({});
  assert.equal(elements.telegramAuthDialog.open, false, 'Dialog is closed after Cancel');
  assert.equal(postCalls.length, 0, 'No account created on Cancel');

  // Verify Escape / dialog cancel event closes dialog and clears draft
  await loginBtn.listeners.click({ currentTarget: loginBtn });
  const escIdInput = descendants(elements.telegramAuthBody, node => node.id === 'tgAccountIdInput')[0];
  escIdInput.value = 'draft_to_discard';
  escIdInput.listeners.input();
  elements.telegramAuthDialog.listeners.cancel({ preventDefault() {} });
  assert.equal(elements.telegramAuthDialog.open, false, 'Dialog is closed on Escape/cancel');
  assert.equal(postCalls.length, 0, 'No account created on Escape');

  // Re-open and verify draft was cleared (shows prefilled account_2 instead of discarded draft)
  await loginBtn.listeners.click({ currentTarget: loginBtn });
  const reopenedIdInput = descendants(elements.telegramAuthBody, node => node.id === 'tgAccountIdInput')[0];
  assert.equal(reopenedIdInput.value, 'account_2', 'Draft was cleared after close');

  // 5. Verify API rejection simulation through postTelegramAuth path (HTTP 409 Conflict)
  postFetchHandler = async (url, options, payload) => {
    if (payload?.account_id === 'worker_exists') {
      return {
        ok: false,
        status: 409,
        async json() { return { ok: false, code: 'account_exists', error: "Account 'worker_exists' already exists." }; }
      };
    }
    return null;
  };

  const retryForm = descendants(elements.telegramAuthBody, node => node.id === 'tgAddAccountForm')[0];
  const retryContinueBtn = descendants(elements.telegramAuthBody, node => node.id === 'tgAddAccountContinue')[0];
  const retryErrorEl = descendants(elements.telegramAuthBody, node => node.id === 'tgAddAccountError')[0];

  reopenedIdInput.value = 'worker_exists';
  await retryForm.listeners.submit({ preventDefault() {} });

  assert.equal(retryErrorEl.hidden, false, 'Inline error is shown when backend rejects creation');
  assert.match(retryErrorEl.textContent, /Account 'worker_exists' already exists/i, 'Error message matches backend 409 response');
  assert.equal(reopenedIdInput.getAttribute('aria-invalid'), 'true', 'aria-invalid set on backend rejection');
  assert.equal(reopenedIdInput.getAttribute('aria-describedby'), 'tgAddAccountError', 'aria-describedby set on backend rejection');
  assert.equal(elements.telegramAuthBanner.hidden, true, 'Single announcement: telegramAuthBanner remains hidden on inline error');
  assert.equal(retryContinueBtn.disabled, false, 'Continue button is NOT disabled so keyboard focus remains stable');
  assert.equal(retryContinueBtn.getAttribute('aria-disabled'), null, 'Continue button aria-disabled cleared for retry');

  // 6. Test closing while POST is pending & double-submit guard (Findings 1, 2, 3)
  reopenedIdInput.value = 'worker_pending';
  const pendingStart = createDeferred();
  const pendingGate = createDeferred();

  postFetchHandler = async (url, options, payload) => {
    if (payload?.account_id === 'worker_pending') {
      pendingStart.resolve();
      await pendingGate.promise;
      existingAccounts.push({ account_id: 'worker_pending', label: 'worker_pending', authorized: false });
      return {
        ok: true,
        status: 201,
        async json() { return { ok: true, account: { account_id: 'worker_pending', label: 'worker_pending' } }; }
      };
    }
    return null;
  };

  const postCountBeforePending = postCalls.length;
  const submitPromise = retryForm.listeners.submit({ preventDefault() {} });
  await pendingStart.promise;
  assert.equal(postCalls.length, postCountBeforePending + 1, 'First submit initiated POST');

  // Verify accessibility during submit: aria-disabled is set, but button is NOT disabled to keep focus stable
  assert.equal(retryContinueBtn.getAttribute('aria-disabled'), 'true', 'Continue button has aria-disabled="true" during submit');
  assert.equal(retryContinueBtn.disabled, false, 'Continue button is NOT disabled during submit to keep keyboard focus stable');

  // Double submit while pending must be blocked
  await retryForm.listeners.submit({ preventDefault() {} });
  assert.equal(postCalls.length, postCountBeforePending + 1, 'Double-submit while pending was blocked');

  // User closes / dismisses the dialog while POST is in-flight
  elements.telegramAuthDialog.listeners.cancel({ preventDefault() {} });
  assert.equal(elements.telegramAuthDialog.open, false, 'Dialog closed while POST is pending');

  // Resolve the in-flight POST
  pendingGate.resolve();
  await submitPromise;

  // Must NOT reopen into the login wizard when dismissed
  assert.equal(elements.telegramAuthDialog.open, false, 'Dialog must not reopen into login wizard after user dismissed it');
  // Must show non-modal notice
  assert.equal(elements.sourcesNotice.hidden, false, 'Non-modal notice is displayed after background completion');
  const noticeChildren = descendants(elements.sourcesNotice, () => true).map(n => n.textContent).join(' ');
  assert.match(noticeChildren, /worker_pending/i, 'Non-modal notice mentions created account');

  // 7. Closed-dialog failure test branch (Finding 4)
  await loginBtn.listeners.click({ currentTarget: loginBtn });
  assert.equal(elements.telegramAuthDialog.open, true, 'Dialog opened for closed-failure test');

  const failIdInput = descendants(elements.telegramAuthBody, node => node.id === 'tgAccountIdInput')[0];
  const failForm = descendants(elements.telegramAuthBody, node => node.id === 'tgAddAccountForm')[0];
  failIdInput.value = 'worker_fail';

  const failStart = createDeferred();
  const failGate = createDeferred();

  postFetchHandler = async (url, options, payload) => {
    if (payload?.account_id === 'worker_fail') {
      failStart.resolve();
      await failGate.promise;
      return {
        ok: false,
        status: 500,
        async json() { return { ok: false, error: 'Database timeout on account creation' }; }
      };
    }
    return null;
  };

  const failSubmitPromise = failForm.listeners.submit({ preventDefault() {} });
  await failStart.promise;

  // Close dialog while failure POST is in flight
  elements.telegramAuthDialog.listeners.cancel({ preventDefault() {} });
  assert.equal(elements.telegramAuthDialog.open, false, 'Dialog closed while failing POST was pending');

  // Let failing POST complete
  failGate.resolve();
  await failSubmitPromise;

  assert.equal(elements.telegramAuthDialog.open, false, 'Dialog remains closed after background failure');
  assert.equal(elements.sourcesNotice.hidden, false, 'Notice is shown for background failure');
  assert.match(elements.sourcesNotice.className, /state-error/, 'Notice has error state for background failure');
  const failNoticeText = descendants(elements.sourcesNotice, () => true).map(n => n.textContent).join(' ');
  assert.match(failNoticeText, /worker_fail/i, 'Notice mentions failed account ID');
  assert.match(failNoticeText, /Database timeout on account creation/i, 'Notice mentions backend error message');

  // 8. Close / Reopen / Second submit regression test (Finding 2)
  await loginBtn.listeners.click({ currentTarget: loginBtn });
  assert.equal(elements.telegramAuthDialog.open, true, 'Dialog opened for close/reopen test');

  const reopen1IdInput = descendants(elements.telegramAuthBody, node => node.id === 'tgAccountIdInput')[0];
  const reopen1Form = descendants(elements.telegramAuthBody, node => node.id === 'tgAddAccountForm')[0];
  reopen1IdInput.value = 'worker_reopen_1';

  const reopen1Start = createDeferred();
  const reopen1Gate = createDeferred();

  postFetchHandler = async (url, options, payload) => {
    if (payload?.account_id === 'worker_reopen_1') {
      reopen1Start.resolve();
      await reopen1Gate.promise;
      existingAccounts.push({ account_id: 'worker_reopen_1', label: 'worker_reopen_1', authorized: false });
      return {
        ok: true,
        status: 201,
        async json() { return { ok: true, account: { account_id: 'worker_reopen_1', label: 'worker_reopen_1' } }; }
      };
    }
    if (payload?.account_id === 'worker_reopen_2') {
      existingAccounts.push({ account_id: 'worker_reopen_2', label: 'worker_reopen_2', authorized: false });
      return {
        ok: true,
        status: 201,
        async json() { return { ok: true, account: { account_id: 'worker_reopen_2', label: 'worker_reopen_2' } }; }
      };
    }
    return null;
  };

  const reopen1SubmitPromise = reopen1Form.listeners.submit({ preventDefault() {} });
  await reopen1Start.promise;

  // Close dialog while POST 1 is in-flight
  elements.telegramAuthDialog.listeners.cancel({ preventDefault() {} });
  assert.equal(elements.telegramAuthDialog.open, false, 'Dialog closed while POST 1 is pending');

  // Reopen dialog while POST 1 is STILL in-flight
  await loginBtn.listeners.click({ currentTarget: loginBtn });
  assert.equal(elements.telegramAuthDialog.open, true, 'Dialog reopened while POST 1 is pending');

  const reopen2ContinueBtn = descendants(elements.telegramAuthBody, node => node.id === 'tgAddAccountContinue')[0];
  const reopen2Form = descendants(elements.telegramAuthBody, node => node.id === 'tgAddAccountForm')[0];
  const reopen2IdInput = descendants(elements.telegramAuthBody, node => node.id === 'tgAccountIdInput')[0];

  assert.equal(reopen2ContinueBtn.getAttribute('aria-disabled'), 'true', 'Reopened continue button reflects pending request');

  // Attempt second submit while first is still pending: must be blocked
  const postCountBeforeSecondSubmit = postCalls.length;
  reopen2IdInput.value = 'worker_reopen_2';
  await reopen2Form.listeners.submit({ preventDefault() {} });
  assert.equal(postCalls.length, postCountBeforeSecondSubmit, 'Second submit while first POST is pending was blocked');

  // Now resolve first POST
  reopen1Gate.resolve();
  await reopen1SubmitPromise;

  // Notice shown for worker_reopen_1
  assert.equal(elements.sourcesNotice.hidden, false, 'Notice shown for background success of first account');
  const reopen1NoticeText = descendants(elements.sourcesNotice, () => true).map(n => n.textContent).join(' ');
  assert.match(reopen1NoticeText, /worker_reopen_1/i, 'Notice mentions worker_reopen_1');

  // Continue button in reopened dialog should now be re-enabled
  assert.equal(reopen2ContinueBtn.getAttribute('aria-disabled'), null, 'Continue button re-enabled after first POST completed');

  // Now submit second account successfully
  const postCountBeforeSecondSend = postCalls.length;
  await reopen2Form.listeners.submit({ preventDefault() {} });
  assert.equal(postCalls.length, postCountBeforeSecondSend + 1, 'Second account creation POST was sent');
  assert.equal(postCalls[postCountBeforeSecondSend].payload.account_id, 'worker_reopen_2');

  // Close wizard
  elements.telegramAuthClose.listeners.click();
  assert.equal(elements.telegramAuthDialog.open, false, 'Dialog closed via close button');

  // 9. Test empty-label fallback: whitespace label falls back to account_id
  postFetchHandler = null;
  await loginBtn.listeners.click({ currentTarget: loginBtn });

  const fallbackIdInput = descendants(elements.telegramAuthBody, node => node.id === 'tgAccountIdInput')[0];
  const fallbackLabelInput = descendants(elements.telegramAuthBody, node => node.id === 'tgAccountLabelInput')[0];
  const fallbackForm = descendants(elements.telegramAuthBody, node => node.id === 'tgAddAccountForm')[0];

  fallbackIdInput.value = 'worker_fallback';
  fallbackLabelInput.value = '   '; // whitespace-only label

  const postCountBeforeFallback = postCalls.length;
  await fallbackForm.listeners.submit({ preventDefault() {} });

  assert.equal(postCalls.length, postCountBeforeFallback + 1, 'Account creation POST sent');
  assert.equal(postCalls[postCountBeforeFallback].path, '/admin/api/v2/telegram/accounts');
  assert.deepEqual(postCalls[postCountBeforeFallback].payload, { account_id: 'worker_fallback', label: 'worker_fallback' }, 'Empty label fell back to normalizedId');
  assert.equal(elements.telegramAuthTitle.textContent, 'Telegram Login: worker_fallback', 'Transitions to login wizard for new account');

  // Close the wizard
  elements.telegramAuthClose.listeners.click();
  assert.equal(elements.telegramAuthDialog.open, false, 'Dialog closed via close button');

  // 10. Verify-session async race regression test: switching accounts while verification is pending (Finding 1)
  existingAccounts.push(
    { account_id: 'acc_verify_a', label: 'Verify A', authorized: false, has_session_file: true },
    { account_id: 'acc_verify_b', label: 'Verify B', authorized: false, has_session_file: true }
  );

  const verifyAStart = createDeferred();
  const verifyAGate = createDeferred();

  const originalGetJson = window.AdminV2.getJson;
  window.AdminV2.getJson = async function (path) {
    if (path.includes('account_id=acc_verify_a') && path.includes('check_live=false')) {
      return { status: 'session_file_present', message: 'Session file present for acc_verify_a' };
    }
    if (path.includes('account_id=acc_verify_a') && path.includes('check_live=true')) {
      verifyAStart.resolve();
      await verifyAGate.promise;
      return { status: 'authorized', message: 'acc_verify_a verified successfully' };
    }
    if (path.includes('account_id=acc_verify_b') && path.includes('check_live=false')) {
      return { status: 'session_file_present', message: 'Session file present for acc_verify_b' };
    }
    return originalGetJson.call(this, path);
  };

  // Refresh account cards
  await elements.telegramAccountsRefreshBtn.listeners.click();

  const cards = descendants(elements.telegramAccountsContainer, n => (n.className || '').includes('telegram-account-card'));
  const cardA = cards.find(c => descendants(c, n => (n.textContent || '').includes('acc_verify_a')).length > 0);
  const cardB = cards.find(c => descendants(c, n => (n.textContent || '').includes('acc_verify_b')).length > 0);
  assert.ok(cardA, 'cardA found');
  assert.ok(cardB, 'cardB found');

  const verifyLoginBtnA = descendants(cardA, n => n.tagName === 'button' && n.textContent === 'Verify session')[0];
  const verifyLoginBtnB = descendants(cardB, n => n.tagName === 'button' && n.textContent === 'Verify session')[0];
  assert.ok(verifyLoginBtnA, 'verifyLoginBtnA found');
  assert.ok(verifyLoginBtnB, 'verifyLoginBtnB found');

  // Open wizard for acc_verify_a
  await verifyLoginBtnA.listeners.click();
  assert.equal(elements.telegramAuthTitle.textContent, 'Telegram Login: acc_verify_a');

  // Click "Verify session" inside wizard
  const verifyBtnInsideA = descendants(elements.telegramAuthBody, n => n.tagName === 'button' && n.textContent === 'Verify session')[0];
  assert.ok(verifyBtnInsideA, 'Verify session button found inside wizard');

  const verifyPromiseA = verifyBtnInsideA.listeners.click();
  await verifyAStart.promise;
  assert.equal(verifyBtnInsideA.textContent, 'Verifying…', 'Verify button shows Verifying… state');

  // While acc_verify_a live verification is pending, switch accounts to acc_verify_b
  await verifyLoginBtnB.listeners.click();
  assert.equal(elements.telegramAuthTitle.textContent, 'Telegram Login: acc_verify_b', 'Wizard switched to acc_verify_b');

  // Now resolve the live check for acc_verify_a
  verifyAGate.resolve();
  await verifyPromiseA;

  // Stale acc_verify_a response must be discarded: dialog must remain on acc_verify_b!
  assert.equal(elements.telegramAuthTitle.textContent, 'Telegram Login: acc_verify_b', 'Title remains acc_verify_b; stale verification discarded');
  const currentWizardText = descendants(elements.telegramAuthBody, () => true).map(n => n.textContent).join(' ');
  assert.doesNotMatch(currentWizardText, /already authorized and active/i, 'Authorized view of acc_verify_a was not rendered');
  assert.match(currentWizardText, /Session file present for acc_verify_b/i, 'Wizard body still displays acc_verify_b session view');

  // Close wizard
  elements.telegramAuthClose.listeners.click();
  assert.equal(elements.telegramAuthDialog.open, false, 'Dialog closed via close button');

  // 11. Test overlapping Login opens and sequence guard
  existingAccounts.push(
    { account_id: 'account_slow', label: 'Account Slow', authorized: false },
    { account_id: 'account_fast', label: 'Account Fast', authorized: false }
  );

  const slowStatusStart = createDeferred();
  const slowStatusGate = createDeferred();

  window.AdminV2.getJson = async function (path) {
    if (path.includes('account_id=account_slow')) {
      slowStatusStart.resolve();
      await slowStatusGate.promise;
      return { status: 'in_progress', login_state: 'code_needed', phone_mask: '+1234567890' };
    }
    if (path.includes('account_id=account_fast')) {
      return { status: 'unconfigured', message: 'Fast unconfigured account' };
    }
    return originalGetJson.call(this, path);
  };

  // Refresh account cards to include account_slow and account_fast
  await elements.telegramAccountsRefreshBtn.listeners.click();

  const refreshedCards = descendants(elements.telegramAccountsContainer, n => (n.className || '').includes('telegram-account-card'));
  const slowCard = refreshedCards.find(card => descendants(card, n => (n.textContent || '').includes('account_slow')).length > 0);
  const fastCard = refreshedCards.find(card => descendants(card, n => (n.textContent || '').includes('account_fast')).length > 0);
  assert.ok(slowCard, 'slowCard found');
  assert.ok(fastCard, 'fastCard found');

  const slowLoginBtn = descendants(slowCard, n => n.tagName === 'button' && n.textContent === 'Log in')[0];
  const fastLoginBtn = descendants(fastCard, n => n.tagName === 'button' && n.textContent === 'Log in')[0];
  assert.ok(slowLoginBtn, 'slowLoginBtn found');
  assert.ok(fastLoginBtn, 'fastLoginBtn found');

  // Trigger login for account_slow (starts slow request)
  const slowLoginPromise = slowLoginBtn.listeners.click();
  await slowStatusStart.promise;

  // Immediately trigger login for account_fast (supersedes account_slow)
  await fastLoginBtn.listeners.click();
  assert.equal(elements.telegramAuthTitle.textContent, 'Telegram Login: account_fast', 'Title updated to account_fast');

  // Now let the slow status check resolve
  slowStatusGate.resolve();
  await slowLoginPromise;

  // The stale account_slow response must be discarded; dialog remains showing account_fast
  assert.equal(elements.telegramAuthTitle.textContent, 'Telegram Login: account_fast', 'Stale response discarded by sequence guard');
  const wizardText = descendants(elements.telegramAuthBody, () => true).map(n => n.textContent).join(' ');
  assert.doesNotMatch(wizardText, /\+1234567890/, 'Stale code step from account_slow was not rendered');

  window.AdminV2.getJson = originalGetJson;

  process.stdout.write('PASS: Telegram Add Account dialog screen replaces prompt, validates inline, and creates account only on Continue\n');
})().catch(error => { console.error(error); process.exitCode = 1; });
