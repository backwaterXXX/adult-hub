"""Tests for Adult Hub Stage 2 standalone setup installer (scripts/setup.sh).

Verifies safety, idempotency, isolated root execution, prerequisite checks,
network binding, port collision detection, and secret redaction.
"""

import os
import shutil
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.helpers.docker_stub import make_docker_stub

ROOT = Path(__file__).resolve().parents[1]
SETUP_SCRIPT = ROOT / "scripts" / "setup.sh"


def make_mock_docker(
    directory: Path,
    *,
    daemon_ok=True,
    compose_v2_ok=True,
    volume_exists=False,
    active_prod_project=False,
    auth_check_ok=False,
    auth_run_ok=True,
    active_session_services=False,
):
    """Create a mock docker executable script for isolated installer testing."""
    mock_bin = directory / "mock_docker.sh"
    content = f"""#!/usr/bin/env bash
cmd="$1"
shift || true

# Log command calls for assertions
echo "docker $cmd $@" >> "{directory / 'docker_calls.log'}"

case "$cmd" in
    ps)
        if [ "$1" = "--filter" ] && [[ "$2" == *"project=adult-hub"* ]]; then
            {"echo 'prod_container_id'" if active_prod_project else "exit 0"}
            exit 0
        fi
        exit 0
        ;;
    volume)
        sub="$1"
        shift || true
        if [ "$sub" = "inspect" ] && [ "$1" = "adult-hub-standalone_postgres_data" ]; then
            {"exit 0" if volume_exists else "exit 1"}
        fi
        exit 0
        ;;
    compose)
        all_args="$*"
        if [[ "$all_args" == *"version"* ]]; then
            {"echo 'Docker Compose version v2.29.2'" if compose_v2_ok else "exit 1"}
            exit 0
        fi
        # Delegate 'config' to real docker compose if available, else exit 0
        if [[ "$all_args" == *"config --quiet"* ]]; then
            if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
                docker compose "$@"
                exit $?
            fi
            exit 0
        fi
        if [[ "$all_args" =~ (^|[[:space:]])ps([[:space:]]|$) ]]; then
            if [[ "$all_args" =~ "resolver sync" ]]; then
                {"echo 'active_container_id'" if active_session_services else "exit 0"}
                exit 0
            fi
            echo '{{"Health": "healthy", "State": "running"}}'
            exit 0
        fi
        if [[ "$all_args" =~ (^|[[:space:]])up([[:space:]]|$) ]]; then
            exit 0
        fi
        if [[ "$all_args" =~ (^|[[:space:]])run([[:space:]]|$) ]]; then
            if [[ "$all_args" =~ "telegram_auth check" ]]; then
                {"exit 0" if auth_check_ok else "exit 1"}
            fi
            if [[ "$all_args" =~ "telegram_auth auth" ]]; then
                {"exit 0" if auth_run_ok else "exit 1"}
            fi
            exit 0
        fi
        exit 0
        ;;
    info)
        {"exit 0" if daemon_ok else "echo 'Cannot connect to daemon' >&2; exit 1"}
        ;;
    *)
        exit 0
        ;;
esac
"""
    mock_bin.write_text(content)
    mock_bin.chmod(0o755)
    return str(mock_bin)




class BaseStandaloneInstallerTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="adulthub_installer_")
        self.isolated_root = Path(self.temp_dir.name)
        # Copy minimal files required by installer into isolated root
        shutil.copy(ROOT / "VERSION", self.isolated_root / "VERSION")
        shutil.copy(ROOT / "compose.standalone.yml", self.isolated_root / "compose.standalone.yml")
        shutil.copytree(ROOT / "common", self.isolated_root / "common")
        # Default Docker CLI for runs without --docker-bin: no containers reported, so the
        # production-project guard does not depend on the host's running stack.
        self.stub_dir = tempfile.TemporaryDirectory(prefix="adulthub_docker_stub_")
        self.docker_stub = make_docker_stub(Path(self.stub_dir.name))


    def tearDown(self):
        self.stub_dir.cleanup()
        self.temp_dir.cleanup()


    def run_installer(self, args, *, env=None, cwd=None, input_data=None):
        cmd = [str(SETUP_SCRIPT)] + args
        run_env = os.environ.copy()
        run_env["DOCKER_BIN"] = self.docker_stub
        if env:
            run_env.update(env)
        target_cwd = cwd or str(self.isolated_root)
        return subprocess.run(
            cmd,
            cwd=target_cwd,
            env=run_env,
            input=input_data,
            capture_output=True,
            text=True,
            timeout=15,
        )



