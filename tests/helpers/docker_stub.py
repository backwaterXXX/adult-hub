"""Hermetic Docker CLI stub for installer tests.

scripts/setup.sh refuses to run while containers labelled with the production
Compose project 'adult-hub' are running. Installer tests point DOCKER_BIN at the
stub written here so their outcome does not depend on the host's Docker state.

The stub never starts, stops, creates or removes anything:
- `ps` reports either no containers or one fake running container.
- `info` succeeds, `volume inspect` reports that no volume exists.
- `compose version` reports a Compose v2 version and `compose ... ps` reports no services.
- `compose ... config` (read-only validation) is passed to the real Docker CLI.
- Every other command exits 1 with a message on stderr.
"""

import shlex
import shutil
from pathlib import Path

_TEMPLATE = """#!/usr/bin/env bash
# Test stub for the Docker CLI. Never changes containers.
REAL_DOCKER=__REAL_DOCKER__

case "$1" in
    ps)
        __PS_BODY__
        exit 0
        ;;
    info)
        exit 0
        ;;
    volume)
        # No standalone PostgreSQL volume exists in test checkouts.
        exit 1
        ;;
    compose)
        case "$*" in
            *" config"*)
                exec "$REAL_DOCKER" "$@"
                ;;
            *" version"*)
                echo "Docker Compose version v2.29.2"
                exit 0
                ;;
            *" ps "*|*" ps")
                exit 0
                ;;
        esac
        echo "docker stub: refusing 'compose $*'" >&2
        exit 1
        ;;
    *)
        echo "docker stub: refusing '$*'" >&2
        exit 1
        ;;
esac
"""


def make_docker_stub(directory: Path, *, running_container: bool = False) -> str:
    """Write an executable Docker CLI stub into ``directory`` and return its path.

    When ``running_container`` is true, ``docker ps`` reports one running container,
    which triggers the production Compose project guard in scripts/setup.sh.
    """
    real_docker = shutil.which("docker") or "docker"
    ps_body = "echo stub-running-container" if running_container else ":"
    script = _TEMPLATE.replace("__REAL_DOCKER__", shlex.quote(real_docker)).replace(
        "__PS_BODY__", ps_body
    )
    stub = Path(directory) / "docker_stub.sh"
    stub.write_text(script, encoding="utf-8")
    stub.chmod(0o755)
    return str(stub)
