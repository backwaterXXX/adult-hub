#!/usr/bin/env python3
"""Compatibility entry point; implementation lives in ``sync.find_dupes``."""

from sync.find_dupes import *  # noqa: F401,F403
from sync.find_dupes import main

if __name__ == "__main__":
    raise SystemExit(main())
