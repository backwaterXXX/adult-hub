"""Compatibility entry point; implementation lives in common.fuzzy_performers."""

from common.fuzzy_performers import *  # noqa: F403
from common.fuzzy_performers import main

if __name__ == "__main__":
    raise SystemExit(main())
