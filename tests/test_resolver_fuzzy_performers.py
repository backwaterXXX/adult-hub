"""Tests for resolver.fuzzy_performers compatibility facade."""

from __future__ import annotations

import runpy
import unittest
from unittest.mock import patch

import common.fuzzy_performers
import resolver.fuzzy_performers


class ResolverFuzzyPerformersTest(unittest.TestCase):
    def test_resolver_fuzzy_performers_exports(self):
        self.assertIs(resolver.fuzzy_performers.main, common.fuzzy_performers.main)
        self.assertTrue(hasattr(resolver.fuzzy_performers, "find_fuzzy_performer_candidates"))

    def test_resolver_fuzzy_performers_main_execution(self):
        with patch("common.fuzzy_performers.main", return_value=42), \
             self.assertRaises(SystemExit) as cm:
            runpy.run_module("resolver.fuzzy_performers", run_name="__main__")
        self.assertEqual(cm.exception.code, 42)


if __name__ == "__main__":
    unittest.main()
