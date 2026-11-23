"""Keep the two standalone Compose files in step.

`compose.standalone.yml` (built from source) and `release/standalone/docker-compose.yml`
(prebuilt images, shipped by scripts/package_release.sh) are maintained by hand.
This test compares their services and the environment variable names of each
service. It only parses YAML; it does not start containers or need Docker.

Build directives, image tags, volume paths and release-only values are expected to
differ and are not compared. Name-level differences must be listed in the
allowlists below with a reason. A listed entry that no longer differs fails the
test, so the allowlists cannot go stale.
"""

import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DOCKER_COMPOSE = ROOT / "docker-compose.yml"
DEV_COMPOSE = ROOT / "docker-compose.dev.yml"
STANDALONE_COMPOSE = ROOT / "compose.standalone.yml"
RELEASE_COMPOSE = ROOT / "release/standalone/docker-compose.yml"
ENV_EXAMPLE = ROOT / ".env.example"
RELEASE_ENV_EXAMPLE = ROOT / "release/standalone/.env.example"

# (service, variable name) -> reason. Variables present only in compose.standalone.yml.
# Empty: both files currently expose the same variable names in every service.
INTENTIONAL_ENV_ONLY_IN_STANDALONE: dict[tuple[str, str], str] = {}

# (service, variable name) -> reason. Variables present only in the release bundle.
# Empty: both files currently expose the same variable names in every service.
INTENTIONAL_ENV_ONLY_IN_RELEASE: dict[tuple[str, str], str] = {}

# Services present in one file only. Empty: both files define the same services.
INTENTIONAL_SERVICE_ONLY_IN_STANDALONE: dict[str, str] = {}
INTENTIONAL_SERVICE_ONLY_IN_RELEASE: dict[str, str] = {}


def _load_services(path: Path) -> dict:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not isinstance(document.get("services"), dict):
        raise AssertionError(f"{path.name} has no 'services' mapping")
    return document["services"]


def _parse_uncommented_env(path: Path) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, sep, value = stripped.partition("=")
        if sep:
            mapping[name.strip()] = value.strip()
    return mapping


def _normalize_env(service: dict) -> dict[str, str]:
    environment = service.get("environment") or {}
    if isinstance(environment, dict):
        return {str(name): "" if value is None else str(value) for name, value in environment.items()}
    mapping: dict[str, str] = {}
    for item in environment:
        name, sep, value = str(item).partition("=")
        mapping[name] = value if sep else ""
    return mapping


def _env_names(service: dict) -> set[str]:
    return set(_normalize_env(service))


class StandaloneComposeParityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.standalone = _load_services(STANDALONE_COMPOSE)
        cls.release = _load_services(RELEASE_COMPOSE)

    def test_service_sets_match_apart_from_allowlist(self) -> None:
        only_standalone = set(self.standalone) - set(self.release)
        only_release = set(self.release) - set(self.standalone)
        self.assertEqual(
            only_standalone,
            set(INTENTIONAL_SERVICE_ONLY_IN_STANDALONE),
            "Services only in compose.standalone.yml differ from the allowlist.",
        )
        self.assertEqual(
            only_release,
            set(INTENTIONAL_SERVICE_ONLY_IN_RELEASE),
            "Services only in release/standalone/docker-compose.yml differ from the allowlist.",
        )

    def test_environment_names_match_per_service_apart_from_allowlist(self) -> None:
        shared = set(self.standalone) & set(self.release)
        only_standalone: set[tuple[str, str]] = set()
        only_release: set[tuple[str, str]] = set()
        for name in sorted(shared):
            standalone_names = _env_names(self.standalone[name])
            release_names = _env_names(self.release[name])
            only_standalone.update((name, var) for var in standalone_names - release_names)
            only_release.update((name, var) for var in release_names - standalone_names)

        self.assertEqual(
            only_standalone,
            set(INTENTIONAL_ENV_ONLY_IN_STANDALONE),
            "Environment names only in compose.standalone.yml differ from the allowlist. "
            "Copy the variable to the release bundle or add a reason to the allowlist.",
        )
        self.assertEqual(
            only_release,
            set(INTENTIONAL_ENV_ONLY_IN_RELEASE),
            "Environment names only in release/standalone/docker-compose.yml differ from the allowlist. "
            "Copy the variable to compose.standalone.yml or add a reason to the allowlist.",
        )

    def test_resolver_desktop_api_environment_default_false(self) -> None:
        compose_files = [
            DOCKER_COMPOSE,
            DEV_COMPOSE,
            STANDALONE_COMPOSE,
            RELEASE_COMPOSE,
        ]
        target_key = "ADULT_HUB_ENABLE_DESKTOP_API"
        expected_value = "${ADULT_HUB_ENABLE_DESKTOP_API:-false}"
        for path in compose_files:
            with self.subTest(file=path.name):
                services = _load_services(path)
                self.assertIn("resolver", services, f"resolver service not found in {path.name}")
                env = _normalize_env(services["resolver"])
                self.assertIn(target_key, env, f"{target_key} missing from resolver in {path.name}")
                self.assertEqual(
                    env[target_key],
                    expected_value,
                    f"{target_key} in {path.name} does not map to {expected_value}",
                )

        env_files = [
            ENV_EXAMPLE,
            RELEASE_ENV_EXAMPLE,
        ]
        expected_env_value = "false"
        for path in env_files:
            rel_name = str(path.relative_to(ROOT))
            with self.subTest(env_file=rel_name):
                env = _parse_uncommented_env(path)
                self.assertIn(
                    target_key,
                    env,
                    f"Uncommented {target_key} missing from {rel_name}",
                )
                self.assertEqual(
                    env[target_key],
                    expected_env_value,
                    f"{target_key} in {rel_name} does not map to {expected_env_value}",
                )


if __name__ == "__main__":
    unittest.main()
