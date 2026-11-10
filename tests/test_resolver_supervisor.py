import subprocess
import unittest
from unittest.mock import Mock, patch

import resolver.supervisor as supervisor


class SupervisorTests(unittest.TestCase):
    def test_graceful_shutdown_fits_supervisor_budget(self):
        args = supervisor.UVICORN_COMMAND
        self.assertLess(int(args[args.index('--timeout-graceful-shutdown') + 1]), supervisor.STOP_TIMEOUT)

    def test_stuck_child_is_killed_after_grace_period(self):
        child = Mock()
        child.poll.return_value = None
        child.wait.side_effect = [subprocess.TimeoutExpired('uvicorn', supervisor.STOP_TIMEOUT), 0]
        supervisor._stop_child(child)
        child.terminate.assert_called_once()
        child.kill.assert_called_once()
        self.assertEqual(child.wait.call_args_list[0].kwargs['timeout'], supervisor.STOP_TIMEOUT)

    def test_exited_child_is_not_signalled(self):
        child = Mock()
        child.poll.return_value = 1
        supervisor._stop_child(child)
        child.terminate.assert_not_called()

    def test_unresponsive_live_process_respects_failure_budget(self):
        worker, visual, child = Mock(), Mock(), Mock()
        for process in (worker, visual, child):
            process.poll.return_value = None
            process.pid = 1
        events = []
        def sleep(_):
            if len(events) >= 3:
                supervisor._stop_requested = True
        def health():
            events.append('check')
            return False
        supervisor._stop_requested = False
        with patch.object(supervisor.signal, 'signal'), patch.object(supervisor.subprocess, 'Popen', side_effect=[worker, visual, child]), \
             patch.object(supervisor, '_healthy', side_effect=health), patch.object(supervisor, '_sleep', side_effect=sleep), \
             patch.object(supervisor, '_stop_child') as stop, patch.object(supervisor, 'STARTUP_GRACE', 0), \
             patch.object(supervisor, 'FAILURES_TO_RESTART', 3):
            self.assertEqual(supervisor.main(), 0)
        self.assertEqual(len(events), 3)
        self.assertIn(child, [call.args[0] for call in stop.call_args_list])
        supervisor._stop_requested = False


if __name__ == '__main__':
    unittest.main()
