"""Compatibility entry point; implementation lives in common.fuzzy_performers."""
import sys
from pathlib import Path

_here = Path(__file__).resolve().parent
for _candidate in (_here / "sync", _here.parent / "sync"):
    if _candidate.is_dir():
        sys.path.insert(0, str(_candidate))
        break

from common.fuzzy_performers import *  # noqa: F401,F403,E402
from common.fuzzy_performers import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
