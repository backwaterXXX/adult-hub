"""Tests for Adult Hub standalone setup installation modes, network bindings, and idempotency."""

import stat
import subprocess
import unittest

from tests.test_installer import BaseStandaloneInstallerTestCase, make_mock_docker


class TestStandaloneInstallerSetup(BaseStandaloneInstallerTestCase):
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
        self.assertIn("DATABASE_URL=postgresql://adulthub:", env_content)
        self.assertNotIn("STANDALONE_DATABASE_URL=", env_content)
        self.assertIn("JELLYFIN_URL=http://jellyfin:8096", env_content)
        self.assertNotIn("STANDALONE_JELLYFIN_URL=", env_content)
        self.assertIn("ADMIN_PUBLIC=false", env_content)
        self.assertNotIn("STANDALONE_ADMIN_PUBLIC=", env_content)

        # Verify public URL matches the selected resolver port
        port_match = [line.split("=", 1)[1] for line in env_content.splitlines() if line.startswith("RESOLVER_PORT=")]
        self.assertTrue(port_match)
        sel_port = port_match[0]
        self.assertIn(f"RESOLVER_PUBLIC_URL=http://127.0.0.1:{sel_port}", env_content)

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
        self.assertIn(f"RESOLVER_PUBLIC_URL={playback_url}", env_content)
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


    def test_legacy_standalone_env_names_survive_rerun(self):
        """An existing .env that uses only STANDALONE_* names keeps those values when setup runs again."""
        mock_docker = make_mock_docker(self.isolated_root)
        env_path = self.isolated_root / ".env"
        env_path.write_text(
            "ADULT_HUB_STANDALONE=true\n"
            "POSTGRES_DB=adulthub\n"
            "POSTGRES_USER=adulthub\n"
            "POSTGRES_PASSWORD=legacy-pw-456\n"
            "STANDALONE_JELLYFIN_URL=http://legacy-jellyfin:8096\n"
            "STANDALONE_ADMIN_PUBLIC=true\n"
            "RESOLVER_BIND_IP=127.0.0.1\n"
            "RESOLVER_PORT=8090\n"
        )
        env_path.chmod(0o600)

        res = self.run_installer([
            "--non-interactive",
            "--docker-bin", mock_docker,
            "--root-dir", str(self.isolated_root),
        ])
        self.assertEqual(res.returncode, 0, f"Setup failed on legacy .env:\n{res.stderr}")
        env_after = env_path.read_text()
        self.assertIn("STANDALONE_JELLYFIN_URL=http://legacy-jellyfin:8096", env_after)
        self.assertIn("STANDALONE_ADMIN_PUBLIC=true", env_after)
        self.assertIn("POSTGRES_PASSWORD=legacy-pw-456", env_after)


    def test_legacy_standalone_resolver_public_url_migrates_on_rerun(self):
        """A legacy .env with only STANDALONE_RESOLVER_PUBLIC_URL gains RESOLVER_PUBLIC_URL with the same value."""
        mock_docker = make_mock_docker(self.isolated_root)
        env_path = self.isolated_root / ".env"
        env_path.write_text(
            "ADULT_HUB_STANDALONE=true\n"
            "POSTGRES_DB=adulthub\n"
            "POSTGRES_USER=adulthub\n"
            "POSTGRES_PASSWORD=legacy-pw-789\n"
            "STANDALONE_RESOLVER_PUBLIC_URL=http://192.168.1.50:8090\n"
            "RESOLVER_BIND_IP=192.168.1.50\n"
            "RESOLVER_PORT=8090\n"
        )
        env_path.chmod(0o600)

        res = self.run_installer([
            "--non-interactive",
            "--docker-bin", mock_docker,
            "--root-dir", str(self.isolated_root),
        ])
        self.assertEqual(res.returncode, 0, f"Setup failed on legacy .env:\n{res.stderr}")
        env_lines = env_path.read_text().splitlines()
        resolver_lines = [line for line in env_lines if line.startswith("RESOLVER_PUBLIC_URL=")]
        self.assertEqual(resolver_lines, ["RESOLVER_PUBLIC_URL=http://192.168.1.50:8090"])
        self.assertIn("STANDALONE_RESOLVER_PUBLIC_URL=http://192.168.1.50:8090", env_lines)
        self.assertEqual(stat.S_IMODE(env_path.stat().st_mode), 0o600)


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
        env_content = env_content.replace("JELLYFIN_API_KEY=", "JELLYFIN_API_KEY=test-jellyfin-key-123\n")
        env_path.write_text(env_content)

        res_status = self.run_installer([
            "--status",
            "--docker-bin", mock_docker,
            "--root-dir", str(self.isolated_root),
        ])
        self.assertEqual(res_status.returncode, 0)
        self.assertIn("Jellyfin API:   ACTIVE", res_status.stdout)



if __name__ == "__main__":
    unittest.main()
