import json
import tempfile
import unittest
from pathlib import Path

from common.worker_capabilities import publish_worker_capabilities, read_worker_capabilities


class TestWorkerCapabilities(unittest.TestCase):
    def test_snapshot_is_atomic_secret_free_and_reports_supported_actions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worker.json"
            publish_worker_capabilities(path, version="release-7", actions=["sync", "availability_check"], now=100)

            result = read_worker_capabilities(path, now=110)
            raw = json.loads(path.read_text())

        self.assertEqual(result, {"available": True, "worker_version": "release-7", "updated_at": 100.0,
                                  "age_seconds": 10, "actions": ["availability_check", "sync"]})
        self.assertEqual(set(raw), {"schema_version", "worker_version", "updated_at", "running", "actions"})

    def test_stale_stopped_malformed_and_future_snapshots_are_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worker.json"
            publish_worker_capabilities(path, version="release-7", actions=["sync"], now=100)
            stale = read_worker_capabilities(path, now=116)
            future = read_worker_capabilities(path, now=90)
            publish_worker_capabilities(path, version="release-7", actions=["sync"], running=False, now=100)
            stopped = read_worker_capabilities(path, now=100)
            path.write_text("not json")
            malformed = read_worker_capabilities(path, now=100)
            missing = read_worker_capabilities(Path(directory) / "missing.json", now=100)

        for value in (stale, future, stopped, malformed, missing):
            self.assertFalse(value["available"])
            self.assertEqual(value["actions"], [])


if __name__ == "__main__":
    unittest.main()
