"""Restart Uvicorn when the resolver stops answering its health endpoint.

Docker restart policies react to process exits, but not to an unhealthy
container. This small PID 1 supervisor closes that gap without adding another
container or mounting the Docker socket.
"""

import json
import logging
import os
import signal
import subprocess
import time
from urllib.error import URLError
from urllib.request import urlopen
from common.logging_setup import configure_logging


UVICORN_COMMAND = [
    "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8090",
]
PHASH_WORKER_COMMAND = ["python3", "phash_worker.py"]
HEALTH_URL = "http://127.0.0.1:8090/admin/health/live"
CHECK_INTERVAL = max(2.0, float(os.getenv("RESOLVER_WATCHDOG_INTERVAL_SECONDS", "10")))
CHECK_TIMEOUT = max(1.0, float(os.getenv("RESOLVER_WATCHDOG_TIMEOUT_SECONDS", "4")))
FAILURES_TO_RESTART = max(1, int(os.getenv("RESOLVER_WATCHDOG_FAILURES", "3")))
STARTUP_GRACE = max(0.0, float(os.getenv("RESOLVER_WATCHDOG_STARTUP_GRACE_SECONDS", "45")))
STOP_TIMEOUT = 8.0
MAX_RESTART_DELAY = 60.0

LOGGER = configure_logging("resolver.supervisor")

_stop_requested = False


def _log(message: str, level: int = logging.INFO) -> None:
    LOGGER.log(level, message)


def _request_stop(signum, _frame) -> None:
    global _stop_requested
    _stop_requested = True
    _log(f"received signal {signum}; stopping")


def _healthy() -> bool:
    try:
        with urlopen(HEALTH_URL, timeout=CHECK_TIMEOUT) as response:
            if response.status != 200:
                return False
            payload = json.loads(response.read(4096))
        return payload.get("status") == "ok"
    except (OSError, ValueError, URLError, TimeoutError):
        return False


def _stop_child(child: subprocess.Popen) -> None:
    if child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=STOP_TIMEOUT)
    except subprocess.TimeoutExpired:
        _log("Uvicorn did not stop gracefully; sending SIGKILL", logging.WARNING)
        child.kill()
        child.wait()


def _sleep(seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while not _stop_requested:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 0.5))


def main() -> int:
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    restart_delay = 2.0
    worker = None
    worker_restart_at = 0.0
    worker_restart_delay = 2.0

    while not _stop_requested:
        if worker is None and time.monotonic() >= worker_restart_at:
            worker = subprocess.Popen(PHASH_WORKER_COMMAND, start_new_session=True)
            _log(f"started pHash worker pid={worker.pid}")
        child = subprocess.Popen(UVICORN_COMMAND)
        started_at = time.monotonic()
        failures = 0
        reason = "process exited"
        _log(f"started Uvicorn pid={child.pid}")

        while not _stop_requested:
            if worker is not None and worker.poll() is not None:
                code = worker.returncode
                _log(f"pHash worker exited with code {code}; restarting in {worker_restart_delay:g}s",
                     logging.WARNING)
                worker = None
                worker_restart_at = time.monotonic() + worker_restart_delay
                worker_restart_delay = min(worker_restart_delay * 2.0, MAX_RESTART_DELAY)

            return_code = child.poll()
            if return_code is not None:
                reason = f"Uvicorn exited with code {return_code}"
                break

            if time.monotonic() - started_at >= STARTUP_GRACE:
                if _healthy():
                    failures = 0
                    restart_delay = 2.0
                    if worker is not None and worker.poll() is None:
                        worker_restart_delay = 2.0
                else:
                    failures += 1
                    _log(f"healthcheck failed ({failures}/{FAILURES_TO_RESTART})", logging.WARNING)
                    if failures >= FAILURES_TO_RESTART:
                        reason = "health endpoint stayed unavailable"
                        _stop_child(child)
                        break

            _sleep(CHECK_INTERVAL)

        if _stop_requested:
            _stop_child(child)
            if worker is not None:
                _stop_child(worker)
            return 0

        _log(f"restarting Uvicorn: {reason}; next attempt in {restart_delay:g}s", logging.WARNING)
        _sleep(restart_delay)
        restart_delay = min(restart_delay * 2.0, MAX_RESTART_DELAY)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
