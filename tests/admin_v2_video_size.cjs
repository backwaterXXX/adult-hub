const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('resolver/static/admin-v2/admin-v2.js', 'utf8');
const start = source.indexOf('  function fileSize(');
const end = source.indexOf('\n  function duration(', start);
assert.ok(start >= 0 && end > start);
const context = vm.createContext({});
vm.runInContext(`${source.slice(start, end)}\nglobalThis.format = fileSize;`, context);
assert.equal(context.format(500000000), '500.00 MB');
assert.equal(context.format(2500000000), '2.50 GB');
assert.equal(context.format(1000000000), '1.00 GB');
for (const value of [null, undefined, 0, -1, '', 'invalid']) {
  assert.equal(context.format(value), 'Not available');
}
console.log('PASS: video size uses MB/GB and handles missing sizes');
