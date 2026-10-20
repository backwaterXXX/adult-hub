"""Ensure only V2 Admin UI routes remain mounted while shared APIs survive."""

import ast
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLASSIC_API_URL = re.compile(r"/admin/api/(?!v2(?:/|['\"`]|$))")
V2_API_LITERAL = re.compile(r"['\"`](/admin/api/v2(?:/[A-Za-z0-9_./-]*)?)")


def declared_routes(paths, *, v2):
    found = set()
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if (not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute)
                        or not isinstance(decorator.func.value, ast.Name)
                        or decorator.func.value.id != "router" or not decorator.args):
                    continue
                route_arg = decorator.args[0]
                if not isinstance(route_arg, ast.Constant) or not isinstance(route_arg.value, str):
                    continue
                route = route_arg.value
                if not route.startswith("/api/") or route.startswith("/api/v2/") is not v2:
                    continue
                method = decorator.func.attr.lower()
                if method == "api_route":
                    methods_arg = next((kw.value for kw in decorator.keywords if kw.arg == "methods"), None)
                    methods = ({item.value.upper() for item in methods_arg.elts
                                if isinstance(item, ast.Constant) and isinstance(item.value, str)}
                               if isinstance(methods_arg, (ast.List, ast.Tuple, ast.Set)) else {"GET"})
                elif method in {"get", "post", "delete", "put", "patch"}:
                    methods = {method.upper()}
                else:
                    continue
                found.update(f"{verb} {route}" for verb in methods)
    return found


def declared_shared_resolver_routes():
    """Routes mounted under /admin for resolver/sync clients, not the Admin UI."""
    found = set()
    for source, receiver in ((ROOT / "resolver/app.py", "api"),
                             (ROOT / "resolver/catalog_api.py", "router")):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if (not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute)
                        or not isinstance(dec.func.value, ast.Name) or dec.func.value.id != receiver
                        or not dec.args):
                    continue
                arg = dec.args[0]
                if not isinstance(arg, ast.Constant) or not isinstance(arg.value, str):
                    continue
                route = arg.value
                if receiver == "api" and route not in {
                    "/health", "/api/performer-bio", "/play/{video_id:path}/stream",
                }:
                    continue
                if receiver == "router" and not route.startswith("/api/"):
                    continue
                if dec.func.attr == "api_route":
                    methods_arg = next((kw.value for kw in dec.keywords if kw.arg == "methods"), None)
                    methods = ({item.value.upper() for item in methods_arg.elts
                                if isinstance(item, ast.Constant) and isinstance(item.value, str)}
                               if isinstance(methods_arg, (ast.List, ast.Tuple, ast.Set)) else {"GET"})
                else:
                    methods = {dec.func.attr.upper()}
                prefix = "/admin"
                found.update((method, prefix + route, source.relative_to(ROOT).as_posix())
                             for method in methods)
    return found


