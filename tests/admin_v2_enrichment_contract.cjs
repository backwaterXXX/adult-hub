const fs = require('node:fs');
const assert = require('node:assert/strict');

const template = fs.readFileSync('resolver/templates/admin_v2.html', 'utf8');
const suggestions = fs.readFileSync('resolver/static/admin-v2/quality.js', 'utf8');
const tasks = fs.readFileSync('resolver/templates/admin_v2.html', 'utf8');
assert.match(template, /id="librarySuggestionsPane"/, 'metadata suggestions remain in Library');
assert.match(template, /id="metadataProposalsRows"/, 'the suggestion queue stays available');
assert.doesNotMatch(template, /enrichmentScan|enrichmentRows|enrichmentSummary|enrichmentFreshness|External metadata progress/,
  'the removed enrichment monitoring and manual scan controls are absent');
assert.doesNotMatch(template, /assets-v2\/enrichment\.js/, 'the old polling controller is not loaded');
assert.doesNotMatch(suggestions, /metadata\/enrichment|setInterval|enrichmentScan/,
  'the retained suggestions controller does not poll or start a metadata scan');
assert.match(tasks, /value="metadata_backfill">External metadata scan/, 'automatic metadata work remains visible in Tasks');
assert.match(tasks, /value="metadata_quality_audit">Metadata quality audit/, 'automatic audit tasks remain available');
assert.match(fs.readFileSync('resolver/admin_v2_enrichment.py', 'utf8'), /metadata\/enrichment/,
  'Stage 5 leaves shared backend endpoints in place for Stage 6');
console.log('PASS: removed enrichment reports make no UI requests while Library suggestions and automatic task types remain');
