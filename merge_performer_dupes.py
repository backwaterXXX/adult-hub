#!/usr/bin/env python3
"""Compatibility entry point; implementation lives in ``sync.merge_performer_dupes``."""

from sync.merge_performer_dupes import *  # noqa: F401,F403
from sync.merge_performer_dupes import main

if __name__ == "__main__":
    raise SystemExit(main())
