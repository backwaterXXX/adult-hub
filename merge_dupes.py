#!/usr/bin/env python3
"""Compatibility entry point; implementation lives in ``sync.merge_dupes``."""

from sync.merge_dupes import *  # noqa: F403
from sync.merge_dupes import main

if __name__ == "__main__":
    raise SystemExit(main())
