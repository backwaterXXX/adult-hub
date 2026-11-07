"""Tests for Adult Hub Stage 2 standalone setup installer (scripts/setup.sh).

Verifies safety, idempotency, isolated root execution, prerequisite checks,
network binding, port collision detection, and secret redaction.
"""

import os
import shutil
import socket
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

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


class TestStandaloneInstaller(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="adulthub_installer_")
        self.isolated_root = Path(self.temp_dir.name)
        # Copy minimal files required by installer into isolated root
        shutil.copy(ROOT / "VERSION", self.isolated_root / "VERSION")
        shutil.copy(ROOT / "compose.standalone.yml", self.isolated_root / "compose.standalone.yml")
        shutil.copytree(ROOT / "common", self.isolated_root / "common")

    def tearDown(self):
        self.temp_dir.cleanup()

    def run_installer(self, args, *, env=None, cwd=None, input_data=None):
        cmd = [str(SETUP_SCRIPT)] + args
        run_env = os.environ.copy()
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

    def test_fresh_installation_loopback_mode(self):
        """Installer generates valid .env, config.yaml, and isolated directories."""
        mock_docker = make_mock_docker(self.isolated_root)
        result = self.run_installer([
            "--non-interactive",
            "--bind-mode", "loopback",
            "--docker-bin", mock_docker,
            "--root-dir", str(self.isolated_root),
        ])

        self.assertEqual(result.returncode, 0, f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")

        # Check .env generation and permissions (mode 0600)
        env_path = self.isolated_root / ".env"
        self.assertTrue(env_path.is_file())
        file_mode = stat.S_IMODE(env_path.stat().st_mode)
        self.assertEqual(file_mode, 0o600)

        env_content = env_path.read_text()
        self.assertIn("ADULT_HUB_STANDALONE=true", env_content)
        self.assertIn("POSTGRES_DB=adulthub", env_content)
        self.assertIn("POSTGRES_USER=adulthub", env_content)
        self.assertIn("RESOLVER_BIND_IP=127.0.0.1", env_content)
        self.assertIn("JELLYFIN_BIND_IP=127.0.0.1", env_content)
        self.assertIn("STANDALONE_JELLYFIN_URL=http://jellyfin:8096", env_content)
        self.assertIn("STANDALONE_ADMIN_PUBLIC=false", env_content)

        # Verify public URL matches the selected resolver port
        port_match = [line.split("=", 1)[1] for line in env_content.splitlines() if line.startswith("RESOLVER_PORT=")]
        self.assertTrue(port_match)
        sel_port = port_match[0]
        self.assertIn(f"STANDALONE_RESOLVER_PUBLIC_URL=http://127.0.0.1:{sel_port}", env_content)

        # Check runtime directories
        self.assertTrue((self.isolated_root / "standalone" / "data").is_dir())
        self.assertTrue((self.isolated_root / "standalone" / "cache").is_dir())
        self.assertTrue((self.isolated_root / "standalone" / "media").is_dir())
        self.assertTrue((self.isolated_root / "standalone" / "data" / "backups").is_dir())

        # Check config.yaml generation
        config_path = self.isolated_root / "standalone" / "config.yaml"
        self.assertTrue(config_path.is_file())
        config_text = config_path.read_text()
        self.assertIn("channels: []", config_text)
        self.assertIn("provider: telegram", config_text)

        # Secret redaction: ensure generated passwords do not appear in console output
        pg_pass = [line.split("=", 1)[1] for line in env_content.splitlines() if line.startswith("POSTGRES_PASSWORD=")][0]
        admin_tok = [line.split("=", 1)[1] for line in env_content.splitlines() if line.startswith("ADMIN_WRITE_TOKEN=")][0]
        self.assertNotIn(pg_pass, result.stdout)
        self.assertNotIn(admin_tok, result.stdout)

        # Real docker compose config validation against the generated .env
        compose_check = subprocess.run(
            ["docker", "compose", "--env-file", str(env_path), "-f", str(self.isolated_root / "compose.standalone.yml"), "config", "--quiet"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(compose_check.returncode, 0, f"Compose validation failed:\n{compose_check.stderr}")

    def test_fresh_installation_lan_mode(self):
        """Installer configures specific LAN IP and client playback URL."""
        mock_docker = make_mock_docker(self.isolated_root)
        lan_ip = "192.168.1.188"
        playback_url = f"http://{lan_ip}:8090"

        result = self.run_installer([
            "--non-interactive",
            "--bind-mode", "lan",
            "--bind-ip", lan_ip,
            "--public-url", playback_url,
            "--docker-bin", mock_docker,
            "--root-dir", str(self.isolated_root),
        ])

        self.assertEqual(result.returncode, 0, result.stderr)
        env_content = (self.isolated_root / ".env").read_text()
        self.assertIn(f"RESOLVER_BIND_IP={lan_ip}", env_content)
        self.assertIn(f"JELLYFIN_BIND_IP={lan_ip}", env_content)
        self.assertIn(f"STANDALONE_RESOLVER_PUBLIC_URL={playback_url}", env_content)
        # Ensure 0.0.0.0 is not used
        self.assertNotIn("0.0.0.0", env_content)

    def test_lan_mode_rejects_loopback_and_wildcard_url(self):
        """Installer rejects loopback or wildcard client playback URLs in LAN mode."""
        mock_docker = make_mock_docker(self.isolated_root)
        lan_ip = "192.168.1.188"

        for bad_url in ("http://127.0.0.1:8090", "http://localhost:8090", "http://0.0.0.0:8090", "not-a-valid-url"):
            with self.subTest(url=bad_url):
                res = self.run_installer([
                    "--non-interactive",
                    "--bind-mode", "lan",
                    "--bind-ip", lan_ip,
                    "--public-url", bad_url,
                    "--docker-bin", mock_docker,
                    "--root-dir", str(self.isolated_root),
                ])
                self.assertEqual(res.returncode, 1)
                self.assertIn("Invalid --public-url", res.stderr)

    def test_idempotent_repeated_setup_preserves_secrets_and_custom_config(self):
        """Running setup repeatedly must preserve secrets, tokens, and customized config."""
        mock_docker = make_mock_docker(self.isolated_root)

        # 1st run
        res1 = self.run_installer([
            "--non-interactive",
            "--bind-mode", "loopback",
            "--docker-bin", mock_docker,
            "--root-dir", str(self.isolated_root),
        ])
        self.assertEqual(res1.returncode, 0)
        env1 = (self.isolated_root / ".env").read_text()

        # Modify config to add a channel
        cfg_file = self.isolated_root / "standalone" / "config.yaml"
        custom_cfg = cfg_file.read_text().replace("channels: []", "channels:\n      - id: '@mychannel'\n        enabled: true")
        cfg_file.write_text(custom_cfg)

        # 2nd run
        res2 = self.run_installer([
            "--non-interactive",
            "--docker-bin", mock_docker,
            "--root-dir", str(self.isolated_root),
        ])
        self.assertEqual(res2.returncode, 0)
        env2 = (self.isolated_root / ".env").read_text()
        cfg2 = cfg_file.read_text()

        # Credentials must be identical
        self.assertEqual(env1, env2)
        # Custom config must be preserved
        self.assertIn("@mychannel", cfg2)

    def test_interrupted_resume_with_existing_volume_missing_env(self):
        """If database volume exists but .env is missing, abort and request recovery."""
        mock_docker = make_mock_docker(self.isolated_root, volume_exists=True)

        res = self.run_installer([
            "--non-interactive",
            "--docker-bin", mock_docker,
            "--root-dir", str(self.isolated_root),
        ])

        self.assertEqual(res.returncode, 1)
        self.assertIn("Existing standalone database volume found with missing .env", res.stderr)

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

    def test_setup_status_flag(self):
        """--status flag displays summary without running services or mutating environment."""
        mock_docker = make_mock_docker(self.isolated_root, auth_check_ok=True)
        # 1. Uninitialized checkout
        res_uninit = self.run_installer(["--status", "--root-dir", str(self.isolated_root)])
        self.assertEqual(res_uninit.returncode, 1)
        self.assertIn("Standalone deployment is not initialized yet", res_uninit.stderr)

        # 2. Initialize minimal standalone environment
        res_init = self.run_installer([
            "--non-interactive",
            "--bind-mode", "loopback",
            "--docker-bin", mock_docker,
            "--root-dir", str(self.isolated_root),
        ])
        self.assertEqual(res_init.returncode, 0)

        # 3. Query status
        (self.isolated_root / "docker_calls.log").write_text("")
        res_status = self.run_installer([
            "--status",
            "--docker-bin", mock_docker,
            "--root-dir", str(self.isolated_root),
        ])
        self.assertEqual(res_status.returncode, 0)
        self.assertIn("Adult Hub Standalone Setup Summary", res_status.stdout)
        self.assertIn("Admin UI:", res_status.stdout)
        self.assertIn("/admin/#sources", res_status.stdout)
        self.assertIn("Jellyfin UI:", res_status.stdout)
        self.assertIn("Jellyfin API:   INACTIVE (API key optional; basic playback available)", res_status.stdout)
        self.assertIn("Next Steps:", res_status.stdout)

        # Confirm no service startup
        docker_calls = (self.isolated_root / "docker_calls.log").read_text()
        self.assertNotIn("up -d", docker_calls)

    def test_setup_summary_reports_active_jellyfin_api_key(self):
        """Summary accurately reports ACTIVE Jellyfin API status when API key is configured."""
        mock_docker = make_mock_docker(self.isolated_root, auth_check_ok=True)
        res_init = self.run_installer([
            "--non-interactive",
            "--bind-mode", "loopback",
            "--docker-bin", mock_docker,
            "--root-dir", str(self.isolated_root),
        ])
        self.assertEqual(res_init.returncode, 0)

        env_path = self.isolated_root / ".env"
        env_content = env_path.read_text()
        env_content = env_content.replace("STANDALONE_JELLYFIN_API_KEY=", "STANDALONE_JELLYFIN_API_KEY=test-jellyfin-key-123\n")
        env_path.write_text(env_content)

        res_status = self.run_installer([
            "--status",
            "--docker-bin", mock_docker,
            "--root-dir", str(self.isolated_root),
        ])
        self.assertEqual(res_status.returncode, 0)
        self.assertIn("Jellyfin API:   ACTIVE", res_status.stdout)

    def test_setup_both_ports_occupied_selects_distinct_alternatives(self):
        """When both resolver and Jellyfin ports are occupied, setup finds distinct non-colliding ports."""
        port1 = 8290
        port2 = 8296
        sock1 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock1.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock1.bind(("127.0.0.1", port1))
        sock1.listen(1)

        sock2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock2.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock2.bind(("127.0.0.1", port2))
        sock2.listen(1)

        try:
            mock_docker = make_mock_docker(self.isolated_root)
            res = self.run_installer([
                "--non-interactive",
                "--bind-mode", "loopback",
                "--resolver-port", str(port1),
                "--jellyfin-port", str(port2),
                "--docker-bin", mock_docker,
                "--root-dir", str(self.isolated_root),
            ])
            self.assertEqual(res.returncode, 0)
            env_content = (self.isolated_root / ".env").read_text()

            # Parse selected ports
            r_port = [line.split("=", 1)[1] for line in env_content.splitlines() if line.startswith("RESOLVER_PORT=")][0]
            j_port = [line.split("=", 1)[1] for line in env_content.splitlines() if line.startswith("JELLYFIN_PORT=")][0]

            self.assertNotEqual(r_port, str(port1))
            self.assertNotEqual(j_port, str(port2))
            self.assertNotEqual(r_port, j_port)
        finally:
            sock1.close()
            sock2.close()

    def test_same_port_override_collision_fails_with_actionable_error(self):
        """When resolver and Jellyfin overrides request the same free host port, installer fails with clear actionable error."""
        free_port = 9188
        mock_docker = make_mock_docker(self.isolated_root)
        res = self.run_installer([
            "--non-interactive",
            "--bind-mode", "loopback",
            "--resolver-port", str(free_port),
            "--jellyfin-port", str(free_port),
            "--docker-bin", mock_docker,
            "--root-dir", str(self.isolated_root),
        ])
        self.assertEqual(res.returncode, 1)
        self.assertIn("Port collision", res.stderr)
        self.assertIn("Specify distinct ports", res.stderr)

    def test_interrupted_resume_with_existing_volume_and_valid_password(self):
        """When volume exists and .env already has POSTGRES_PASSWORD, setup resumes cleanly."""
        mock_docker = make_mock_docker(self.isolated_root, volume_exists=True)
        # Create valid existing .env with standalone marker and postgres password
        env_path = self.isolated_root / ".env"
        env_path.write_text(
            "ADULT_HUB_STANDALONE=true\n"
            "POSTGRES_DB=adulthub\n"
            "POSTGRES_USER=adulthub\n"
            "POSTGRES_PASSWORD=preexisting-secret-pw\n"
            "STANDALONE_DATABASE_URL=postgresql://adulthub:preexisting-secret-pw@database:5432/adulthub\n"
        )
        env_path.chmod(0o600)

        res = self.run_installer([
            "--non-interactive",
            "--docker-bin", mock_docker,
            "--root-dir", str(self.isolated_root),
        ])
        self.assertEqual(res.returncode, 0, f"Setup failed to resume:\n{res.stderr}")
        env_after = env_path.read_text()
        self.assertIn("POSTGRES_PASSWORD=preexisting-secret-pw", env_after)

    def test_setup_preserves_preexisting_media_backups_and_data(self):
        """Existing media files, backups, and data are strictly preserved during installer run."""
        mock_docker = make_mock_docker(self.isolated_root)

        # Pre-create media, backup, and cache files
        media_file = self.isolated_root / "standalone" / "media" / "test_clip.strm"
        media_file.parent.mkdir(parents=True, exist_ok=True)
        media_file.write_text("http://127.0.0.1:8090/stream/123\n")

        backup_file = self.isolated_root / "standalone" / "data" / "backups" / "backup_20261106.sql.gz"
        backup_file.parent.mkdir(parents=True, exist_ok=True)
        backup_file.write_bytes(b"gzip_compressed_backup_data")

        data_file = self.isolated_root / "standalone" / "data" / "admin-source-overrides.json"
        data_file.write_text('{"sources": {"telegram": {"channels": []}}}')

        res = self.run_installer([
            "--non-interactive",
            "--bind-mode", "loopback",
            "--docker-bin", mock_docker,
            "--root-dir", str(self.isolated_root),
        ])
        self.assertEqual(res.returncode, 0)

        # Verify all files remain intact
        self.assertTrue(media_file.exists())
        self.assertEqual(media_file.read_text(), "http://127.0.0.1:8090/stream/123\n")

        self.assertTrue(backup_file.exists())
        self.assertEqual(backup_file.read_bytes(), b"gzip_compressed_backup_data")

        self.assertTrue(data_file.exists())
        self.assertEqual(data_file.read_text(), '{"sources": {"telegram": {"channels": []}}}')

    def test_setup_with_existing_env_and_missing_config(self):
        """When .env already exists but config.yaml is missing, setup generates config and preserves .env."""
        mock_docker = make_mock_docker(self.isolated_root)
        env_path = self.isolated_root / ".env"
        env_path.write_text(
            "ADULT_HUB_STANDALONE=true\n"
            "POSTGRES_DB=adulthub\n"
            "POSTGRES_USER=adulthub\n"
            "POSTGRES_PASSWORD=custom-pw-123\n"
            "RESOLVER_BIND_IP=127.0.0.1\n"
            "RESOLVER_PORT=8090\n"
        )
        env_path.chmod(0o600)

        config_path = self.isolated_root / "standalone" / "config.yaml"
        self.assertFalse(config_path.exists())

        res = self.run_installer([
            "--non-interactive",
            "--docker-bin", mock_docker,
            "--root-dir", str(self.isolated_root),
        ])
        self.assertEqual(res.returncode, 0)
        self.assertTrue(config_path.exists())
        self.assertIn("POSTGRES_PASSWORD=custom-pw-123", env_path.read_text())


if __name__ == "__main__":
    unittest.main()
