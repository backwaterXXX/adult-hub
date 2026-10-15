const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');

const window = {};
vm.runInNewContext(
  fs.readFileSync('resolver/static/admin-v2/header-status.js', 'utf8'),
  { window, Number, Object, String }
);
const status = window.AdminV2HeaderStatus;

assert.deepEqual({ ...status.access(null) }, {
  state: 'unknown', label: 'Access unavailable', title: 'Admin access could not be verified.'
});
assert.equal(status.access({ actions_enabled: true }).label, 'Read & write');
assert.equal(status.access({ actions_enabled: true, token_required: true }).label, 'Writes require token');
assert.equal(status.access({ actions_enabled: false }).label, 'Read-only');
assert.match(status.access({ actions_enabled: false, read_only_reason: 'worker_unavailable' }).title, /worker is unavailable/);

assert.deepEqual({ ...status.activeTasks({ data: { available: true }, active_total: 0 }) }, {
  state: 'ready', label: 'Active tasks: 0', count: 0
});
assert.equal(status.activeTasks({ data: { available: true }, active_total: 7 }).label, 'Active tasks: 7');
assert.equal(status.activeTasks({ data: { available: false }, active_total: 0 }).state, 'unknown');
assert.equal(status.activeTasks({ data: { available: true }, active_total: null }).label, 'Active tasks unavailable');

assert.equal(status.syncStatusText({ available: false, running: false, status_text: 'Waiting for next run' }), 'No data');
assert.equal(status.syncStatusText({ available: true, running: true, last_success: false }), 'Sync in progress');
assert.equal(status.syncStatusText({ available: true, running: false, last_success: true }), 'Last run succeeded');
assert.equal(status.syncStatusText({ available: true, running: false, last_success: false }), 'Last run failed');
assert.equal(status.syncStatusText({ available: true, running: false, status_text: 'Waiting for next run' }), 'Waiting for next run');
assert.equal(status.syncStatusText({ available: false, freshness: 'unknown' }), 'No data');
assert.equal(status.syncStatusText({ available: true, running: false, last_success: true, freshness: 'stale' }), 'Status stale · Last run succeeded');
assert.equal(status.syncStatusText({ available: true, running: false, last_success: false, freshness: 'unknown' }), 'Freshness unknown · Last run failed');
assert.equal(status.syncStatusText({ available: true, running: false, freshness: 'unknown' }), 'No data');

console.log('PASS: Admin v2 topbar access mode and active-task count distinguish writable, token, read-only, zero, and unavailable states');
