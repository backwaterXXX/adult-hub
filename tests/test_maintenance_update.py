"""Tests for Adult Hub Stage 5 safe maintenance and update workflow (scripts/update.sh / make update).

Verifies:
1. Refusal on missing .env (requires setup first).
2. Refusal on production .env (protects production deployment; directs to make deploy).
3. Refusal when active production compose project is running.
4. Prerequisite checks (Docker CLI, Compose v2, daemon availability).
5. Respect for VERSION and export of APP_VERSION.
6. Graceful shutdown of sync service with 240s stop grace period before replacement.
7. Database schema migration execution via migration service container.
8. Migration failure aborts update without replacing or disrupting running application services.
9. Telegram authorization inspection (withholding sync/backup when unauthorized).
10. Preservation of .env, config.yaml, media, backups, data, and named volumes (no alteration, no orphans, no anon-volume renewal).
11. No git pull execution.
"""

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UPDATE_SCRIPT = ROOT / "scripts" / "update.sh"
SETUP_SCRIPT = ROOT / "scripts" / "setup.sh"


def make_mock_docker(
    directory: Path,
    *,
    filename="mock_docker.sh",
    daemon_ok=True,
    compose_v2_ok=True,
    volume_exists=False,
    active_prod_project=False,
    sync_running=False,
    migration_ok=True,
    build_ok=True,
    auth_check_ok=True,
    resolver_healthy=True,
):
    """Create a mock docker executable script for isolated update workflow testing."""
    mock_bin = directory / filename
    prod_resp = "echo 'prod_container_id'" if active_prod_project else "exit 0"
    vol_resp = "exit 0" if volume_exists else "exit 1"
    version_resp = "echo 'Docker Compose version v2.29.2'" if compose_v2_ok else "exit 1"
    sync_resp = "echo 'sync_running_id'" if sync_running else "exit 0"
    resolver_json = '{"Health": "healthy", "State": "running"}' if resolver_healthy else '{"Health": "starting", "State": "running"}'
    build_resp = "exit 0" if build_ok else "echo 'Build failed' >&2; exit 1"
    migration_resp = "exit 0" if migration_ok else "echo 'Migration failed' >&2; exit 1"
    auth_resp = "exit 0" if auth_check_ok else "exit 1"
    daemon_resp = "exit 0" if daemon_ok else "echo 'Cannot connect to daemon' >&2; exit 1"

    content = f"""#!/usr/bin/env bash
cmd="$1"
shift || true

# Log command calls for assertions
echo "docker $cmd $@" >> "{directory / 'docker_calls.log'}"

case "$cmd" in
    ps)
        if [ "$1" = "--filter" ] && [[ "$2" == *"project=adult-hub"* ]]; then
            {prod_resp}
            exit 0
        fi
        exit 0
        ;;
    volume)
        sub="$1"
        shift || true
        if [ "$sub" = "inspect" ] && [ "$1" = "adult-hub-standalone_postgres_data" ]; then
            {vol_resp}
        fi
        exit 0
        ;;
    compose)
        all_args="$*"
        if [[ "$all_args" == *"version"* ]]; then
            {version_resp}
            exit 0
        fi
        if [[ "$all_args" == *"config --quiet"* ]]; then
            if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
                docker compose "$@"
                exit $?
            fi
            exit 0
        fi
        if [[ "$all_args" =~ (^|[[:space:]])ps([[:space:]]|$) ]]; then
            if [[ "$all_args" =~ "sync" ]]; then
                {sync_resp}
                exit 0
            fi
            if [[ "$all_args" =~ "resolver" ]]; then
                echo '{resolver_json}'
                exit 0
            fi
            echo '{{"Health": "healthy", "State": "running"}}'
            exit 0
        fi
        if [[ "$all_args" =~ (^|[[:space:]])stop([[:space:]]|$) ]]; then
            exit 0
        fi
        if [[ "$all_args" =~ (^|[[:space:]])build([[:space:]]|$) ]]; then
            {build_resp}
        fi
        if [[ "$all_args" =~ (^|[[:space:]])up([[:space:]]|$) ]]; then
            if [[ "$all_args" =~ "migration" ]]; then
                {migration_resp}
            fi
            exit 0
        fi
        if [[ "$all_args" =~ (^|[[:space:]])run([[:space:]]|$) ]]; then
            if [[ "$all_args" =~ "telegram_auth check" ]]; then
                {auth_resp}
            fi
            exit 0
        fi
        exit 0
        ;;
    info)
        {daemon_resp}
        ;;
    *)
        exit 0
        ;;
esac
"""
    mock_bin.write_text(content)
    mock_bin.chmod(0o755)
    return str(mock_bin)


