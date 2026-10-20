"""Database-free checks for accessible names in the Admin V2 template."""

from html.parser import HTMLParser
from pathlib import Path
import re
import unittest


TEMPLATE = Path(__file__).parents[1] / "resolver/templates/admin_v2.html"
STATIC_DIR = Path(__file__).parents[1] / "resolver/static/admin-v2"
POLISH_DIACRITICS = re.compile(r"[ąćęłńóśźżĄĆĘŁŃÓŚŹŻ]")
COMMON_POLISH_UI_WORDS = re.compile(
    r"\b(?:blad\w*|bled\w*|brak\w*|zapis\w*|usun\w*|dodaj|anuluj|potwierdz\w*|"
    r"plik\w*|kolejk\w*|zadani\w*|ustawien\w*|wybierz|odswiez\w*|wycofaj|"
    r"niedostep\w*|zrod\w*|harmonogram\w*|scal(?:anie|enie|eniu|aj|ony|ona|one)?|wyszukaj|pobierz|zastosuj|"
    r"zmian\w*|podglad\w*|nie mozna)\b",
    re.IGNORECASE,
)


class _FormControlAudit(HTMLParser):
    CONTROL_TAGS = {"input", "select", "textarea"}

    def __init__(self):
        super().__init__()
        self.ids = set()
        self.id_counts = {}
        self.labels = set()
        self.controls = []
        self.label_stack = []
        self.labelled_by_refs = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        element_id = attributes.get("id")
        if element_id:
            self.ids.add(element_id)
            self.id_counts[element_id] = self.id_counts.get(element_id, 0) + 1
        if tag == "label":
            self.label_stack.append(attributes.get("for"))
            if attributes.get("for"):
                self.labels.add(attributes["for"])
        elif tag in self.CONTROL_TAGS:
            self.controls.append({
                "id": element_id,
                "type": attributes.get("type", "").lower(),
                "aria_label": attributes.get("aria-label"),
                "aria_labelledby": attributes.get("aria-labelledby"),
                "title": attributes.get("title"),
                "wrapped_by_label": bool(self.label_stack),
            })
            if attributes.get("aria-labelledby"):
                self.labelled_by_refs.extend(attributes["aria-labelledby"].split())

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag == "label" and self.label_stack:
            self.label_stack.pop()


class _ButtonAudit(HTMLParser):
    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
                 "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.buttons = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        hidden = (any(frame["hidden"] for frame in self.stack)
                  or "hidden" in attributes
                  or attributes.get("aria-hidden", "").lower() == "true"
                  or tag == "template")
        button = None
        if tag == "button":
            button = {"attrs": attributes, "text": [], "hidden": hidden}
            self.buttons.append(button)
        self.stack.append({"tag": tag, "hidden": hidden, "button": button})
        if tag in self.VOID_TAGS:
            self.stack.pop()

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index]["tag"] == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        if not any(frame["hidden"] for frame in self.stack):
            for frame in self.stack:
                if frame["button"] is not None:
                    frame["button"]["text"].append(data)


class _AnchorAudit(HTMLParser):
    VOID_TAGS = _ButtonAudit.VOID_TAGS

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.links = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        aria_hidden = (any(frame["aria_hidden"] for frame in self.stack)
                       or attributes.get("aria-hidden", "").lower() == "true")
        link = None
        if tag == "a":
            link = {"attrs": attributes, "text": [], "aria_hidden": aria_hidden}
            self.links.append(link)
        elif tag == "img" and not aria_hidden:
            alt = attributes.get("alt", "").strip()
            if alt:
                for frame in self.stack:
                    if frame["link"] is not None:
                        frame["link"]["text"].append(alt)
        self.stack.append({"tag": tag, "aria_hidden": aria_hidden, "link": link})
        if tag in self.VOID_TAGS:
            self.stack.pop()

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index]["tag"] == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        if not any(frame["aria_hidden"] for frame in self.stack):
            for frame in self.stack:
                if frame["link"] is not None:
                    frame["link"]["text"].append(data)


class _DialogPlacementAudit(HTMLParser):
    VOID_TAGS = _ButtonAudit.VOID_TAGS

    def __init__(self):
        super().__init__()
        self.stack = []
        self.operation_dialog_ancestors = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "dialog" and attributes.get("id") == "operationDialog":
            self.operation_dialog_ancestors = list(self.stack)
        self.stack.append((tag, attributes))
        if tag in self.VOID_TAGS:
            self.stack.pop()

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                return


