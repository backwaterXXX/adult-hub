"""Post-baseline migration for durable Admin v2 operation previews."""

from __future__ import annotations

from typing import Any

from common.migrations.base import Migration


def apply(conn: Any, **kwargs) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS admin_operation_plans (
            plan_id TEXT PRIMARY KEY,
            operation_type TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('previewed', 'queued', 'expired')),
            scope JSONB NOT NULL,
            revisions JSONB NOT NULL,
            preview JSONB NOT NULL,
            result JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMPTZ NOT NULL,
            consumed_at TIMESTAMPTZ
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_admin_operation_plans_expiry ON admin_operation_plans (expires_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_admin_operation_plans_state ON admin_operation_plans (state, created_at DESC)")


migration = Migration(
    version=10,
    name="0010_admin_operation_plans",
    description="Persist bounded, revision-checked Admin v2 operation plans",
    apply_fn=apply,
)
