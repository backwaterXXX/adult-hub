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
from dataclasses import dataclass
from urllib.error import URLError
from urllib.request import urlopen
from common.logging_setup import configure_logging


UVICORN_COMMAND = [
    "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8090",
    "--timeout-graceful-shutdown", "5",
]
PHASH_WORKER_COMMAND = ["python3", "phash_worker.py"]
VISUAL_WORKER_COMMAND = ["python3", "video_visual_worker.py"]
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


@dataclass
class _Worker:
    """A helper process restarted with exponential backoff whenever it exits."""
    label: str
    command: list[str]
    process: subprocess.Popen | None = None
    restart_at: float = 0.0
    restart_delay: float = 2.0

    def start_if_due(self, now: float) -> None:
        if self.process is None and now >= self.restart_at:
            self.process = subprocess.Popen(self.command, start_new_session=True)
            _log(f"started {self.label} pid={self.process.pid}")

    def reap_if_exited(self) -> None:
        if self.process is not None and self.process.poll() is not None:
            code = self.process.returncode
            _log(f"{self.label} exited with code {code}; restarting in {self.restart_delay:g}s",
                 logging.WARNING)
            self.process = None
            self.restart_at = time.monotonic() + self.restart_delay
            self.restart_delay = min(self.restart_delay * 2.0, MAX_RESTART_DELAY)

    def reset_backoff_if_running(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.restart_delay = 2.0

    def stop(self) -> None:
        if self.process is not None:
            _stop_child(self.process)


def main() -> int:
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    restart_delay = 2.0
    workers = (
        _Worker("pHash worker", PHASH_WORKER_COMMAND),
        _Worker("visual duplicate worker", VISUAL_WORKER_COMMAND),
    )

    while not _stop_requested:
        for worker in workers:
            worker.start_if_due(time.monotonic())
        child = subprocess.Popen(UVICORN_COMMAND)
        started_at = time.monotonic()
        _log(f"started Uvicorn pid={child.pid}")

        reason, healthy_seen = _watch_child(child, workers, started_at)
        if healthy_seen:
            restart_delay = 2.0

        if _stop_requested:
            _stop_child(child)
            for worker in workers:
                worker.stop()
            return 0

        _log(f"restarting Uvicorn: {reason}; next attempt in {restart_delay:g}s", logging.WARNING)
        _sleep(restart_delay)
        restart_delay = min(restart_delay * 2.0, MAX_RESTART_DELAY)

    return 0


def _watch_child(child: subprocess.Popen, workers, started_at: float) -> tuple[str, bool]:
    """Watch Uvicorn and its helpers until Uvicorn must restart or a stop is requested.

    Returns (restart reason, whether any health check passed).
    """
    failures = 0
    healthy_seen = False
    while not _stop_requested:
        for worker in workers:
            worker.reap_if_exited()
        return_code = child.poll()
        if return_code is not None:
            return f"Uvicorn exited with code {return_code}", healthy_seen

        if time.monotonic() - started_at >= STARTUP_GRACE:
            if _healthy():
                failures = 0
                healthy_seen = True
                for worker in workers:
                    worker.reset_backoff_if_running()
            else:
                failures += 1
                _log(f"healthcheck failed ({failures}/{FAILURES_TO_RESTART})", logging.WARNING)
                if failures >= FAILURES_TO_RESTART:
                    _stop_child(child)
                    return ("process is alive but health endpoint stayed unresponsive "
                            "(event loop may be busy)"), healthy_seen

        _sleep(CHECK_INTERVAL)
    return "process exited", healthy_seen


if __name__ == "__main__":
    raise SystemExit(main())