class TestAdminV1V2RouteInventory(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture = ROOT / "tests/fixtures/v1-route-parity.json"
        cls.inventory = json.loads(fixture.read_text())

    def test_no_admin_v1_routes_remain_and_phash_client_routes_are_v2(self):
        source = (ROOT / "resolver/admin.py").read_text(encoding="utf-8")
        self.assertNotRegex(source, r"import admin_(?:status|diagnostics|library|maintenance|jobs|performers)\b")
        self.assertNotRegex(source, r"router\.include_router\(admin_(?:status|diagnostics|library|maintenance|jobs|performers)\.")
        routes = declared_routes(sorted((ROOT / "resolver").glob("admin*.py")), v2=False)
        self.assertEqual(routes, set())
        v2_routes = declared_routes(sorted((ROOT / "resolver").glob("admin*.py")), v2=True)
        expected_client_routes = {
            f"{item['method']} {item['path'].replace('/api/v1/', '/api/v2/').removeprefix('/admin')}"
            for item in self.inventory["client_integration_routes"]
        }
        self.assertTrue(expected_client_routes <= v2_routes)
        self.assertFalse((ROOT / "resolver/admin_v1_phash.py").exists())
        self.assertFalse((ROOT / "resolver/admin_v1_phash.py.orig").exists())
        self.assertIn("import admin_v2_phash", source)
        for module in ("admin_status.py", "admin_diagnostics.py", "admin_library.py",
                       "admin_maintenance.py", "admin_jobs.py", "admin_performers.py"):
            self.assertFalse((ROOT / "resolver" / module).exists(), module)

    def test_classic_ui_and_assets_are_absent(self):
        self.assertFalse((ROOT / "resolver/templates/admin.html").exists())
        self.assertFalse((ROOT / "resolver/static/admin").exists())
        source = (ROOT / "resolver/admin.py").read_text(encoding="utf-8")
        self.assertNotIn("/classic", source)
        template = (ROOT / "resolver/templates/admin_v2.html").read_text(encoding="utf-8")
        self.assertIn('lang="en"', template)

    def test_readme_documents_v2_and_does_not_recommend_retired_admin_api(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("## Admin v2 (`/admin/`)", readme)
        self.assertIn("`/admin/classic/` URL and all former Admin V1 API routes return 404", readme)
        self.assertNotIn("## Admin Dashboard", readme)
        for retired_route in (
            "/admin/api/capabilities", "/admin/api/jobs", "/admin/api/sync/trigger",
            "/admin/api/sources", "/admin/api/library/unavailable",
            "/admin/api/videos/duplicates", "/admin/api/performers/merge/preview",
        ):
            with self.subTest(route=retired_route):
                self.assertNotIn(retired_route, readme)

    def test_client_and_shared_routes_remain_classified_outside_admin_ui(self):
        actual = declared_shared_resolver_routes()
        inventory = {(item["method"], item["path"], item["source"])
                     for item in self.inventory["shared_resolver_routes"]}
        self.assertEqual(inventory, actual)
        for item in self.inventory["client_integration_routes"] + self.inventory["shared_resolver_routes"]:
            self.assertTrue(item.get("consumer"))
            self.assertTrue(item.get("purpose"))

    def test_admin_v2_browser_modules_do_not_call_retired_api_routes(self):
        offenders = []
        for path in sorted((ROOT / "resolver/static/admin-v2").glob("*.js")):
            source = path.read_text(encoding="utf-8")
            for line, content in enumerate(source.splitlines(), 1):
                if CLASSIC_API_URL.search(content):
                    offenders.append(f"{path.relative_to(ROOT)}:{line}")
        self.assertEqual(offenders, [])

    def test_literal_v2_browser_paths_resolve_to_registered_routes(self):
        registered = {route.split(" ", 1)[1] for route in declared_routes(
            sorted((ROOT / "resolver").glob("admin*.py")), v2=True)}
        offenders = []
        for path in sorted((ROOT / "resolver/static/admin-v2").glob("*.js")):
            source = path.read_text(encoding="utf-8")
            for match in V2_API_LITERAL.finditer(source):
                route = match.group(1).removeprefix("/admin").rstrip("/")
                if route and not any(item == route or item.startswith(route + "/")
                                     or item.startswith(route + "{") for item in registered):
                    offenders.append(f"{path.relative_to(ROOT)}:{source.count(chr(10), 0, match.start()) + 1}")
        self.assertEqual(offenders, [])

    def test_historical_migration_inventory_links_live_v2_evidence(self):
        for group in self.inventory["groups"] + self.inventory["ui_action_inventory"]:
            self.assertTrue(group.get("area"))
            self.assertTrue(group.get("v2_routes") or group.get("v2_replacement"))
            for relative in group.get("parity_tests", []):
                path = ROOT / relative
                self.assertTrue(path.is_file(), relative)
                self.assertEqual(path.parent, ROOT / "tests")

    def test_historical_migration_inventory_has_no_duplicate_test_evidence(self):
        for group in self.inventory["groups"] + self.inventory["ui_action_inventory"]:
            with self.subTest(area=group.get("area")):
                evidence = group.get("parity_tests", [])
                self.assertEqual(len(evidence), len(set(evidence)))


if __name__ == "__main__":
    unittest.main()
