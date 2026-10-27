"""Seed the duplicate reconciliation schedule disabled so hiding stays opt-in."""
from common.migrations.base import Migration

SCHEDULE_ID = "duplicate-reconcile"


def apply(conn, **kwargs):
    conn.execute(
        "INSERT INTO admin_schedules (id,name,action,payload,schedule_type,interval_minutes,"
        "timezone,enabled,revision,next_run_at) "
        "VALUES (%s,'Duplicate reconciliation','duplicate_reconcile','{}','interval',15,"
        "'Europe/Warsaw',FALSE,1,NULL) ON CONFLICT(id) DO NOTHING",
        (SCHEDULE_ID,),
    )
    # A database started by 0.4.0 already has this row enabled by the runtime
    # seed. Switching it off here is the point of the migration, not a revert
    # of an operator choice, so the revision moves and a stale Admin page has to
    # reload before it can claim the schedule back.
    conn.execute(
        "UPDATE admin_schedules SET enabled=FALSE,next_run_at=NULL,revision=revision+1,updated_at=now() "
        "WHERE id=%s AND enabled=TRUE",
        (SCHEDULE_ID,),
    )


migration = Migration(55, "0055_duplicate_reconcile_opt_in", "Make scheduled duplicate merges opt-in", apply)
current_migration = Migration(7, "0007_duplicate_reconcile_opt_in", "Make scheduled duplicate merges opt-in", apply)
