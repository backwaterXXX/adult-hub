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

assert.deepEqual({ ...status.activeTasks(undefined) }, {
  state: 'checking', label: 'Checking tasks…', count: null
});
assert.deepEqual({ ...status.activeTasks({ checking: true }) }, {
  state: 'checking', label: 'Checking tasks…', count: null
});
assert.deepEqual({ ...status.activeTasks(null) }, {
  state: 'unknown', label: 'Active tasks unavailable', count: null
});
assert.deepEqual({ ...status.activeTasks({ available: true, active_total: 0 }) }, {
  state: 'ready', label: 'Active tasks: 0', count: 0
});
assert.equal(status.activeTasks({ available: true, active_total: 7 }).label, 'Active tasks: 7');
assert.equal(status.activeTasks({ available: false, active_total: 0 }).state, 'unknown');
assert.equal(status.activeTasks({ available: true, active_total: null }).label, 'Active tasks unavailable');

assert.equal(status.syncStatusText({ available: false, running: false, status_text: 'Waiting for next run' }), 'No data');
assert.equal(status.syncStatusText({ available: true, running: true, last_success: false }), 'Sync in progress');
assert.equal(status.syncStatusText({ available: true, running: false, last_success: true }), 'Last run succeeded');
assert.equal(status.syncStatusText({ available: true, running: false, last_success: false }), 'Last run failed');
assert.equal(status.syncStatusText({ available: true, running: false, status_text: 'Waiting for next run' }), 'Waiting for next run');
assert.equal(status.syncStatusText({ available: false, freshness: 'unknown' }), 'No data');
assert.equal(status.syncStatusText({ available: true, running: false, last_success: true, freshness: 'stale' }), 'Status stale · Last run succeeded');
assert.equal(status.syncStatusText({ available: true, running: false, last_success: false, freshness: 'unknown' }), 'Freshness unknown · Last run failed');
assert.equal(status.syncStatusText({ available: true, running: false, freshness: 'unknown' }), 'No data');

// The header keeps one aggregate control, so its combined state must never
// read healthier than the weakest source it hides.
const healthy = { capabilities: { actions_enabled: true }, sync: { available: true, freshness: 'fresh', last_success: true }, activeTasks: { available: true, active_total: 2 } };
assert.deepEqual({ ...status.serviceStatus({ checking: true }) }, {
  state: 'checking', label: 'Checking services…', title: 'Service status is still loading.',
});
assert.equal(status.serviceStatus(null).state, 'checking', 'no data yet must read as loading, not as healthy');
assert.equal(status.serviceStatus({ ...healthy, connection: false }).state, 'error', 'a lost connection outranks current-looking details');
assert.equal(status.serviceStatus({ ...healthy, connection: false }).label, 'Connection unavailable');
assert.deepEqual({ ...status.serviceStatus({ connection: true, capabilities: null, sync: null, activeTasks: null }) }, {
  state: 'unknown', label: 'Status unverified', title: 'No admin status source could be verified.',
});
const workerPause = status.serviceStatus({
  ...healthy, connection: true, capabilities: { actions_enabled: false, read_only_reason: 'worker_unavailable' },
});
assert.equal(workerPause.state, 'attention');
assert.equal(workerPause.label, 'Attention needed');
assert.match(workerPause.title, /sync worker is unavailable/i, 'the aggregate control keeps the concrete write cause');
assert.equal(status.serviceStatus({
  ...healthy, connection: true, sync: { available: false },
}).state, 'partial', 'one unverified source must not read as fully healthy');
assert.match(status.serviceStatus({ ...healthy, connection: true, sync: { available: false } }).title, /Sync status unavailable/);
assert.equal(status.serviceStatus({
  ...healthy, connection: true, sync: { available: true, freshness: 'fresh', last_success: false },
}).state, 'attention', 'a failed synchronization reads as attention, not as success');
assert.equal(status.serviceStatus({ ...healthy, connection: true }).label, 'All services available',
  'aggregate service status does not duplicate the task count');

// Healthy access + healthy sync + activeTasks undefined/checking must not report ok
const healthyAccessSyncUndefinedTasks = {
  connection: true,
  capabilities: { actions_enabled: true },
  sync: { available: true, freshness: 'fresh', last_success: true },
  activeTasks: undefined,
};
const undefinedTaskServiceStatus = status.serviceStatus(healthyAccessSyncUndefinedTasks);
assert.notEqual(undefinedTaskServiceStatus.state, 'ok', 'active tasks undefined must prevent ok status');
assert.equal(undefinedTaskServiceStatus.state, 'checking');
assert.equal(undefinedTaskServiceStatus.label, 'Checking services…');

const healthyAccessSyncCheckingTasks = {
  ...healthyAccessSyncUndefinedTasks,
  activeTasks: { checking: true },
};
const checkingTaskServiceStatus = status.serviceStatus(healthyAccessSyncCheckingTasks);
assert.notEqual(checkingTaskServiceStatus.state, 'ok', 'active tasks checking must prevent ok status');
assert.equal(checkingTaskServiceStatus.state, 'checking');
assert.equal(checkingTaskServiceStatus.label, 'Checking services…');

// Attention needed from failed sync must not hide whether tasks are active
const failedSyncWithTasks = {
  ...healthy,
  connection: true,
  sync: { available: true, freshness: 'fresh', last_success: false },
  activeTasks: { available: true, active_total: 5 },
};
assert.equal(status.serviceStatus(failedSyncWithTasks).label, 'Attention needed');
assert.equal(status.serviceStatus(failedSyncWithTasks).state, 'attention');
assert.equal(status.activeTasks(failedSyncWithTasks.activeTasks).label, 'Active tasks: 5',
  'active task count remains readable when aggregate status needs attention');

const failedSyncZeroTasks = {
  ...healthy,
  connection: true,
  sync: { available: true, freshness: 'fresh', last_success: false },
  activeTasks: { available: true, active_total: 0 },
};
assert.equal(status.serviceStatus(failedSyncZeroTasks).label, 'Attention needed');
assert.equal(status.activeTasks(failedSyncZeroTasks.activeTasks).label, 'Active tasks: 0',
  'exact zero task count is preserved when sync needs attention');

const failedSyncUnavailableTasks = {
  ...healthy,
  connection: true,
  sync: { available: true, freshness: 'fresh', last_success: false },
  activeTasks: { available: false },
};
assert.equal(status.serviceStatus(failedSyncUnavailableTasks).label, 'Attention needed');
assert.equal(status.activeTasks(failedSyncUnavailableTasks.activeTasks).label, 'Active tasks unavailable',
  'task unavailability is preserved when sync needs attention');

console.log('PASS: Admin v2 header access modes, task counts, and the aggregate service state distinguish writable, token, read-only, zero, unavailable, and unverified states');
