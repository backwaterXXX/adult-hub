const fs = require('fs');
const assert = require('assert/strict');

const classicTemplate = 'resolver/templates/admin.html';
const classicAssets = 'resolver/static/admin';
const v2Template = fs.readFileSync('resolver/templates/admin_v2.html', 'utf8');
const adminRoutes = fs.readFileSync('resolver/admin.py', 'utf8');
const appRoutes = fs.readFileSync('resolver/app.py', 'utf8');
const v2Script = fs.readFileSync('resolver/static/admin-v2/admin-v2.js', 'utf8');

assert.equal(fs.existsSync(classicTemplate), false, 'Classic HTML must be removed');
assert.equal(fs.existsSync(classicAssets), false, 'Classic browser assets must be removed');
assert(v2Template.includes('<html lang="en">'), 'The sole Admin UI must remain English');
assert(v2Template.includes('/admin/assets-v2/admin-v2.css'), 'The sole Admin UI must load v2 styles');
assert(!v2Template.includes('/admin/classic/'), 'V2 must not link back to the retired panel');
assert(!appRoutes.includes('resolver/static/admin"'), 'The app must not mount Classic assets');
assert(!adminRoutes.includes('ADMIN_V2_DEFAULT'), 'The retired route-switch setting must be gone');
assert(!adminRoutes.includes('admin_classic_page'), 'The old Classic URL must not be registered');
assert(!/^import (admin_status|admin_diagnostics|admin_library|admin_maintenance|admin_jobs|admin_performers)\b/m.test(adminRoutes),
  'Legacy V1 routers must not be imported by the active Admin router');
assert.match(adminRoutes, /async def admin_page\(request: Request\):\s+return await admin_v2_page\(request\)/,
  'The root Admin route must always serve V2');
assert(!v2Script.includes("legacy: '/admin/classic/#"), 'V2 must not keep per-section Classic fallbacks');