class TestStandaloneInstaller(BaseStandaloneInstallerTestCase):
    def test_refuse_existing_production_deployment(self):
        """Installer must refuse execution if .env exists without standalone marker."""
        prod_env = self.isolated_root / ".env"
        prod_env.write_text("DATABASE_URL=postgresql://prod:secret@db:5432/adult\nHOST_IP=192.168.1.10\n")

        mock_docker = make_mock_docker(self.isolated_root)
        result = self.run_installer(["--non-interactive", "--docker-bin", mock_docker, "--root-dir", str(self.isolated_root)])

        self.assertEqual(result.returncode, 1)
        self.assertIn("Existing production deployment detected", result.stderr)
        # Confirm .env was NOT modified or overwritten
        self.assertIn("DATABASE_URL=postgresql://prod:secret@db:5432/adult", prod_env.read_text())
        self.assertNotIn("ADULT_HUB_STANDALONE", prod_env.read_text())


    def test_refuse_when_active_production_compose_project_running(self):
        """Installer must refuse if production 'adult-hub' compose project is running."""
        mock_docker = make_mock_docker(self.isolated_root, active_prod_project=True)
        result = self.run_installer(["--non-interactive", "--docker-bin", mock_docker, "--root-dir", str(self.isolated_root)])

        self.assertEqual(result.returncode, 1)
        self.assertIn("Active production Compose project 'adult-hub'", result.stderr)


    def test_guard_refuses_when_stub_reports_running_adult_hub_container(self):
        """Production guard still refuses when the default Docker CLI reports a running adult-hub container."""
        running_dir = Path(self.stub_dir.name) / "running"
        running_dir.mkdir()
        running_stub = make_docker_stub(running_dir, running_container=True)

        result = self.run_installer(
            ["--non-interactive", "--root-dir", str(self.isolated_root)],
            env={"DOCKER_BIN": running_stub},
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("Active production Compose project 'adult-hub' is currently running", result.stderr)
        self.assertFalse((self.isolated_root / ".env").exists())


    def test_missing_docker_cli(self):
        """Missing docker CLI produces an actionable error message."""
        res = self.run_installer([
            "--non-interactive",
            "--docker-bin", "/nonexistent/docker/bin",
            "--root-dir", str(self.isolated_root),
        ])
        self.assertEqual(res.returncode, 1)
        self.assertIn("Docker CLI '/nonexistent/docker/bin' is not installed", res.stderr)


    def test_missing_compose_v2(self):
        """Unsupported or missing Compose v2 produces an actionable error."""
        mock_docker = make_mock_docker(self.isolated_root, compose_v2_ok=False)
        res = self.run_installer([
            "--non-interactive",
            "--docker-bin", mock_docker,
            "--root-dir", str(self.isolated_root),
        ])
        self.assertEqual(res.returncode, 1)
        self.assertIn("Docker Compose v2 is required", res.stderr)


    def test_docker_daemon_unavailable(self):
        """Unavailable Docker daemon produces an actionable error without mutating files."""
        mock_docker = make_mock_docker(self.isolated_root, daemon_ok=False)
        res = self.run_installer([
            "--non-interactive",
            "--docker-bin", mock_docker,
            "--root-dir", str(self.isolated_root),
        ])
        self.assertEqual(res.returncode, 1)
        self.assertIn("Cannot connect to the Docker daemon", res.stderr)
        self.assertFalse((self.isolated_root / ".env").exists())


    def test_incompatible_existing_config_aborts(self):
        """If existing config.yaml violates schema, installer aborts to prevent corruption."""
        mock_docker = make_mock_docker(self.isolated_root)
        cfg_dir = self.isolated_root / "standalone"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        # Invalid config (illegal top-level key)
        (cfg_dir / "config.yaml").write_text("invalid_key: 12345\n")

        res = self.run_installer([
            "--non-interactive",
            "--docker-bin", mock_docker,
            "--root-dir", str(self.isolated_root),
        ])
        self.assertEqual(res.returncode, 1)
        self.assertIn("Existing config ./standalone/config.yaml is incompatible", res.stderr)


    def test_port_conflict_detection_and_alternative_offered(self):
        """If default resolver port is occupied, alternate port is suggested and used."""
        test_port = 8190
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", test_port))
        sock.listen(1)

        try:
            mock_docker = make_mock_docker(self.isolated_root)
            res = self.run_installer([
                "--non-interactive",
                "--bind-mode", "loopback",
                "--resolver-port", str(test_port),
                "--docker-bin", mock_docker,
                "--root-dir", str(self.isolated_root),
            ])
            self.assertEqual(res.returncode, 0)
            self.assertIn(f"Port {test_port} is in use", res.stdout)
            self.assertIn(f"Suggested alternate port: {test_port + 1}", res.stdout)

            env_content = (self.isolated_root / ".env").read_text()
            self.assertIn(f"RESOLVER_PORT={test_port + 1}", env_content)
        finally:
            sock.close()


    def test_setup_with_already_authorized_session_starts_services(self):
        """Pre-existing authorized Telegram session skips login and starts resolver, sync, and backup."""
        # 1. First run to generate environment
        mock_docker = make_mock_docker(self.isolated_root, auth_check_ok=True)
        res1 = self.run_installer([
            "--non-interactive",
            "--bind-mode", "loopback",
            "--docker-bin", mock_docker,
            "--root-dir", str(self.isolated_root),
        ])
        self.assertEqual(res1.returncode, 0)

        # Set Telegram credentials in .env
        env_path = self.isolated_root / ".env"
        env_content = env_path.read_text()
        env_content = env_content.replace("TELEGRAM_API_ID=", "TELEGRAM_API_ID=123456\n")
        env_content = env_content.replace("TELEGRAM_API_HASH=", "TELEGRAM_API_HASH=abcdef0123456789abcdef0123456789\n")
        env_path.write_text(env_content)

        # Create session file
        session_file = self.isolated_root / "standalone" / "data" / "telegram.session"
        session_file.write_bytes(b"existing_valid_session")

        # 2. Second run: already authorized
        res2 = self.run_installer([
            "--non-interactive",
            "--docker-bin", mock_docker,
            "--root-dir", str(self.isolated_root),
        ])
        self.assertEqual(res2.returncode, 0)
        self.assertIn("Telegram session authorized. Starting resolver and sync...", res2.stdout)
        self.assertIn("Telegram Auth:  AUTHORIZED", res2.stdout)
        self.assertIn("Core Services:  All services up and running", res2.stdout)

        # Verify docker compose up was called for resolver, sync
        docker_calls = (self.isolated_root / "docker_calls.log").read_text()
        lines = [line.strip() for line in docker_calls.splitlines()]
        self.assertTrue(any(line.endswith("up -d resolver sync") for line in lines), f"Expected exact invocation 'up -d resolver sync' in calls:\n{docker_calls}")
        self.assertFalse(any("backup" in line and "up -d" in line for line in lines), "Unexpected 'backup' service in compose up call")


    def test_setup_interactive_auth_success_updates_env_and_starts_services(self):
        """Interactive setup prompts for API ID/Hash, runs one-shot auth, and starts services."""
        mock_docker = make_mock_docker(self.isolated_root, auth_check_ok=False, auth_run_ok=True)
        # Inputs: API ID, API Hash
        input_stream = "123456\nabcdef0123456789abcdef0123456789\n"

        res = self.run_installer(
            [
                "--bind-mode", "loopback",
                "--docker-bin", mock_docker,
                "--root-dir", str(self.isolated_root),
            ],
            input_data=input_stream,
        )
        self.assertEqual(res.returncode, 0, f"STDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}")

        # Ensure credentials saved in .env
        env_content = (self.isolated_root / ".env").read_text()
        self.assertIn("TELEGRAM_API_ID=123456", env_content)
        self.assertIn("TELEGRAM_API_HASH=abcdef0123456789abcdef0123456789", env_content)

        # Ensure auth container was called and services started
        docker_calls = (self.isolated_root / "docker_calls.log").read_text()
        self.assertIn("telegram_auth auth", docker_calls)
        lines = [line.strip() for line in docker_calls.splitlines()]
        self.assertTrue(any(line.endswith("up -d resolver sync") for line in lines), f"Expected exact invocation 'up -d resolver sync' in calls:\n{docker_calls}")
        self.assertFalse(any("backup" in line and "up -d" in line for line in lines), "Unexpected 'backup' service in compose up call")
        self.assertIn("Telegram Auth:  AUTHORIZED", res.stdout)


    def test_setup_auth_failure_withholds_services(self):
        """When authentication fails or is cancelled, resolver and sync are withheld."""
        mock_docker = make_mock_docker(self.isolated_root, auth_check_ok=False, auth_run_ok=False)

        # Setup with credentials in .env
        res1 = self.run_installer([
            "--non-interactive",
            "--bind-mode", "loopback",
            "--docker-bin", mock_docker,
            "--root-dir", str(self.isolated_root),
        ])
        self.assertEqual(res1.returncode, 0)
        env_path = self.isolated_root / ".env"
        env_content = env_path.read_text()
        env_content = env_content.replace("TELEGRAM_API_ID=", "TELEGRAM_API_ID=123456\n")
        env_content = env_content.replace("TELEGRAM_API_HASH=", "TELEGRAM_API_HASH=abcdef0123456789abcdef0123456789\n")
        env_path.write_text(env_content)

        # Clear docker call log before test run
        (self.isolated_root / "docker_calls.log").write_text("")

        res2 = self.run_installer(
            [
                "--docker-bin", mock_docker,
                "--root-dir", str(self.isolated_root),
            ],
            input_data="\n",
        )
        self.assertEqual(res2.returncode, 0)
        self.assertIn("Telegram authentication failed or was cancelled", res2.stderr)
        self.assertIn("Services 'resolver' and 'sync' remain withheld", res2.stderr)

        # Crucial: resolver, sync must NOT be started
        docker_calls = (self.isolated_root / "docker_calls.log").read_text()
        lines = [line.strip() for line in docker_calls.splitlines()]
        self.assertFalse(any(line.endswith("up -d resolver sync") for line in lines), "Resolver and sync should not start on auth failure")
        self.assertFalse(any("backup" in line and "up -d" in line for line in lines))
        self.assertIn("NOT_AUTHORIZED", res2.stdout)


    def test_setup_concurrent_session_process_blocks_auth(self):
        """Running resolver or sync container blocks authentication to prevent SQLite locks."""
        mock_docker = make_mock_docker(self.isolated_root, active_session_services=True)

        res1 = self.run_installer([
            "--non-interactive",
            "--bind-mode", "loopback",
            "--docker-bin", mock_docker,
            "--root-dir", str(self.isolated_root),
        ])
        self.assertEqual(res1.returncode, 0)
        env_path = self.isolated_root / ".env"
        env_content = env_path.read_text()
        env_content = env_content.replace("TELEGRAM_API_ID=", "TELEGRAM_API_ID=123456\n")
        env_content = env_content.replace("TELEGRAM_API_HASH=", "TELEGRAM_API_HASH=abcdef0123456789abcdef0123456789\n")
        env_path.write_text(env_content)

        (self.isolated_root / "docker_calls.log").write_text("")

        res2 = self.run_installer(
            [
                "--docker-bin", mock_docker,
                "--root-dir", str(self.isolated_root),
            ],
            input_data="\n",
        )
        self.assertIn("Cannot authenticate while resolver or sync service is running", res2.stderr)
        docker_calls = (self.isolated_root / "docker_calls.log").read_text()
        self.assertNotIn("telegram_auth auth", docker_calls)
        self.assertNotIn("up -d resolver sync backup", docker_calls)


    def test_setup_protects_existing_unauthorized_session(self):
        """If target session file exists but is not authorized, setup refuses to overwrite and withholds services."""
        mock_docker = make_mock_docker(self.isolated_root, auth_check_ok=False)

        res1 = self.run_installer([
            "--non-interactive",
            "--bind-mode", "loopback",
            "--docker-bin", mock_docker,
            "--root-dir", str(self.isolated_root),
        ])
        self.assertEqual(res1.returncode, 0)
        env_path = self.isolated_root / ".env"
        env_content = env_path.read_text()
        env_content = env_content.replace("TELEGRAM_API_ID=", "TELEGRAM_API_ID=123456\n")
        env_content = env_content.replace("TELEGRAM_API_HASH=", "TELEGRAM_API_HASH=abcdef0123456789abcdef0123456789\n")
        env_path.write_text(env_content)

        # Place unauthorized session
        session_file = self.isolated_root / "standalone" / "data" / "telegram.session"
        session_file.write_bytes(b"preserved_pre_existing_data")

        (self.isolated_root / "docker_calls.log").write_text("")

        res2 = self.run_installer(
            [
                "--docker-bin", mock_docker,
                "--root-dir", str(self.isolated_root),
            ],
            input_data="\n",
        )
        self.assertIn("Existing Telegram session file is present but not authorized", res2.stderr)
        self.assertIn("setup will never delete or overwrite this file", res2.stderr)
        # Pre-existing bytes strictly preserved
        self.assertEqual(session_file.read_bytes(), b"preserved_pre_existing_data")

        docker_calls = (self.isolated_root / "docker_calls.log").read_text()
        self.assertNotIn("telegram_auth auth", docker_calls)
        lines = [line.strip() for line in docker_calls.splitlines()]
        self.assertFalse(any(line.endswith("up -d resolver sync") for line in lines), "Services must not start when session is unauthorized")
        self.assertFalse(any("backup" in line and "up -d" in line for line in lines))


    def test_setup_non_interactive_without_session_starts_services_and_gates_scans(self):
        """Non-interactive setup with credentials but missing session informs user and starts services with scans gated."""
        mock_docker = make_mock_docker(self.isolated_root, auth_check_ok=False)

        res1 = self.run_installer([
            "--non-interactive",
            "--bind-mode", "loopback",
            "--docker-bin", mock_docker,
            "--root-dir", str(self.isolated_root),
        ])
        self.assertEqual(res1.returncode, 0)
        env_path = self.isolated_root / ".env"
        env_content = env_path.read_text()
        env_content = env_content.replace("TELEGRAM_API_ID=", "TELEGRAM_API_ID=123456\n")
        env_content = env_content.replace("TELEGRAM_API_HASH=", "TELEGRAM_API_HASH=abcdef0123456789abcdef0123456789\n")
        env_path.write_text(env_content)

        (self.isolated_root / "docker_calls.log").write_text("")

        res2 = self.run_installer([
            "--non-interactive",
            "--docker-bin", mock_docker,
            "--root-dir", str(self.isolated_root),
        ])
        self.assertEqual(res2.returncode, 0)
        self.assertIn("interactive authentication is required", res2.stdout)
        self.assertIn("PENDING_INTERACTIVE_AUTH", res2.stdout)

        docker_calls = (self.isolated_root / "docker_calls.log").read_text()
        self.assertNotIn("telegram_auth auth", docker_calls)
        lines = [line.strip() for line in docker_calls.splitlines()]
        self.assertTrue(any(line.endswith("up -d resolver sync") for line in lines), "Services must start so Admin UI web wizard is reachable")
        self.assertFalse(any("backup" in line and "up -d" in line for line in lines))



if __name__ == "__main__":
    unittest.main()
