"""Scheduler behavior without a production backup or database."""
import fcntl
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import backup_scheduler


class BackupSchedulerTests(unittest.TestCase):
    def test_existing_backup_lock_is_reported_as_overlap(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / 'backup.lock'
            with patch.dict(os.environ, {'BACKUP_LOCK_PATH': str(lock)}):
                with lock.open('w') as held:
                    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.assertEqual(backup_scheduler._run_backup(), backup_scheduler.BACKUP_OVERLAP)

    def test_once_skips_when_schedule_is_not_due(self):
        with patch.dict(os.environ, {'DATABASE_URL': 'postgresql://fixture.invalid/test'}), \
             patch.object(sys, 'argv', ['backup_scheduler', '--once']), \
             patch.object(backup_scheduler, 'ensure_system_schedules'), \
             patch.object(backup_scheduler, 'claim_due_system_schedule', return_value=False), \
             patch.object(backup_scheduler, '_run_backup') as run:
            self.assertEqual(backup_scheduler.main(), 0)
        run.assert_not_called()

    def test_once_records_overlap_without_marking_success(self):
        with patch.dict(os.environ, {'DATABASE_URL': 'postgresql://fixture.invalid/test'}), \
             patch.object(sys, 'argv', ['backup_scheduler', '--once']), \
             patch.object(backup_scheduler, 'ensure_system_schedules'), \
             patch.object(backup_scheduler, 'claim_due_system_schedule', return_value=True), \
             patch.object(backup_scheduler, '_run_backup', return_value=backup_scheduler.BACKUP_OVERLAP), \
             patch.object(backup_scheduler, 'finish_system_schedule') as finish:
            self.assertEqual(backup_scheduler.main(), 0)
        self.assertEqual(finish.call_args.kwargs, {'success': False, 'skipped_overlap': True})

    def test_once_records_a_crash_as_a_failure(self):
        with patch.dict(os.environ, {'DATABASE_URL': 'postgresql://fixture.invalid/test'}), \
             patch.object(sys, 'argv', ['backup_scheduler', '--once']), \
             patch.object(backup_scheduler, 'ensure_system_schedules'), \
             patch.object(backup_scheduler, 'claim_due_system_schedule', return_value=True), \
             patch.object(backup_scheduler, '_run_backup', side_effect=RuntimeError('boom')), \
             patch.object(backup_scheduler, 'finish_system_schedule') as finish, \
             patch.object(backup_scheduler, 'LOGGER'):
            self.assertEqual(backup_scheduler.main(), 1)
        self.assertEqual(finish.call_args.kwargs, {'success': False, 'skipped_overlap': False})

    def test_daemon_finishes_the_claim_even_when_the_backup_crashes(self):
        class StopEvent:
            def __init__(self):
                self.stopped = False

            def is_set(self):
                return self.stopped

            def set(self):
                self.stopped = True

            def wait(self, _timeout):
                self.set()

        with patch.dict(os.environ, {'DATABASE_URL': 'postgresql://fixture.invalid/test'}), \
             patch.object(sys, 'argv', ['backup_scheduler']), \
             patch.object(backup_scheduler, 'STOP', StopEvent()), \
             patch.object(backup_scheduler, 'ensure_system_schedules'), \
             patch.object(backup_scheduler, 'claim_due_system_schedule', return_value=True), \
             patch.object(backup_scheduler, '_run_backup', side_effect=RuntimeError('boom')), \
             patch.object(backup_scheduler, 'finish_system_schedule') as finish, \
             patch.object(backup_scheduler, 'LOGGER'):
            self.assertEqual(backup_scheduler.main(), 0)
        self.assertEqual(finish.call_args.kwargs, {'success': False, 'skipped_overlap': False})


if __name__ == '__main__':
    unittest.main()
