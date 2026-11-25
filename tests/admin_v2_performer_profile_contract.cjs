const assert = require('node:assert/strict');
const fs = require('node:fs');
const { adminV2Source } = require('./helpers/admin_v2_scripts.cjs');

const script = adminV2Source();
const baseline = fs.readFileSync('common/migrations/schema_baseline.sql', 'utf8');

assert.match(script, /id = 'personProfileBio'/, 'the details dialog exposes biography editing');
assert.match(script, /id = 'personProfilePhoto'/, 'the details dialog exposes artwork URL editing');
assert.match(script, /id = 'personProfileAliases'/, 'the details dialog exposes alias management');
assert.match(script, /\$\{V2_API\}\/organize\/people\/\$\{encodeURIComponent\(name\)\}\/profile/,
  'profile details use the shared performer profile API');
assert.match(script, /expected_revision: Number\(profileSection\.dataset\.revision\)/,
  'updates use the revision displayed by the loaded profile');
assert.match(script, /error\.status === 409[\s\S]{0,180}Profile changed elsewhere/,
  'stale writes ask the user to reload rather than silently overwrite');
assert.match(script, /if \(!window\.confirm\('Reset biography, artwork, and aliases/,
  'restoring provider values requires explicit confirmation');
assert.match(script, /if \(!canWrite\) profileStatus\.textContent \+= ' Editing is unavailable in READ-ONLY mode\.'/,
  'read-only mode disables editing and explains why');
assert.match(script, /content\.insertBefore\(profileSection, heading\.nextSibling\)/,
  'the editor stays within the existing performer details dialog');
assert.match(script, /profile\.stashdb_url[\s\S]{0,120}StashDB/,
  'the details dialog renders clickable link to StashDB');
assert.match(script, /profile\.pbc_url[\s\S]{0,120}PBC/,
  'the details dialog renders clickable link to PBC');
assert.match(script, /\['Open in StashDB',\s*\['scene_match_url'\]\]/,
  'video detail renders StashDB scene match link');
assert.match(baseline, /CREATE TABLE public\.performer_profile_overrides/,
  'manual values have revisioned storage separate from provider-owned fields');
assert.match(baseline, /performer_profile_overrides_revision_check/,
  'manual values are revisioned so stale writes can be rejected');
assert.match(baseline, /'performer_profile_update'::text/,
  'the audit allowlist records performer profile updates');

for (const providerPath of [
  'sync/pbc_backfill.py', 'sync/theporndb.py',
  'sync/stashdb.py',
]) {
  const provider = fs.readFileSync(providerPath, 'utf8');
  assert.match(provider, /lock_profile_enrichment\(conn, /,
    `${providerPath} serializes provider writes against manual edits`);
  assert.match(provider, /get_override_fields\(conn, /,
    `${providerPath} checks active manual override fields before writing`);
}

console.log('Performer profile UI and migration contracts passed');