class TestMaintenanceUpdateWorkflow(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="adulthub_update_")
        self.isolated_root = Path(self.temp_dir.name)
        shutil.copy(ROOT / "VERSION", self.isolated_root / "VERSION")
        shutil.copy(ROOT / "compose.standalone.yml", self.isolated_root / "compose.standalone.yml")
        shutil.copytree(ROOT / "common", self.isolated_root / "common")

    def tearDown(self):
        self.temp_dir.cleanup()

    def run_update(self, args=None, *, env=None):
        cmd = [str(UPDATE_SCRIPT)] + (args or [])
        run_env = os.environ.copy()
        if env:
            run_env.update(env)
        return subprocess.run(
            cmd,
            cwd=str(self.isolated_root),
            env=run_env,
            capture_output=True,
            text=True,
            timeout=15,
        )

    def init_standalone_env(self, mock_docker):
        """Helper to initialize an isolated standalone environment via setup.sh."""
        res = subprocess.run(
            [
                str(SETUP_SCRIPT),
                "--non-interactive",
                "--skip-docker-start",
                "--docker-bin", mock_docker,
                "--root-dir", str(self.isolated_root),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(res.returncode, 0, f"setup.sh failed during init:\n{res.stderr}")

    def test_update_refuses_when_env_missing(self):
        """Update aborts if .env is missing and directs user to run setup."""
        mock_docker = make_mock_docker(self.isolated_root)
        res = self.run_update(["--docker-bin", mock_docker, "--root-dir", str(self.isolated_root)])
        self.assertEqual(res.returncode, 1)
        self.assertIn("No .env file found", res.stderr)
        self.assertIn("make setup", res.stderr)

    def test_update_refuses_when_production_env_detected(self):
        """Update refuses execution if .env exists without standalone marker (protects production)."""
        prod_env = self.isolated_root / ".env"
        prod_env.write_text("DATABASE_URL=postgresql://prod:secret@db:5432/adult\n")

        mock_docker = make_mock_docker(self.isolated_root)
        res = self.run_update(["--docker-bin", mock_docker, "--root-dir", str(self.isolated_root)])
        self.assertEqual(res.returncode, 1)
        self.assertIn("Production deployment detected! 'make update' is standalone-only.", res.stderr)
        self.assertIn("make deploy", res.stderr)
        # Production .env must remain untouched
        self.assertEqual(prod_env.read_text(), "DATABASE_URL=postgresql://prod:secret@db:5432/adult\n")

    def test_update_refuses_when_active_production_compose_running(self):
        """Update refuses execution if production compose project 'adult-hub' is active."""
        init_docker = make_mock_docker(self.isolated_root, filename="init_docker.sh")
        self.init_standalone_env(init_docker)

        prod_docker = make_mock_docker(self.isolated_root, filename="prod_docker.sh", active_prod_project=True)
        res = self.run_update(["--docker-bin", prod_docker, "--root-dir", str(self.isolated_root)])
        self.assertEqual(res.returncode, 1)
        self.assertIn("Active production Compose project 'adult-hub'", res.stderr)

    def test_update_missing_docker_cli(self):
        """Update fails with clear error when docker CLI is not found."""
        mock_docker = make_mock_docker(self.isolated_root)
        self.init_standalone_env(mock_docker)

        res = self.run_update(["--docker-bin", "/nonexistent/docker", "--root-dir", str(self.isolated_root)])
        self.assertEqual(res.returncode, 1)
        self.assertIn("Docker CLI '/nonexistent/docker' is not installed", res.stderr)

    def test_update_missing_compose_v2(self):
        """Update fails with clear error when Docker Compose v2 is missing."""
        init_docker = make_mock_docker(self.isolated_root, filename="init_docker.sh")
        self.init_standalone_env(init_docker)

        no_compose_docker = make_mock_docker(self.isolated_root, filename="no_compose_docker.sh", compose_v2_ok=False)
        res = self.run_update(["--docker-bin", no_compose_docker, "--root-dir", str(self.isolated_root)])
        self.assertEqual(res.returncode, 1)
        self.assertIn("Docker Compose v2 is required", res.stderr)

    def test_update_daemon_unavailable(self):
        """Update fails cleanly when Docker daemon is not accessible."""
        init_docker = make_mock_docker(self.isolated_root, filename="init_docker.sh", daemon_ok=True)
        self.init_standalone_env(init_docker)

        no_daemon_docker = make_mock_docker(self.isolated_root, filename="no_daemon_docker.sh", daemon_ok=False)
        res = self.run_update(["--docker-bin", no_daemon_docker, "--root-dir", str(self.isolated_root)])
        self.assertEqual(res.returncode, 1)
        self.assertIn("Cannot connect to the Docker daemon", res.stderr)

    def test_update_respects_version_file_and_rebuilds_services(self):
        """Update reads APP_VERSION from VERSION and builds resolver and sync."""
        (self.isolated_root / "VERSION").write_text("0.5.0\n")
        mock_docker = make_mock_docker(self.isolated_root)
        self.init_standalone_env(mock_docker)

        # Clear calls log before update
        (self.isolated_root / "docker_calls.log").write_text("")

        res = self.run_update(["--docker-bin", mock_docker, "--root-dir", str(self.isolated_root)])
        self.assertEqual(res.returncode, 0, f"Update failed:\n{res.stderr}")
        self.assertIn("Updating Adult Hub Standalone to version 0.5.0", res.stdout)
        self.assertIn("Version:            0.5.0", res.stdout)

        calls = (self.isolated_root / "docker_calls.log").read_text()
        self.assertIn("build resolver sync", calls)

    def test_update_graceful_sync_shutdown_when_sync_running(self):
        """When sync worker is running, update stops it with -t 240 allowing current channel to drain."""
        mock_docker = make_mock_docker(self.isolated_root, sync_running=True)
        self.init_standalone_env(mock_docker)

        (self.isolated_root / "docker_calls.log").write_text("")

        res = self.run_update(["--docker-bin", mock_docker, "--root-dir", str(self.isolated_root)])
        self.assertEqual(res.returncode, 0, f"Update failed:\n{res.stderr}")
        self.assertIn("Gracefully stopping sync worker (waiting up to 4m for current channel to finish)...", res.stdout)
        self.assertIn("Sync worker stopped cleanly", res.stdout)

        calls = (self.isolated_root / "docker_calls.log").read_text()
        self.assertIn("stop -t 240 sync", calls)

    def test_update_runs_database_migrations_and_readiness(self):
        """Update runs schema migration and verifies resolver readiness."""
        mock_docker = make_mock_docker(self.isolated_root, auth_check_ok=True, resolver_healthy=True)
        self.init_standalone_env(mock_docker)

        (self.isolated_root / "docker_calls.log").write_text("")

        res = self.run_update(["--docker-bin", mock_docker, "--root-dir", str(self.isolated_root)])
        self.assertEqual(res.returncode, 0, f"Update failed:\n{res.stderr}")
        self.assertIn("Running database schema migrations...", res.stdout)
        self.assertIn("Database migration completed successfully", res.stdout)
        self.assertIn("Verifying resolver readiness...", res.stdout)
        self.assertIn("Resolver readiness verified", res.stdout)

        calls = (self.isolated_root / "docker_calls.log").read_text()
        self.assertIn("up migration", calls)
        self.assertIn("up -d --no-build resolver sync backup", calls)

    def test_update_migration_failure_aborts_without_replacing_services(self):
        """If database migration fails, update aborts and does NOT replace running services."""
        mock_docker = make_mock_docker(self.isolated_root, migration_ok=False)
        self.init_standalone_env(mock_docker)

        (self.isolated_root / "docker_calls.log").write_text("")

        res = self.run_update(["--docker-bin", mock_docker, "--root-dir", str(self.isolated_root)])
        self.assertEqual(res.returncode, 1)
        self.assertIn("Database schema migration failed during update", res.stderr)
        self.assertIn("Application services have NOT been replaced", res.stderr)

        calls = (self.isolated_root / "docker_calls.log").read_text()
        self.assertIn("up migration", calls)
        # Resolver, sync, backup must NOT have been started after migration failed
        self.assertNotIn("up -d --no-build resolver sync backup", calls)

    def test_update_withholds_sync_and_backup_when_telegram_not_authorized(self):
        """When Telegram is not authorized, update starts resolver but withholds sync and backup."""
        mock_docker = make_mock_docker(self.isolated_root, auth_check_ok=False)
        self.init_standalone_env(mock_docker)

        (self.isolated_root / "docker_calls.log").write_text("")

        res = self.run_update(["--docker-bin", mock_docker, "--root-dir", str(self.isolated_root)])
        self.assertEqual(res.returncode, 0, f"Update failed:\n{res.stderr}")
        self.assertIn("Telegram session not authorized. Starting resolver (sync and backup remain withheld)", res.stdout)

        calls = (self.isolated_root / "docker_calls.log").read_text()
        self.assertIn("up -d --no-build resolver", calls)
        self.assertNotIn("up -d --no-build resolver sync backup", calls)

    def test_update_preserves_env_config_media_backups_and_volumes(self):
        """Update must never alter configuration, secrets, media, backups, or named volumes."""
        mock_docker = make_mock_docker(self.isolated_root)
        self.init_standalone_env(mock_docker)

        # Pre-populate custom settings and files
        env_file = self.isolated_root / ".env"
        env_orig = env_file.read_text()

        config_file = self.isolated_root / "standalone" / "config.yaml"
        custom_config = config_file.read_text().replace("channels: []", "channels:\n      - id: '@mychannel'")
        config_file.write_text(custom_config)

        media_file = self.isolated_root / "standalone" / "media" / "important_video.strm"
        media_file.write_text("http://127.0.0.1:8090/stream/999\n")

        backup_file = self.isolated_root / "standalone" / "data" / "backups" / "backup_20261106.sql.gz"
        backup_file.write_bytes(b"existing_db_backup_bytes")

        (self.isolated_root / "docker_calls.log").write_text("")

        res = self.run_update(["--docker-bin", mock_docker, "--root-dir", str(self.isolated_root)])
        self.assertEqual(res.returncode, 0, f"Update failed:\n{res.stderr}")

        # Assert no configuration or secrets altered
        self.assertEqual(env_file.read_text(), env_orig)
        self.assertEqual(stat.S_IMODE(env_file.stat().st_mode), 0o600)
        self.assertEqual(config_file.read_text(), custom_config)

        # Assert media and backup files are unchanged
        self.assertEqual(media_file.read_text(), "http://127.0.0.1:8090/stream/999\n")
        self.assertEqual(backup_file.read_bytes(), b"existing_db_backup_bytes")

        # Assert safe compose invocation (no destructive flags)
        calls = (self.isolated_root / "docker_calls.log").read_text()
        self.assertNotIn("--renew-anon-volumes", calls)
        self.assertNotIn("--remove-orphans", calls)
        self.assertNotIn(" down", calls)
        self.assertNotIn(" compose rm", calls)
        self.assertNotIn("volume rm", calls)
        self.assertNotIn("git", calls)


if __name__ == "__main__":
    unittest.main()
