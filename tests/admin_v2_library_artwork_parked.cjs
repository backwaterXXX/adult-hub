'use strict';
// Contract: a video whose artwork extraction is parked says so in the library,
// instead of looking like ordinary missing artwork.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.join(__dirname, '..');
const source = fs.readFileSync(
  path.join(ROOT, 'resolver', 'static', 'admin-v2', 'admin-v2-library.js'),
  'utf8',
);

assert.match(source, /video\.artwork_parked/, 'the library reads the parked artwork flag');
assert.match(
  source,
  /create\('span', 'status-pill pill-warning', 'Artwork parked'\)/,
  'the parked state renders as a warning pill',
);
assert.equal(
  (source.match(/'Artwork parked'/g) || []).length,
  2,
  'both the card view and the table view show the parked state',
);

process.stdout.write('PASS: parked artwork is visible in the library card and table views\n');
