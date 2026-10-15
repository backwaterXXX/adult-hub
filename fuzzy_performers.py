#!/usr/bin/env python3
"""Compatibility entry point; implementation lives in ``common.fuzzy_performers``."""

from common.fuzzy_performers import *  # noqa: F401,F403
from common.fuzzy_performers import main

if __name__ == "__main__":
    raise SystemExit(main())
