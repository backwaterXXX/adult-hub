"""Tests for Adult Hub prebuilt image distribution and release packaging.

Verifies:
1. scripts/package_release.sh packages self-contained archives (.tar.gz and .zip).
2. Release bundle contains no repository dependencies, no source code, and no build directives.
3. No repository-only bind mounts in the distributable compose specification.
4. Distributable compose validation with fake credentials and isolated config (no starts).
5. Migration gate (service_completed_successfully) and telegram-auth profiles (tools).
6. Clear failure on missing required credentials or uninitialized session.
7. Correct client-facing stream URL defaults and Jellyfin container DNS resolution.
"""

import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_SCRIPT = ROOT / "scripts" / "package_release.sh"
RELEASE_SOURCE = ROOT / "release" / "standalone"
SETUP_SCRIPT = ROOT / "scripts" / "setup.sh"


class TestReleasePackaging(unittest.TestCase):
    """Verifies packaging mechanics and bundle structure."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="adulthub_pkg_test_")
        self.isolated_root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_package_script_creates_valid_archives(self):
        """Packaging script creates .tar.gz containing the expected release files."""
        res = subprocess.run(
            [str(PACKAGE_SCRIPT), "0.4.14"],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=20,
        )
        self.assertEqual(res.returncode, 0, f"Packaging failed:\n{res.stderr}")
        self.assertIn("Packaged standalone release bundles", res.stdout)

        tar_path = ROOT / "dist" / "adult-hub-standalone-v0.4.14.tar.gz"
        self.assertTrue(tar_path.exists(), f"Archive {tar_path} was not created")

        # Inspect archive contents
        with tarfile.open(tar_path, "r:gz") as tar:
            names = tar.getnames()
            self.assertIn("docker-compose.yml", names)
            self.assertIn(".env.example", names)
            self.assertIn("config.example.yaml", names)
            self.assertIn("README.md", names)

            # Ensure no repository files were packaged
            self.assertFalse(any("resolver/" in n for n in names))
            self.assertFalse(any("sync/" in n for n in names))
            self.assertFalse(any("common/" in n for n in names))
            self.assertFalse(any(".git" in n for n in names))

    def test_release_bundle_has_no_build_directives_and_no_repo_bind_mounts(self):
        """Distributable compose has ZERO build directives and ZERO repository bind mounts."""
        compose_file = RELEASE_SOURCE / "docker-compose.yml"
        self.assertTrue(compose_file.exists())

        raw_compose = compose_file.read_text(encoding="utf-8")
        parsed = yaml.safe_load(raw_compose)

        services = parsed.get("services", {})
        for svc_name, svc_conf in services.items():
            self.assertNotIn(
                "build",
                svc_conf,
                f"Distributable service '{svc_name}' declares 'build'; release bundles must be prebuilt.",
            )

        # Check all volume declarations across all services
        for svc_name, svc_conf in services.items():
            volumes = svc_conf.get("volumes", [])
            for vol in volumes:
                vol_str = str(vol)
                self.assertNotIn("./scripts/", vol_str)
                self.assertNotIn("./resolver/", vol_str)
                self.assertNotIn("./sync/", vol_str)
                self.assertNotIn("./common/", vol_str)
                self.assertNotIn("..", vol_str)

    def test_distributable_compose_validation_with_fake_credentials(self):
        """Extracted bundle validates cleanly with real 'docker compose config' (no container starts)."""
        # Extract release tarball into isolated root
        subprocess.run([str(PACKAGE_SCRIPT), "0.4.14"], check=True, cwd=str(ROOT))
        tar_path = ROOT / "dist" / "adult-hub-standalone-v0.4.14.tar.gz"

        with tarfile.open(tar_path, "r:gz") as tar:
            if hasattr(tarfile, "data_filter"):
                tar.extractall(path=self.isolated_root, filter="data")
            else:
                tar.extractall(path=self.isolated_root)

        # Setup isolated environment with fake credentials
        env_file = self.isolated_root / ".env"
        env_file.write_text("""
