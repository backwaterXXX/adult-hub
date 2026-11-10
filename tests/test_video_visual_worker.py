"""Opt-in idle behaviour of the visual duplicate worker."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import resolver.video_visual_worker as video_visual_worker


class VisualWorkerFlagTests(unittest.TestCase):
    def setUp(self):
        video_visual_worker.STOP = False
        self.addCleanup(setattr, video_visual_worker, "STOP", False)

    def run_first_cycle(self, enabled: str | None, idle, jobs=()):
        environ = {} if enabled is None else {"VIDEO_VISUAL_WORKER_ENABLED": enabled}
        with patch.dict(os.environ, environ, clear=False), \
                patch.object(video_visual_worker, "recover_expired") as recover, \
                patch.object(video_visual_worker, "claim_next",
                             side_effect=list(jobs) or None) as claim, \
                patch.object(video_visual_worker, "_signature") as signature, \
                patch.object(video_visual_worker, "heartbeat", return_value=True), \
                patch.object(video_visual_worker, "_resolver_ready", return_value=True), \
                patch.object(video_visual_worker.time, "sleep", side_effect=idle):
            if enabled is None:
                os.environ.pop("VIDEO_VISUAL_WORKER_ENABLED", None)
            # One sleep ends the run, so the loop is bounded either way.
            video_visual_worker.main()
        return recover, claim, signature

    def stop_after_one_idle(self, _seconds: float) -> None:
        video_visual_worker.STOP = True

    def test_worker_leaves_the_queue_alone_until_enabled(self):
        recover, claim, _signature = self.run_first_cycle(None, self.stop_after_one_idle)
        recover.assert_not_called()
        claim.assert_not_called()

    def test_enabled_worker_leases_and_runs_a_job(self):
        recover, claim, signature = self.run_first_cycle(
            "true", self.stop_after_one_idle, jobs=[{"id": "job-1", "kind": "signature"}, None])
        self.assertTrue(recover.called)
        claim.assert_called()
        signature.assert_called_once()


if __name__ == "__main__":
    unittest.main()
