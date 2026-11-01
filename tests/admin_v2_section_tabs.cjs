const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const template = fs.readFileSync('resolver/templates/admin_v2.html', 'utf8');
assert(template.includes('src="/admin/assets-v2/section-tabs.js"'), 'Admin V2 must load the shared keyboard tab navigation');
assert.doesNotMatch(template, /organize-tabs\.js/, 'the removed Organize host must not keep its own tab script');

const tablists = [
  ['libraryTabList', [['libraryVideosTab', '#library', 'libraryVideosPane'],
    ['libraryDuplicatesTab', '#library?tab=duplicates', 'libraryDuplicatesPane'],
    ['librarySuggestionsTab', '#library?tab=suggestions', 'librarySuggestionsPane'],
    ['libraryQuarantineTab', '#library?tab=quarantine', 'libraryQuarantinePane']]],
  ['performersTabList', [['performerDirectoryTab', '#performers?tab=directory', 'performerDirectoryPane'],
    ['performerReviewTab', '#performers?tab=review', 'performerReviewPane']]],
];
for (const [listId, tabs] of tablists) {
  assert(template.includes(`id="${listId}" role="tablist"`), `${listId} must be an ARIA tablist`);
  for (const [tabId, href, paneId] of tabs) {
    assert(new RegExp(`href="${href.replace(/[?]/g, '\\?')}" id="${tabId}" role="tab"`).test(template),
      `${tabId} must stay a link so the tab keeps its own address`);
    assert(template.includes(`aria-controls="${paneId}"`), `${tabId} must point to its panel`);
    assert(template.includes(`id="${paneId}" role="tabpanel" aria-labelledby="${tabId}"`),
      `${paneId} must be labelled by its controlling tab`);
  }
}

class Element {
  constructor(id) { this.id = id; this.attributes = {}; this.listeners = {}; this.hidden = false; this.navigation = 0; }
  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  focus() { document.activeElement = this; }
  click() { this.navigation += 1; (this.listeners.click || []).forEach(listener => listener({ target: this })); }
}

const document = { activeElement: null, listeners: {}, addEventListener(name, callback) { this.listeners[name] = callback; } };
// The router owns aria-selected and panel visibility; the fake reproduces that on activation.
const groups = tablists.map(([listId, tabs]) => {
  const elements = tabs.map(([tabId, , paneId], index) => {
    const tab = new Element(tabId);
    tab.setAttribute('aria-selected', index === 0 ? 'true' : 'false');
    const pane = new Element(paneId);
    pane.hidden = index !== 0;
    tab.pane = pane;
    return tab;
  });
  const list = new Element(listId);
  list.querySelectorAll = selector => selector === '[role="tab"]' ? elements : [];
  elements.forEach((tab, index) => tab.addEventListener('click', () => {
    elements.forEach((item, tabIndex) => {
      item.setAttribute('aria-selected', tabIndex === index ? 'true' : 'false');
      item.pane.hidden = tabIndex !== index;
    });
  }));
  return { list, elements };
});
document.querySelectorAll = selector => selector === 'nav.section-tabs[role="tablist"]'
  ? groups.map(group => group.list) : [];
document.querySelector = () => null;

vm.runInNewContext(fs.readFileSync('resolver/static/admin-v2/section-tabs.js', 'utf8'), { document, Array });

function step(group, keyName, expectedIndex) {
  const { list, elements } = group;
  let prevented = false;
  list.listeners.keydown[0]({ key: keyName, preventDefault() { prevented = true; } });
  assert.equal(prevented, true, `${keyName} should be handled by the tablist`);
  assert.equal(document.activeElement, elements[expectedIndex], `${keyName} should move focus to the right tab`);
  assert.equal(elements[expectedIndex].getAttribute('aria-selected'), 'true', `${keyName} activates the target tab`);
  assert.equal(elements[expectedIndex].pane.hidden, false, `${keyName} shows the target panel`);
  assert.deepEqual(elements.map(tab => tab.getAttribute('tabindex')),
    elements.map((_tab, index) => index === expectedIndex ? '0' : '-1'));
}

for (const group of groups) {
  const tabs = group.elements;
  const count = tabs.length;
  assert.deepEqual(tabs.map(tab => tab.getAttribute('tabindex')),
    tabs.map((_tab, index) => index === 0 ? '0' : '-1'), `${group.list.id} starts with one tab stop`);
  let cursor = 1 % count;
  tabs[cursor].focus();
  cursor = (cursor + 1) % count;
  step(group, 'ArrowRight', cursor);
  assert.ok(tabs[cursor].navigation > 0, `${group.list.id} keyboard activation follows the tab link`);
  cursor = (cursor + 1) % count;
  step(group, 'ArrowRight', cursor);
  cursor = (cursor - 1 + count) % count;
  step(group, 'ArrowLeft', cursor);
  cursor = 0;
  step(group, 'Home', cursor);
  cursor = count - 1;
  step(group, 'End', cursor);
  cursor = 1 % count;
  tabs[cursor].click();
  assert.deepEqual(tabs.map(tab => tab.getAttribute('tabindex')),
    tabs.map((_tab, index) => index === cursor ? '0' : '-1'),
    `${group.list.id} pointer activation should update the roving tab stop`);
  assert.equal(tabs.filter(tab => tab.getAttribute('aria-selected') === 'true').length, 1,
    `${group.list.id} keeps exactly one selected tab`);
}
const performers = groups[1];
assert.equal(performers.elements[0].getAttribute('tabindex'), '-1');
let outsidePrevented = false;
performers.list.listeners.keydown[0]({ key: 'ArrowDown', preventDefault() { outsidePrevented = true; } });
assert.equal(outsidePrevented, false, 'unhandled keys must stay available to the page');
// Each strip keeps its own tab stop, so a Library selection cannot steal the Performers stop.
assert.equal(groups[0].elements[1].getAttribute('tabindex'), '0');
assert.equal(groups[1].elements[1].getAttribute('tabindex'), '0');
assert.equal(groups[1].elements[0].getAttribute('aria-selected'), 'false');
process.stdout.write('PASS: Library and Performers tab lists support arrow/Home/End navigation, panel selection, and a single tab stop\n');

// Browser Back/Forward changes selection without clicking a tab.
for (const { elements } of groups) {
  elements.forEach((tab, index) => tab.setAttribute('aria-selected', String(index === 1)));
}
document.listeners['adminv2:navigate']();
for (const { elements } of groups) {
  assert.equal(elements[1].getAttribute('tabindex'), '0');
  assert.equal(elements[0].getAttribute('tabindex'), '-1');
}