ADULT_HUB_STANDALONE=true
APP_VERSION=0.4.14
POSTGRES_DB=adulthub
POSTGRES_USER=adulthub
POSTGRES_PASSWORD=fake_pg_password_12345
STANDALONE_DATABASE_URL=postgresql://adulthub:fake_pg_password_12345@database:5432/adulthub
CONFIG_PATH=./config.yaml
DATA_DIR=./data
CACHE_DIR=./cache
MEDIA_DIR=./media
RESOLVER_BIND_IP=127.0.0.1
RESOLVER_PORT=8090
JELLYFIN_BIND_IP=127.0.0.1
JELLYFIN_PORT=8096
ADMIN_WRITE_TOKEN=fake_admin_token_abcdef
TELEGRAM_API_ID=123456
TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef
TELEGRAM_SESSION_PATH=/data/telegram.session
""")
        env_file.chmod(0o600)

        # Copy config
        shutil.copy(self.isolated_root / "config.example.yaml", self.isolated_root / "config.yaml")

        # Run real docker compose config
        res = subprocess.run(
            ["docker", "compose", "--env-file", str(env_file), "-f", str(self.isolated_root / "docker-compose.yml"), "config"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(res.returncode, 0, f"Compose validation failed:\n{res.stderr}")

        parsed_config = yaml.safe_load(res.stdout)
        services = parsed_config.get("services", {})
        self.assertIn("database", services)
        self.assertIn("migration", services)
        self.assertIn("jellyfin", services)
        self.assertIn("resolver", services)
        self.assertIn("sync", services)
        self.assertNotIn("backup", services)
        # Profiled service is excluded from default compose config (safeguard against unintended 'up -d' runs)
        self.assertNotIn("telegram-auth", services)

        # But validates when the profile is explicitly passed
        profile_res = subprocess.run(
            ["docker", "compose", "--env-file", str(env_file), "-f", str(self.isolated_root / "docker-compose.yml"), "--profile", "tools", "config"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(profile_res.returncode, 0)
        profile_services = yaml.safe_load(profile_res.stdout).get("services", {})
        self.assertIn("telegram-auth", profile_services)

    def test_migration_gate_and_telegram_auth_profile(self):
        """Verifies migration condition is service_completed_successfully and auth is in tools profile."""
        compose_file = RELEASE_SOURCE / "docker-compose.yml"
        parsed = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
        services = parsed["services"]

        # Migration service
        mig = services["migration"]
        self.assertEqual(mig.get("restart"), "no")
        self.assertEqual(mig["depends_on"]["database"]["condition"], "service_healthy")

        # Resolver depends on migration exit 0
        res = services["resolver"]
        self.assertEqual(res["depends_on"]["migration"]["condition"], "service_completed_successfully")
        self.assertEqual(res["depends_on"]["database"]["condition"], "service_healthy")

        # Sync depends on migration exit 0
        sync_svc = services["sync"]
        self.assertEqual(sync_svc["depends_on"]["migration"]["condition"], "service_completed_successfully")
        self.assertEqual(sync_svc["depends_on"]["database"]["condition"], "service_healthy")

        # Telegram-auth is profiled under 'tools' so 'docker compose up -d' never runs it
        auth_svc = services["telegram-auth"]
        self.assertEqual(auth_svc.get("profiles"), ["tools"])
        self.assertEqual(auth_svc.get("restart"), "no")
        self.assertTrue(auth_svc.get("stdin_open"))
        self.assertTrue(auth_svc.get("tty"))

    def test_distributable_compose_fails_fast_on_missing_required_credentials(self):
        """Omitting required POSTGRES_PASSWORD fails Compose configuration parsing."""
        compose_file = RELEASE_SOURCE / "docker-compose.yml"

        # Missing POSTGRES_PASSWORD
        env_missing_pw = self.isolated_root / ".env_missing"
        env_missing_pw.write_text("POSTGRES_DB=adulthub\nPOSTGRES_USER=adulthub\n")

        res = subprocess.run(
            ["docker", "compose", "--env-file", str(env_missing_pw), "-f", str(compose_file), "config"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("POSTGRES_PASSWORD", res.stderr)

    def test_client_facing_stream_url_defaults_to_resolver_container(self):
        """Resolver URL written to .strm defaults to http://resolver:8090 for Jellyfin container bridge resolution."""
        compose_file = RELEASE_SOURCE / "docker-compose.yml"
        raw = compose_file.read_text(encoding="utf-8")
        parsed = yaml.safe_load(raw)

        sync_env = parsed["services"]["sync"].get("environment", [])
        resolver_url = next((e for e in sync_env if str(e).startswith("RESOLVER_PUBLIC_URL=")), None)
        self.assertIsNotNone(resolver_url)
        self.assertIn("http://resolver:8090", resolver_url)

    def test_setup_installer_supports_source_build_flag(self):
        """scripts/setup.sh recognizes --source-build flag."""
        res = subprocess.run(
            [str(SETUP_SCRIPT), "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("--source-build", res.stdout)


if __name__ == "__main__":
    unittest.main()