class _DialogAndTabAudit(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()
        self.dialog_names = []
        self.tabs = []
        self.panels = []
        self.labelled_by_refs = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        element_id = attributes.get("id")
        if element_id:
            self.ids.add(element_id)
        self.labelled_by_refs.extend(attributes.get("aria-labelledby", "").split())
        if tag == "dialog" or attributes.get("role") == "dialog":
            self.dialog_names.append((element_id or tag, attributes.get("aria-labelledby", "").split()))
        if attributes.get("role") == "tab":
            self.tabs.append((element_id, attributes.get("aria-controls")))
        elif attributes.get("role") == "tabpanel":
            self.panels.append((element_id, attributes.get("aria-labelledby")))


class TestAdminV2TemplateAccessibility(unittest.TestCase):
    def test_user_facing_sources_do_not_regress_to_polish_copy(self):
        """The Admin v2 shell and its dynamic UI strings must stay English."""
        sources = [TEMPLATE, *sorted(STATIC_DIR.rglob("*.js"))]
        findings = []
        for path in sources:
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if POLISH_DIACRITICS.search(line):
                    findings.append(f"{path.relative_to(TEMPLATE.parents[2])}:{line_number}")
        self.assertEqual(findings, [], "Polish-language text found in Admin v2 UI sources.")

    def test_user_facing_sources_do_not_regress_to_unaccented_polish_copy(self):
        """Catch common Polish UI wording even when it is typed without diacritics."""
        sources = [TEMPLATE, *sorted(STATIC_DIR.rglob("*.js"))]
        findings = []
        for path in sources:
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if COMMON_POLISH_UI_WORDS.search(line):
                    findings.append(f"{path.relative_to(TEMPLATE.parents[2])}:{line_number}")
        self.assertEqual(findings, [], "Common unaccented Polish UI wording found in Admin v2 sources.")

    def test_unaccented_polish_guard_detects_common_ui_phrases(self):
        self.assertIsNotNone(COMMON_POLISH_UI_WORDS.search("Zapisano plik. Wybierz zrodlo."))

    def test_every_static_form_control_has_an_accessible_name(self):
        audit = _FormControlAudit()
        audit.feed(TEMPLATE.read_text(encoding="utf-8"))

        unlabeled = [control for control in audit.controls
                     if control["type"] != "hidden"
                     and not control["wrapped_by_label"]
                     and not control["aria_label"]
                     and not control["aria_labelledby"]
                     and not control["title"]
                     and control["id"] not in audit.labels]
        self.assertTrue(audit.controls, "The template audit should inspect form controls.")
        self.assertEqual(unlabeled, [])

    def test_form_label_references_resolve_to_controls_and_labelledby_ids_exist(self):
        audit = _FormControlAudit()
        audit.feed(TEMPLATE.read_text(encoding="utf-8"))
        control_ids = {control["id"] for control in audit.controls if control["id"]}
        self.assertEqual(audit.labels - control_ids, set())
        self.assertEqual(set(audit.labelled_by_refs) - audit.ids, set())

    def test_template_ids_are_unique(self):
        audit = _FormControlAudit()
        audit.feed(TEMPLATE.read_text(encoding="utf-8"))
        duplicates = sorted(element_id for element_id, count in audit.id_counts.items() if count > 1)
        self.assertEqual(duplicates, [])

    def test_every_visible_static_button_has_an_accessible_name(self):
        audit = _ButtonAudit()
        audit.feed(TEMPLATE.read_text(encoding="utf-8"))
        unnamed = []
        for button in audit.buttons:
            attrs = button["attrs"]
            text = " ".join(" ".join(button["text"]).split())
            has_name = bool(text) or any(attrs.get(name, "").strip()
                                         for name in ("aria-label", "aria-labelledby", "title"))
            if not button["hidden"] and not has_name:
                unnamed.append(attrs.get("id") or attrs.get("class") or "<anonymous button>")
        self.assertTrue(audit.buttons, "The template audit should inspect buttons.")
        self.assertEqual(unnamed, [])

    def test_every_static_link_in_every_route_view_has_an_accessible_name(self):
        audit = _AnchorAudit()
        audit.feed(TEMPLATE.read_text(encoding="utf-8"))
        # Anchors in route views are included even while their section starts
        # hidden; the router can make those links visible after navigation.
        control_audit = _FormControlAudit()
        control_audit.feed(TEMPLATE.read_text(encoding="utf-8"))
        ids = control_audit.ids
        unnamed = []
        invalid_label_refs = []
        for link in audit.links:
            attrs = link["attrs"]
            if link["aria_hidden"]:
                continue
            text = " ".join(" ".join(link["text"]).split())
            named_by = attrs.get("aria-labelledby", "").split()
            has_name = bool(text or attrs.get("aria-label", "").strip()
                            or attrs.get("title", "").strip()
                            or named_by)
            if not has_name:
                unnamed.append(attrs.get("href") or attrs.get("class") or "<anonymous link>")
            invalid_label_refs.extend(ref for ref in named_by if ref not in ids)
        self.assertTrue(audit.links, "The template audit should inspect links.")
        self.assertEqual(unnamed, [])
        self.assertEqual(invalid_label_refs, [])

    def test_keyboard_skip_link_targets_focusable_main_content(self):
        source = TEMPLATE.read_text(encoding="utf-8")
        audit = _AnchorAudit()
        audit.feed(source)
        skip_links = [link for link in audit.links
                      if link["attrs"].get("href") == "#main-content"]
        self.assertEqual(len(skip_links), 1)
        self.assertEqual(" ".join(" ".join(skip_links[0]["text"]).split()),
                         "Skip to main content")
        self.assertRegex(source, r'<main\b[^>]*id="main-content"[^>]*tabindex="-1"')
        css = (STATIC_DIR / "admin-v2.css").read_text(encoding="utf-8")
        self.assertIn(".skip-link:focus { transform: translateY(0); }", css)

    def test_connection_state_announces_only_meaningful_status_changes(self):
        source = TEMPLATE.read_text(encoding="utf-8")
        self.assertRegex(
            source,
            r'<a class="connection-state" id="connectionState" href="#system\?tab=diagnostics"[^>]*aria-label="[^"]+"[^>]*aria-live="polite"[^>]*aria-atomic="true"',
        )
        script = (STATIC_DIR / "admin-v2.js").read_text(encoding="utf-8")
        self.assertIn("if (!element) return;", script)
        self.assertIn("label.textContent === message", script)

    def test_shared_operation_dialog_is_outside_hidden_route_views(self):
        audit = _DialogPlacementAudit()
        audit.feed(TEMPLATE.read_text(encoding="utf-8"))
        self.assertIsNotNone(audit.operation_dialog_ancestors)
        for tag, attrs in audit.operation_dialog_ancestors:
            self.assertNotIn("view", attrs.get("class", "").split(),
                             "Shared operation dialog must not be hidden with one route.")
            self.assertNotIn("hidden", attrs,
                             "Shared operation dialog must not be nested under hidden content.")

    def test_dialog_names_and_tab_panel_relationships_resolve(self):
        """Dialog labels may be rendered dynamically, but their targets must be created in JS."""
        audit = _DialogAndTabAudit()
        audit.feed(TEMPLATE.read_text(encoding="utf-8"))
        dynamic_ids = set()
        for path in STATIC_DIR.rglob("*.js"):
            source = path.read_text(encoding="utf-8")
            dynamic_ids.update(re.findall(r"\.id\s*=\s*['\"]([^'\"]+)['\"]", source))

        unresolved_dialog_names = [
            dialog for dialog, refs in audit.dialog_names
            if len(refs) != 1 or refs[0] not in audit.ids | dynamic_ids
        ]
        self.assertTrue(audit.dialog_names, "The template audit should inspect dialogs.")
        self.assertEqual(unresolved_dialog_names, [])
        self.assertEqual(
            set(audit.labelled_by_refs) - audit.ids - dynamic_ids,
            set(),
            "Every aria-labelledby reference must resolve to a static or generated ID.",
        )

        tab_to_panel = dict(audit.tabs)
        panel_to_tab = dict(audit.panels)
        self.assertTrue(audit.tabs, "The template audit should inspect tabs.")
        self.assertEqual(len(tab_to_panel), len(audit.tabs), "Every tab needs a unique ID.")
        self.assertEqual(len(panel_to_tab), len(audit.panels), "Every tab panel needs a unique ID.")
        panel_ids = {panel_id for panel_id, _ in audit.panels}
        tab_ids = {tab_id for tab_id, _ in audit.tabs}
        self.assertEqual(set(tab_to_panel.values()), panel_ids)
        self.assertEqual(set(panel_to_tab.values()), tab_ids)
        self.assertEqual(
            {tab_id: panel_id for panel_id, tab_id in audit.panels},
            tab_to_panel,
            "Tab and panel relationships must point back to each other.",
        )


if __name__ == "__main__":
    unittest.main()
