const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const template = fs.readFileSync('resolver/templates/admin_v2.html', 'utf8');
assert(template.includes('src="/admin/assets-v2/organize-tabs.js"'), 'Admin V2 must load keyboard tab navigation');
for (const [tabId, paneId] of [
  ['duplicatesTab', 'organizeDuplicatesPane'],
  ['peopleTab', 'organizePeoplePane'],
  ['quarantineTab', 'organizeQuarantinePane'],
]) {
  assert(template.includes(`id="${tabId}" role="tab"`), `${tabId} must be an ARIA tab`);
  assert(template.includes(`id="${tabId}" role="tab" aria-selected="${tabId === 'duplicatesTab' ? 'true' : 'false'}" aria-controls="${paneId}"`),
    `${tabId} must point to its panel and start in the correct selected state`);
  assert(template.includes(`id="${paneId}" role="tabpanel" aria-labelledby="${tabId}"`),
    `${paneId} must be labelled by its controlling tab`);
}

class Element {
  constructor(id) { this.id = id; this.attributes = {}; this.listeners = {}; this.hidden = false; }
  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  focus() { document.activeElement = this; }
  click() { (this.listeners.click || []).forEach(listener => listener({ target: this })); }
}

const document = { activeElement: null };
const tabs = ['duplicates', 'people', 'quarantine'].map((name, index) => {
  const tab = new Element(`${name}Tab`);
  tab.setAttribute('aria-selected', index === 0 ? 'true' : 'false');
  return tab;
});
const panes = ['organizeDuplicatesPane', 'organizePeoplePane', 'organizeQuarantinePane']
  .map((id, index) => { const pane = new Element(id); pane.hidden = index !== 0; return pane; });
const tablist = new Element('organizeTabList');
tablist.querySelectorAll = selector => selector === '[role="tab"]' ? tabs : [];
document.querySelector = selector => selector === '#organizeTabList' ? tablist : null;

tabs.forEach((tab, index) => tab.addEventListener('click', () => {
  tabs.forEach((item, tabIndex) => item.setAttribute('aria-selected', tabIndex === index ? 'true' : 'false'));
  panes.forEach((pane, paneIndex) => { pane.hidden = paneIndex !== index; });
}));

vm.runInNewContext(fs.readFileSync('resolver/static/admin-v2/organize-tabs.js', 'utf8'), { document, Array });

function key(keyName, expectedIndex) {
  let prevented = false;
  tablist.listeners.keydown[0]({ key: keyName, preventDefault() { prevented = true; } });
  assert.equal(prevented, true, `${keyName} should be handled by the tablist`);
  assert.equal(document.activeElement, tabs[expectedIndex], `${keyName} should move focus to the right tab`);
  assert.equal(tabs[expectedIndex].getAttribute('aria-selected'), 'true');
  assert.equal(panes[expectedIndex].hidden, false);
  assert.deepEqual(tabs.map(tab => tab.getAttribute('tabindex')),
    tabs.map((_tab, index) => index === expectedIndex ? '0' : '-1'));
}

assert.deepEqual(tabs.map(tab => tab.getAttribute('tabindex')), ['0', '-1', '-1']);
tabs[1].focus(); key('ArrowRight', 2);
key('ArrowRight', 0);
key('ArrowLeft', 2);
key('Home', 0);
key('End', 2);
tabs[1].click();
assert.deepEqual(tabs.map(tab => tab.getAttribute('tabindex')), ['-1', '0', '-1'],
  'pointer activation should update the roving tab stop');
process.stdout.write('PASS: Organize tabs support arrow/Home/End navigation, panel selection, and a single tab stop\n');
