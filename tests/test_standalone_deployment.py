"""Tests for Adult Hub Stage 5 standalone deployment and Compose architecture.

Verifies:
1. No GPU hardware acceleration (/dev/dri) or external Docker network requirements.
2. Compose configuration validation against freshly generated isolated environment.
3. PostgreSQL persistence declarations (named volumes).
4. Production deployment detection and isolation.
5. Preservation of media, backups, and data.
"""

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from tests.helpers.docker_stub import make_docker_stub

ROOT = Path(__file__).resolve().parents[1]
STANDALONE_COMPOSE = ROOT / "compose.standalone.yml"
PRODUCTION_COMPOSE = ROOT / "docker-compose.yml"
SETUP_SCRIPT = ROOT / "scripts" / "setup.sh"


class TestStandaloneDeploymentDeclarations(unittest.TestCase):
    """Verifies architectural guarantees in compose.standalone.yml."""

    @classmethod
    def setUpClass(cls):
        with open(STANDALONE_COMPOSE, "r", encoding="utf-8") as f:
            cls.standalone_raw = f.read()
            cls.standalone_cfg = yaml.safe_load(cls.standalone_raw)

        with open(PRODUCTION_COMPOSE, "r", encoding="utf-8") as f:
            cls.prod_raw = f.read()
            cls.prod_cfg = yaml.safe_load(cls.prod_raw)

    def test_no_gpu_and_no_hardware_devices_required(self):
        """Standalone deployment must NOT require GPU devices (/dev/dri) or hardware acceleration."""
        services = self.standalone_cfg.get("services", {})
        for svc_name, svc_conf in services.items():
            self.assertNotIn(
                "devices",
                svc_conf,
                f"Service '{svc_name}' in compose.standalone.yml declares devices; standalone must run on generic CPU hosts.",
            )
        self.assertNotIn("/dev/dri", self.standalone_raw)

    def test_no_external_docker_network(self):
        """Standalone deployment must NOT require external Docker networks (e.g. selfhost)."""
        networks = self.standalone_cfg.get("networks", {})
        for net_name, net_conf in networks.items():
            if net_conf:
                self.assertFalse(
                    net_conf.get("external", False),
                    f"Network '{net_name}' in compose.standalone.yml is marked external; standalone must create its own network.",
                )
        # Contrast with production compose which requires external 'selfhost'
        prod_networks = self.prod_cfg.get("networks", {})
        self.assertTrue(
            prod_networks.get("selfhost", {}).get("external", False),
            "Production compose is expected to use external 'selfhost' network.",
        )

    def test_postgresql_persistence_declarations(self):
        """PostgreSQL must use a dedicated persistent named volume."""
        volumes = self.standalone_cfg.get("volumes", {})
        self.assertIn("postgres_data", volumes, "postgres_data named volume missing in standalone Compose volumes")

        # Database volume mount
        db_volumes = self.standalone_cfg["services"]["database"]["volumes"]
        self.assertTrue(
            any("postgres_data:/var/lib/postgresql/data" in str(v) for v in db_volumes),
            "PostgreSQL service does not mount postgres_data named volume at /var/lib/postgresql/data",
        )

        # Standalone media volume mount (read-write for sync/resolver)
        sync_volumes = self.standalone_cfg["services"]["sync"]["volumes"]
        self.assertTrue(
            any(":/media/adult" in str(v) and ":ro" not in str(v) for v in sync_volumes),
            "Sync service must mount media directory with write permissions",
        )

    def test_resolver_url_for_strm_is_reachable_from_the_compose_network(self):
        """Resolver URL written to .strm in standalone must use Compose DNS."""
        sync_env = self.standalone_cfg["services"]["sync"].get("environment", [])
        resolver_url_entry = next((e for e in sync_env if str(e).startswith("RESOLVER_PUBLIC_URL=")), None)
        self.assertIsNotNone(resolver_url_entry, "RESOLVER_PUBLIC_URL must be defined for sync service")
        # Must resolve to http://resolver:8090 by default, never 127.0.0.1 or host-only binding
        self.assertIn("http://resolver:8090", resolver_url_entry)
        self.assertNotIn("STANDALONE_RESOLVER_PUBLIC_URL", resolver_url_entry)


