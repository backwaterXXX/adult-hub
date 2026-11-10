"""Backup freshness and availability issues for Admin v2."""

from __future__ import annotations

from resolver.admin_v2_issue_builder import _issue, _timestamp


def collect_backup_issues(admin, now, health, issues):
    try:
        backup_status = admin._backup_status()
        inventory_available = backup_status.get("inventory_available") is True
        health["available"]["backup_inventory"] = inventory_available
        if not inventory_available:
            issues.append(_issue(
                "backup", "high", "Backup inventory unavailable",
                "The configured backup directory could not be read. Check the directory and permissions.",
                "backup", None, now, now, "Review backups", "/admin/v2/#system?tab=backups",
            ))
        else:
            latest = backup_status.get("latest")
            latest_at = _timestamp(latest.get("modified_at")) if isinstance(latest, dict) else None
            health["freshness"]["backup_at"] = latest_at
            if latest is None:
                issues.append(_issue(
                    "backup", "high", "No database backup is available",
                    "No database backup file was found in the configured backup directory. Create a backup and verify it.",
                    "backup", None, now, now, "Review backups", "/admin/v2/#system?tab=backups",
                ))
            elif latest_at is None:
                health["available"]["backup_inventory"] = False
                issues.append(_issue(
                    "backup", "high", "Backup time is unavailable",
                    "The latest database backup has no usable modification time. Review the backup inventory.",
                    "backup", None, now, now, "Review backups", "/admin/v2/#system?tab=backups",
                ))
            elif now - latest_at >= health["thresholds"]["backup_warning_seconds"]:
                threshold = health["thresholds"]["backup_warning_seconds"]
                age_hours = max(0, now - latest_at) // 3600
                issues.append(_issue(
                    "backup", "high", "Database backup is overdue",
                    f"The latest database backup is {age_hours} hours old; the configured warning limit is {threshold // 3600} hours. Create and verify a fresh backup.",
                    "backup", None, latest_at + threshold, now, "Review backups", "/admin/v2/#system?tab=backups",
                ))
    except Exception:
        health["available"]["backup_inventory"] = False
        issues.append(_issue(
            "backup", "high", "Backup inventory unavailable",
            "The backup inventory could not be loaded. Check the configured backup directory and try again.",
            "backup", None, now, now, "Review backups", "/admin/v2/#system?tab=backups",
        ))
