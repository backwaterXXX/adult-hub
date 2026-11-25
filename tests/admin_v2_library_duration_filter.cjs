'use strict';
// Contract: the Admin V2 library exposes min/max duration filters and sends them
// with every catalog request, including URL sync and saved views.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { adminV2Source } = require('./helpers/admin_v2_scripts.cjs');

const ROOT = path.join(__dirname, '..');
const template = fs.readFileSync(path.join(ROOT, 'resolver', 'templates', 'admin_v2.html'), 'utf8');
const formStart = template.indexOf('id="libraryFilters"');
const formEnd = template.indexOf('</form>', formStart);
assert.ok(formStart >= 0 && formEnd > formStart, 'libraryFilters form must exist');
const formMarkup = template.slice(formStart, formEnd);

for (const [id, name, label] of [
  ['filterMinDuration', 'min_duration', 'Min duration (s)'],
  ['filterMaxDuration', 'max_duration', 'Max duration (s)'],
]) {
  assert.match(formMarkup, new RegExp(`id="${id}"[^>]*name="${name}"`), `${id} input must exist`);
  assert.ok(
    formMarkup.includes(`<label for="${id}">${label}</label>`),
    `${id} must be labelled`,
  );
  assert.match(formMarkup, new RegExp(`name="${name}"[^>]*type="number"`), `${name} must be numeric`);
}

const source = adminV2Source();
const start = source.indexOf('  function filterParams() {');
assert.ok(start >= 0, 'filterParams must exist in the Admin V2 parts');
const end = source.indexOf('\n  function resetCatalogPage(', start);
assert.ok(end > start, 'resetCatalogPage marker must follow filterParams');

const values = {
  q: '  ',
  channel_id: '',
  studio: '',
  performer: '',
  tag: '',
  availability: '',
  missing: '',
  min_duration: ' 300 ',
  max_duration: '900',
  sort: 'last_seen',
};
const form = {
  elements: {
    namedItem: name => ({ value: values[name] ?? '' }),
  },
};
const context = {
  $: selector => {
    assert.equal(selector, '#libraryFilters');
    return form;
  },
  state: { offset: 0, catalogCursor: null, catalogCursorDirection: 'after' },
  URLSearchParams,
  String,
};
vm.runInNewContext(
  `${source.slice(start, end)}\nglobalThis.filterParams = filterParams;`,
  context,
);

const params = context.filterParams();
assert.equal(params.get('min_duration'), '300', 'min duration must be trimmed and sent');
assert.equal(params.get('max_duration'), '900', 'max duration must be sent');
assert.equal(params.get('q'), null, 'blank search text must not be sent');

values.min_duration = '';
values.max_duration = '';
const empty = context.filterParams();
assert.equal(empty.get('min_duration'), null, 'empty min duration must be omitted');
assert.equal(empty.get('max_duration'), null, 'empty max duration must be omitted');

process.stdout.write('PASS: Library duration filters reach catalog requests and stay out when empty\n');
