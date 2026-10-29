"""Cooperative shutdown state for the sync child process.

A deploy stops the container, and Docker answers SIGTERM with SIGKILL ten
seconds later. The sync child had no handler, so it died in the middle of a
channel: the durable job stayed in `processing`, the pass wrote no history, and
the catalog kept whatever the last committed batch left behind.

The handler here only records the request. Orchestration checks it between
channels, so the channel in flight can finish, the collected batch can be
flushed and the pass can write a history entry that says it was interrupted.
"""
from __future__ import annotations

import signal
import threading

_STOP_REQUESTED = threading.Event()


def stop_event() -> threading.Event:
    return _STOP_REQUESTED


def stop_requested() -> bool:
    return _STOP_REQUESTED.is_set()


def install_signal_handlers(signum: int = signal.SIGTERM) -> None:
    """Turn a termination signal into a request the next checkpoint honours."""
    signal.signal(signum, lambda *_args: _STOP_REQUESTED.set())
