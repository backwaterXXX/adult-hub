"""Scheduler behavior without a production backup or database."""
import fcntl
import sys
from unittest.mock import patch

import backup_scheduler


def test_existing_backup_lock_is_reported_as_overlap(tmp_path, monkeypatch):
    lock = tmp_path / 'backup.lock'
    monkeypatch.setenv('BACKUP_LOCK_PATH', str(lock))
    with lock.open('w') as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert backup_scheduler._run_backup() == backup_scheduler.BACKUP_OVERLAP


def test_once_skips_when_schedule_is_not_due(monkeypatch):
    monkeypatch.setenv('DATABASE_URL', 'postgresql://fixture.invalid/test')
    with patch.object(sys, 'argv', ['backup_scheduler', '--once']), \
         patch.object(backup_scheduler, 'ensure_system_schedules'), \
         patch.object(backup_scheduler, 'claim_due_system_schedule', return_value=False), \
         patch.object(backup_scheduler, '_run_backup') as run:
        assert backup_scheduler.main() == 0
        run.assert_not_called()


def test_once_records_overlap_without_marking_success(monkeypatch):
    monkeypatch.setenv('DATABASE_URL', 'postgresql://fixture.invalid/test')
    with patch.object(sys, 'argv', ['backup_scheduler', '--once']), \
         patch.object(backup_scheduler, 'ensure_system_schedules'), \
         patch.object(backup_scheduler, 'claim_due_system_schedule', return_value=True), \
         patch.object(backup_scheduler, '_run_backup', return_value=backup_scheduler.BACKUP_OVERLAP), \
         patch.object(backup_scheduler, 'finish_system_schedule') as finish:
        assert backup_scheduler.main() == 0
        assert finish.call_args.kwargs == {'success': False, 'skipped_overlap': True}
