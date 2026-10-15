const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

let activeDocument;
class Element {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase(); this.children = []; this.listeners = {}; this.attributes = {};
    this.dataset = {}; this.hidden = false; this.disabled = false; this.value = ''; this._textContent = '';
    this.className = ''; this.id = ''; this.open = false; this.isConnected = true;
  }
  addEventListener(type, callback) { this.listeners[type] = callback; }
  click() { this.listeners.click?.({ target: this }); }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  get textContent() { return this._textContent + this.children.map(node => node.textContent).join(''); }
  set textContent(value) { this._textContent = String(value); this.children = []; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  removeAttribute(name) { delete this.attributes[name]; }
  focus() { if (activeDocument) activeDocument.activeElement = this; }
  showModal() { this.open = true; }
  close() { this.open = false; }
  scrollIntoView() {}
  querySelectorAll(selector) {
    const all = descendants(this).slice(1);
    if (selector === '[role="option"]') return all.filter(item => item.getAttribute('role') === 'option');
    return [];
  }
  querySelector(selector) {
    if (selector.startsWith('#')) return descendants(this).find(item => item.id === selector.slice(1)) || null;
    return null;
  }
}
function descendants(root) { return [root, ...(root.children || []).flatMap(descendants)]; }
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

(async () => {
  const ids = ['commandPalette', 'commandPaletteTrigger', 'commandPaletteClose', 'commandPaletteQuery', 'commandPaletteResults',
    'commandPaletteStatus', 'commandPaletteShortcut', 'adminAccessMode', 'main-content', 'clearResolverCache',
    'refreshJellyfinLibrary', 'triggerFullSync', 'qualityRunAudit', 'qualityAuditMode', 'backupCreate', 'detailTitle', 'detailCheckAvailability',
    'personDetailDialog', 'personDetailTitle', 'personMergeCanonical', 'sourceDetailDialog', 'sourceDetailTitle',
    'sourceDetailContent', 'sourceDetailSyncNew', 'sourceDetailCheckAccess'];
  const elements = new Map(ids.map(id => { const item = new Element(id === 'commandPalette' ? 'dialog' : 'div'); item.id = id; return [id, item]; }));
  elements.get('adminAccessMode').dataset.state = 'readonly';
  const documentListeners = new Map();
  const location = { href: 'http://localhost/admin/v2/#start', hash: '#start', search: '' };
  const history = { pushState(_state, _title, href) {
    const url = new URL(href, location.href); location.href = url.href; location.hash = url.hash; location.search = url.search;
  } };
  const calls = [];
  let writeAllowed = true;
  let clickCount = 0;
  let scopedClickCount = 0;
  for (const id of ['clearResolverCache', 'refreshJellyfinLibrary', 'triggerFullSync', 'qualityRunAudit', 'backupCreate']) {
    elements.get(id).addEventListener('click', () => { clickCount += 1; });
  }
  elements.get('qualityAuditMode').value = 'metadata_quality';
  elements.get('detailCheckAvailability').addEventListener('click', () => { scopedClickCount += 1; });
  elements.get('sourceDetailSyncNew').addEventListener('click', () => { scopedClickCount += 1; });
  elements.get('sourceDetailCheckAccess').addEventListener('click', () => { scopedClickCount += 1; });
  const document = activeDocument = {
    activeElement: new Element('body'),
    querySelector(selector) { return selector.startsWith('#') ? elements.get(selector.slice(1)) || null : null; },
    createElement(tag) { return new Element(tag); },
    addEventListener(type, callback) { documentListeners.set(type, [...(documentListeners.get(type) || []), callback]); },
    removeEventListener(type, callback) { documentListeners.set(type, (documentListeners.get(type) || []).filter(item => item !== callback)); },
    dispatchEvent(event) { for (const callback of documentListeners.get(event.type) || []) callback(event); },
  };
  const window = {
    AdminV2: { async getJson(url) {
      calls.push(url);
      if (url.endsWith('/capabilities')) return { actions_enabled: writeAllowed };
      const query = new URL(url, 'http://localhost').searchParams.get('q') || '';
      const delay = query === 'alpha' ? 240 : query === 'beta' ? 10 : 0;
      if (delay) await wait(delay);
      if (url.includes('/library/videos')) return { items: query ? [{ id: `${query}-video`, title: `${query} video`, provider: 'telegram' }] : [] };
      if (url.includes('/performers/catalog')) return { items: query ? [{ id: `${query}-person`, name: `${query} performer` }] : [] };
      if (url.includes('/sources/search')) return { items: query ? [{ id: `${query}-channel`, label: `${query} channel`, provider: 'telegram', enabled: true }] : [] };
      throw new Error(`Unexpected URL: ${url}`);
    } },
    dispatchEvent(event) {
      if (event.type !== 'popstate') return;
      const route = location.hash.slice(1).split(/[?&]/, 1)[0];
      const params = new URLSearchParams(location.hash.includes('?') ? location.hash.slice(location.hash.indexOf('?') + 1) : '');
      const mapped = route.startsWith('film/') ? 'library'
        : route === 'performers' && params.get('tab') === 'directory' ? 'organize' : route;
      document.dispatchEvent({ type: 'adminv2:navigate', detail: { route: mapped } });
    },
  };
  class PopStateEvent { constructor(type) { this.type = type; } }
  const context = {
    document, window, location, history, URL, URLSearchParams, AbortController, PopStateEvent,
    navigator: { platform: 'Linux x86_64', userAgent: 'Test Browser' },
    requestAnimationFrame: callback => callback(), setTimeout, clearTimeout, console,
  };
  vm.runInNewContext(fs.readFileSync('resolver/static/admin-v2/command-palette.js', 'utf8'), context);
  const palette = window.AdminV2CommandPalette;
  const enrichmentPage = palette.pages.find(page => page.label === 'Catalog Health · Enrichment');
  assert.equal(enrichmentPage?.route, '#quality?tab=enrichment',
    'the Command Palette must navigate directly to the Catalog Health Enrichment tab');
  assert.equal(palette.score('clear cache', ['Clear resolver cache', 'cache clear reset']), 4,
    'semantic synonyms match a multiword command');
  assert.ok(Number.isFinite(palette.score('performr', ['Performer directory'])), 'one-character typos are tolerated');
  assert.equal(elements.get('commandPaletteShortcut').textContent, 'Ctrl K');
  const fullAudit = palette.actions.find(action => action.label === 'Run full audit');
  assert.equal(fullAudit?.mode, 'full', 'the palette audit action is explicit about running the full audit mode');
  assert.equal(palette.actions.find(action => action.label === 'Run metadata quality audit')?.mode, 'metadata_quality');
  assert.equal(palette.actions.find(action => action.label === 'Run file consistency audit')?.mode, 'file_consistency');
  assert.equal(palette.actions.find(action => action.label === 'Create database backup')?.route, '#system?tab=backups',
    'backup command navigates to the visible Backups tab before invoking its handler');

  let input = elements.get('commandPaletteQuery');
  elements.get('commandPaletteTrigger').listeners.click();
  assert.equal(elements.get('commandPalette').open, true, 'clicking the visible trigger opens the modal');
  assert.equal(document.activeElement, elements.get('commandPaletteQuery'), 'opening moves focus into the combobox');
  elements.get('commandPaletteClose').listeners.click();
  assert.equal(elements.get('commandPalette').open, false, 'the visible close button closes the dialog on touch devices');
  elements.get('commandPaletteTrigger').listeners.click();
  assert.equal(document.activeElement, elements.get('commandPaletteQuery'));
  documentListeners.get('keydown').at(-1)({ key: 'k', ctrlKey: true, metaKey: false, isComposing: true, preventDefault() { throw new Error('IME shortcut must not be captured'); } });
  input = elements.get('commandPaletteQuery');
  input.listeners.keydown({ key: 'Escape', isComposing: false, preventDefault() {} });
  assert.equal(elements.get('commandPalette').open, false, 'Escape closes the dialog');
  assert.equal(document.activeElement, elements.get('commandPaletteTrigger'), 'closing restores focus to the opener');
  documentListeners.get('keydown').at(-1)({ key: 'k', ctrlKey: true, metaKey: false, isComposing: false, preventDefault() { this.prevented = true; } });
  assert.equal(elements.get('commandPalette').open, true, 'Ctrl+K opens the palette on Linux');

  input = elements.get('commandPaletteQuery');
  input.value = 'alpha'; input.listeners.input();
  await wait(170);
  input.value = 'beta'; input.listeners.input();
  await wait(190);
  let rendered = descendants(elements.get('commandPaletteResults')).map(item => item.textContent).join(' ');
  assert.match(rendered, /beta video/);
  assert.doesNotMatch(rendered, /alpha video/, 'a slower stale response cannot replace the latest results');
  assert.ok(calls.some(url => url.includes('/sources/search?q=beta')), 'channel lookup uses the bounded search endpoint');

  input.value = 'clear cache'; input.listeners.input(); await wait(180);
  let options = elements.get('commandPaletteResults').querySelectorAll('[role="option"]');
  let disabled = options.find(option => option.textContent.includes('Clear resolver cache'));
  assert.equal(disabled.getAttribute('aria-disabled'), 'true', 'mutating actions are disabled in read-only mode');
  assert.match(disabled.textContent, /READ-ONLY/);
  const arrowEvent = { key: 'ArrowDown', isComposing: false, preventDefault() { this.prevented = true; } };
  input.listeners.keydown(arrowEvent);
  assert.equal(arrowEvent.prevented, true, 'arrow navigation prevents page scrolling');

  elements.get('adminAccessMode').dataset.state = 'writable';
  input.value = 'refresh jellyfin'; input.listeners.input(); await wait(180);
  options = elements.get('commandPaletteResults').querySelectorAll('[role="option"]');
  const action = options.find(option => option.textContent.includes('Refresh Jellyfin library'));
  assert.ok(action, 'the command registry provides a Jellyfin action result');
  for (let i = 0; i < Number(action.id.replace('command-result-', '')); i += 1) {
    input.listeners.keydown({ key: 'ArrowDown', isComposing: false, preventDefault() {} });
  }
  input.listeners.keydown({ key: 'Enter', isComposing: false, preventDefault() {} });
  await wait(20);
  assert.equal(clickCount, 1, 'Enter reuses the existing action button handler after server permission check');
  assert.equal(location.hash, '#system');
  assert.equal(elements.get('commandPalette').open, false);
  palette.open(elements.get('commandPaletteTrigger'));
  input.value = 'run audit'; input.listeners.input(); await wait(180);
  options = elements.get('commandPaletteResults').querySelectorAll('[role="option"]');
  const auditAction = options.find(option => option.textContent.includes('Run full audit'));
  assert.ok(auditAction, 'the audit command is discoverable by the generic run audit phrase');
  for (let i = 0; i < Number(auditAction.id.replace('command-result-', '')); i += 1) {
    input.listeners.keydown({ key: 'ArrowDown', isComposing: false, preventDefault() {} });
  }
  input.listeners.keydown({ key: 'Enter', isComposing: false, preventDefault() {} });
  await wait(20);
  assert.equal(elements.get('qualityAuditMode').value, 'full', 'palette selects full mode before invoking the existing audit confirmation handler');
  assert.equal(clickCount, 2);
  assert.equal(location.hash, '#quality?tab=issues');
  location.href = 'http://localhost/admin/v2/#film/alpha%3Avideo';
  location.hash = '#film/alpha%3Avideo';
  elements.get('detailTitle').textContent = 'Alpha movie';
  palette.open(elements.get('commandPaletteTrigger'));
  input.value = 'check availability'; input.listeners.input(); await wait(180);
  options = elements.get('commandPaletteResults').querySelectorAll('[role="option"]');
  const scoped = options.find(option => option.textContent.includes('Check availability — Alpha movie'));
  assert.ok(scoped, 'a detail context adds a clearly scoped availability action');
  for (let i = 0; i < Number(scoped.id.replace('command-result-', '')); i += 1) {
    input.listeners.keydown({ key: 'ArrowDown', isComposing: false, preventDefault() {} });
  }
  input.listeners.keydown({ key: 'Enter', isComposing: false, preventDefault() {} });
  await wait(20);
  assert.equal(scopedClickCount, 1, 'scoped availability reuses the video detail handler and preview flow');

  elements.get('personDetailDialog').open = true;
  elements.get('personDetailTitle').textContent = 'Jane Performer';
  location.hash = '#performers?tab=directory';
  palette.open(elements.get('commandPaletteTrigger'));
  input = elements.get('commandPaletteQuery'); input.value = 'review merge'; input.listeners.input(); await wait(180);
  options = elements.get('commandPaletteResults').querySelectorAll('[role="option"]');
  const performerAction = options.find(option => option.textContent.includes('Review merge for Jane Performer'));
  assert.ok(performerAction, 'an open performer filmography exposes a clearly scoped merge review command');
  for (let i = 0; i < Number(performerAction.id.replace('command-result-', '')); i += 1) {
    input.listeners.keydown({ key: 'ArrowDown', isComposing: false, preventDefault() {} });
  }
  input.listeners.keydown({ key: 'Enter', isComposing: false, preventDefault() {} });
  await wait(20);
  assert.equal(document.activeElement, elements.get('personMergeCanonical'), 'performer merge review only focuses the existing canonical field');
  assert.equal(scopedClickCount, 1, 'reviewing a performer merge does not execute a mutation');

  elements.get('personDetailDialog').open = false;
  elements.get('sourceDetailDialog').open = true;
  elements.get('sourceDetailTitle').textContent = 'Fixture Channel';
  elements.get('sourceDetailContent').dataset.sourceId = 'fixture-channel';
  elements.get('sourceDetailContent').dataset.sourceProvider = 'telegram';
  location.hash = '#sources';
  palette.open(elements.get('commandPaletteTrigger'));
  input = elements.get('commandPaletteQuery'); input.value = 'sync new'; input.listeners.input(); await wait(180);
  options = elements.get('commandPaletteResults').querySelectorAll('[role="option"]');
  const channelAction = options.find(option => option.textContent.includes('Sync new — Fixture Channel'));
  assert.ok(channelAction, 'an open source drawer exposes a channel-scoped sync action');
  for (let i = 0; i < Number(channelAction.id.replace('command-result-', '')); i += 1) {
    input.listeners.keydown({ key: 'ArrowDown', isComposing: false, preventDefault() {} });
  }
  input.listeners.keydown({ key: 'Enter', isComposing: false, preventDefault() {} });
  await wait(20);
  assert.equal(scopedClickCount, 2, 'channel sync reuses its existing detail control after server capability recheck');
  process.stdout.write('PASS: Command Palette shortcuts, IME, ranking, stale lookups, permission gating, and scoped video/person/source actions\n');
})().catch(error => { console.error(error); process.exitCode = 1; });
