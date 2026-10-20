#!/usr/bin/env python3
"""Wait for the Jellyfin library scan to finish, then import changed sidecars.

The library scan re-reads NFO sidecars while it runs and can revert an
import that happened earlier, so the Telegram NFO import has to wait until
the scan leaves the Running state. The scan task is matched by its stable
key rather than its localized name.
"""

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

for _candidate in (Path("/app"), Path.cwd()):
    if _candidate.is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))


def _log(message: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}", flush=True)


def _scan_state(url: str, api_key: str):
    """Return the state of the media library scan task, or None when absent."""
    import requests
    headers = {
        "X-Emby-Token": api_key,
        "X-Emby-Authorization": (
            'MediaBrowser Client="adult-hub", Device="sync", DeviceId="adult-hub-sync", '
            f'Version="1.0.0", Token="{api_key}"'
        ),
        "Authorization": (
            'MediaBrowser Client="adult-hub", Device="sync", DeviceId="adult-hub-sync", '
            f'Version="1.0.0", Token="{api_key}"'
        ),
    }
    response = requests.get(f"{url.rstrip('/')}/ScheduledTasks", headers=headers, timeout=30)
    response.raise_for_status()
    for task in response.json():
        if task.get("Key") == "RefreshLibrary":
            return task.get("State"), task.get("CurrentProgressPercentage")
    return None


def wait_for_scan(url: str, api_key: str, poll_seconds: int, timeout_seconds: int) -> bool:
    deadline = time.monotonic() + timeout_seconds
    reported = None
    while time.monotonic() < deadline:
        try:
            state = _scan_state(url, api_key)
        except Exception as exc:
            _log(f"Nie mogę odczytać stanu skanu: {type(exc).__name__}")
            time.sleep(poll_seconds)
            continue
        if state is None:
            _log("Nie znaleziono zadania skanowania biblioteki")
            return False
        current, progress = state
        if current != reported:
            _log(f"skan biblioteki: stan={current}, postep={progress}%")
            reported = current
        if current != "Running":
            _log("skan biblioteki zakonczony")
            return True
        time.sleep(poll_seconds)
    _log("Przekroczono czas oczekiwania na koniec skanu")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True,
                        help="report file written by backfill_catalog_titles.py")
    parser.add_argument("--command", required=True,
                        help="command to run once the scan has finished")
    parser.add_argument("--poll-seconds", type=int, default=60)
    parser.add_argument("--timeout-seconds", type=int, default=6 * 3600)
    args = parser.parse_args()

    url = (os.getenv("JELLYFIN_URL") or "").strip()
    api_key = (os.getenv("JELLYFIN_API_KEY") or "").strip()
    if not url or not api_key:
        _log("Brak konfiguracji Jellyfin")
        return 1
    if not Path(args.report).is_file():
        _log(f"Raport {args.report} jeszcze nie istnieje; czekam")
        while not Path(args.report).is_file():
            time.sleep(args.poll_seconds)
    _log(f"Raport gotowy: {args.report}")
    if not wait_for_scan(url, api_key, args.poll_seconds, args.timeout_seconds):
        return 1
    time.sleep(30)
    _log(f"Uruchamiam: {args.command}")
    completed = subprocess.run(args.command, shell=True, text=True)
    _log(f"Kod wyjscia polecenia: {completed.returncode}")
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
