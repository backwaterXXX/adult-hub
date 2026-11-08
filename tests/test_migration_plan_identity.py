import unittest
from datetime import datetime, timezone

from common.migrations.base import MigrationRecord
from common.migrations.runner import CURRENT_MIGRATIONS, _migration_plan


def record(version, name):
    return MigrationRecord(version, name, datetime.now(timezone.utc))


class MigrationPlanIdentityTests(unittest.TestCase):
    def test_current_versions_after_nine_do_not_replay_historical_chain(self):
        applied = [record(1, '0001_baseline')] + [record(m.version, m.name) for m in CURRENT_MIGRATIONS]
        self.assertEqual(_migration_plan(applied), [])

    def test_historical_versions_ten_and_eleven_continue_historical_chain(self):
        applied = [record(1, '0001_baseline'), record(10, '0010_admin_operation_plans'),
                   record(11, '0011_video_metadata_overrides')]
        pending = _migration_plan(applied)
        self.assertEqual(pending[0].name, '0012_video_availability_checks')
        self.assertEqual(pending[-1].name, '0065_system_schedules_last_job_id')


if __name__ == '__main__':
    unittest.main()