class TestStandaloneComposeConfigValidation(unittest.TestCase):
    """Validates real docker compose config generation with isolated temporary env files."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="adulthub_compose_test_")
        self.isolated_root = Path(self.temp_dir.name)
        shutil.copy(ROOT / "VERSION", self.isolated_root / "VERSION")
        shutil.copy(STANDALONE_COMPOSE, self.isolated_root / "compose.standalone.yml")
        shutil.copytree(ROOT / "common", self.isolated_root / "common")
        # setup.sh must not depend on the host's running containers, so it gets a Docker CLI stub.
        self.stub_dir = tempfile.TemporaryDirectory(prefix="adulthub_docker_stub_")
        self.setup_env = {**os.environ, "DOCKER_BIN": make_docker_stub(Path(self.stub_dir.name))}

    def tearDown(self):
        self.stub_dir.cleanup()
        self.temp_dir.cleanup()

    def test_compose_config_validation_with_generated_env(self):
        """Compose config command parses successfully with freshly generated standalone .env."""
        # Run installer in --skip-docker-start mode to generate .env and config
        setup_res = subprocess.run(
            [str(SETUP_SCRIPT), "--non-interactive", "--skip-docker-start", "--root-dir", str(self.isolated_root)],
            capture_output=True,
            text=True,
            timeout=15,
            env=self.setup_env,
        )
        self.assertEqual(setup_res.returncode, 0, f"Installer failed:\n{setup_res.stderr}")

        env_path = self.isolated_root / ".env"
        self.assertTrue(env_path.exists())
        self.assertEqual(stat.S_IMODE(env_path.stat().st_mode), 0o600)

        # Run real docker compose config
        config_res = subprocess.run(
            [
                "docker", "compose",
                "--env-file", str(env_path),
                "-p", "adult-hub-standalone-test",
                "-f", str(self.isolated_root / "compose.standalone.yml"),
                "config",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(config_res.returncode, 0, f"docker compose config failed:\n{config_res.stderr}")

        # Verify parsed compose contains all expected services and no unresolved required vars
        parsed_config = yaml.safe_load(config_res.stdout)
        services = parsed_config.get("services", {})
        self.assertIn("database", services)
        self.assertIn("migration", services)
        self.assertIn("resolver", services)
        self.assertIn("sync", services)
        self.assertNotIn("backup", services)

        # Verify environment values are substituted
        db_env = services["database"].get("environment", {})
        self.assertEqual(db_env.get("POSTGRES_DB"), "adulthub")
        self.assertEqual(db_env.get("POSTGRES_USER"), "adulthub")
        self.assertTrue(db_env.get("POSTGRES_PASSWORD"))

        sync_env = services["sync"].get("environment", {})
        # sync must receive the URL the installer wrote to .env, not the in-network default
        written_url = next(
            line.split("=", 1)[1]
            for line in env_path.read_text().splitlines()
            if line.startswith("RESOLVER_PUBLIC_URL=")
        )
        self.assertEqual(sync_env.get("RESOLVER_PUBLIC_URL"), written_url)


class TestStandalonePersistenceAndIsolation(unittest.TestCase):
    """Verifies preservation of media/backups/data and production isolation."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="adulthub_persist_test_")
        self.isolated_root = Path(self.temp_dir.name)
        shutil.copy(ROOT / "VERSION", self.isolated_root / "VERSION")
        shutil.copy(STANDALONE_COMPOSE, self.isolated_root / "compose.standalone.yml")
        shutil.copytree(ROOT / "common", self.isolated_root / "common")
        # setup.sh must not depend on the host's running containers, so it gets a Docker CLI stub.
        self.stub_dir = tempfile.TemporaryDirectory(prefix="adulthub_docker_stub_")
        self.setup_env = {**os.environ, "DOCKER_BIN": make_docker_stub(Path(self.stub_dir.name))}

    def tearDown(self):
        self.stub_dir.cleanup()
        self.temp_dir.cleanup()

    def test_media_and_backup_data_preservation_across_repeated_runs(self):
        """Pre-existing media, backups, and runtime state are never purged or altered."""
        # 1. Run initial setup
        setup_res = subprocess.run(
            [str(SETUP_SCRIPT), "--non-interactive", "--skip-docker-start", "--root-dir", str(self.isolated_root)],
            capture_output=True,
            text=True,
            timeout=15,
            env=self.setup_env,
        )
        self.assertEqual(setup_res.returncode, 0)

        # 2. Add sample media, backup, and state data
        media_clip = self.isolated_root / "standalone" / "media" / "sample_video.strm"
        media_clip.write_text("http://127.0.0.1:8090/stream/42\n")

        backup_archive = self.isolated_root / "standalone" / "data" / "backups" / "db_dump_v1.sql.gz"
        backup_archive.write_bytes(b"\x1f\x8b\x08test_backup_binary_content")

        overrides_file = self.isolated_root / "standalone" / "data" / "admin-source-overrides.json"
        overrides_file.write_text('{"sources": {"telegram": {"channels": [{"id": "@channel1"}]}}}')

        env_before = (self.isolated_root / ".env").read_text()
        config_before = (self.isolated_root / "standalone" / "config.yaml").read_text()

        # 3. Run setup again
        repeat_res = subprocess.run(
            [str(SETUP_SCRIPT), "--non-interactive", "--skip-docker-start", "--root-dir", str(self.isolated_root)],
            capture_output=True,
            text=True,
            timeout=15,
            env=self.setup_env,
        )
        self.assertEqual(repeat_res.returncode, 0)

        # 4. Assert all files are preserved byte-for-byte
        self.assertEqual(media_clip.read_text(), "http://127.0.0.1:8090/stream/42\n")
        self.assertEqual(backup_archive.read_bytes(), b"\x1f\x8b\x08test_backup_binary_content")
        self.assertEqual(overrides_file.read_text(), '{"sources": {"telegram": {"channels": [{"id": "@channel1"}]}}}')
        self.assertEqual(env_before, (self.isolated_root / ".env").read_text())
        self.assertEqual(config_before, (self.isolated_root / "standalone" / "config.yaml").read_text())

    def test_refuse_production_checkout_with_existing_production_env(self):
        """Installer and standalone commands refuse to execute if .env lacks standalone marker."""
        prod_env = self.isolated_root / ".env"
        prod_env.write_text("DATABASE_URL=postgresql://prod_user:secret@postgres.prod:5432/adult_hub\n")

        res = subprocess.run(
            [str(SETUP_SCRIPT), "--non-interactive", "--skip-docker-start", "--root-dir", str(self.isolated_root)],
            capture_output=True,
            text=True,
            timeout=15,
            env=self.setup_env,
        )
        self.assertEqual(res.returncode, 1)
        self.assertIn("Existing production deployment detected", res.stderr)
        # Production .env must remain untouched
        self.assertEqual(prod_env.read_text(), "DATABASE_URL=postgresql://prod_user:secret@postgres.prod:5432/adult_hub\n")


if __name__ == "__main__":
    unittest.main()
