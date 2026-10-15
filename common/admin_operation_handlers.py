"""Per-operation handlers for consuming Admin v2 operation plans."""
from __future__ import annotations

def handle_orphan_file_operation(conn, db_url, plan_id, row, config, scope, revisions, now):
    """Compatibility facade for orphan file operation execution."""
    from common import admin_operation_audit_handlers as implementation
    return implementation.handle_orphan_file_operation(
        conn, db_url, plan_id, row, config, scope, revisions, now
    )




def handle_restore(conn, db_url, plan_id, row, config, scope, revisions, now):
    """Compatibility facade for restore operation execution."""
    from common import admin_operation_duplicate_handlers as implementation
    return implementation.handle_restore(conn, db_url, plan_id, row, config, scope, revisions, now)




def handle_hide(conn, db_url, plan_id, row, config, scope, revisions, now):
    """Compatibility facade for hide operation execution."""
    from common import admin_operation_duplicate_handlers as implementation
    return implementation.handle_hide(conn, db_url, plan_id, row, config, scope, revisions, now)




def handle_hide_batch(conn, db_url, plan_id, row, config, scope, revisions, now):
    """Compatibility facade for hide_batch operation execution."""
    from common import admin_operation_duplicate_handlers as implementation
    return implementation.handle_hide_batch(conn, db_url, plan_id, row, config, scope, revisions, now)




def handle_metadata_update_batch(conn, db_url, plan_id, row, config, scope, revisions, now):
    """Compatibility facade for batch metadata operation execution."""
    from common import admin_operation_metadata_handlers as implementation
    return implementation.handle_metadata_update_batch(
        conn, db_url, plan_id, row, config, scope, revisions, now
    )


def handle_metadata_update(conn, db_url, plan_id, row, config, scope, revisions, now):
    """Compatibility facade for single-item metadata operation execution."""
    from common import admin_operation_metadata_handlers as implementation
    return implementation.handle_metadata_update(
        conn, db_url, plan_id, row, config, scope, revisions, now
    )



def handle_performer_merge(conn, db_url, plan_id, row, config, scope, revisions, now):
    """Compatibility facade for performer_merge operation execution."""
    from common import admin_operation_duplicate_handlers as implementation
    return implementation.handle_performer_merge(conn, db_url, plan_id, row, config, scope, revisions, now)




def handle_audit_repair_nfo(conn, db_url, plan_id, row, config, scope, revisions, now):
    """Compatibility facade for audit NFO repair execution."""
    from common import admin_operation_audit_handlers as implementation
    return implementation.handle_audit_repair_nfo(
        conn, db_url, plan_id, row, config, scope, revisions, now
    )




def handle_audit_import_refresh(conn, db_url, plan_id, row, config, scope, revisions, now):
    """Compatibility facade for Jellyfin audit import refresh execution."""
    from common import admin_operation_audit_handlers as implementation
    return implementation.handle_audit_import_refresh(
        conn, db_url, plan_id, row, config, scope, revisions, now
    )




def handle_quarantine(conn, db_url, plan_id, row, config, scope, revisions, now):
    """Compatibility facade for quarantine operation execution."""
    from common import admin_operation_duplicate_handlers as implementation
    return implementation.handle_quarantine(conn, db_url, plan_id, row, config, scope, revisions, now)


