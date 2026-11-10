#!/usr/bin/env bash
# =============================================================================
# Adult Hub Standalone Maintenance and Update Workflow (Stage 5)
# =============================================================================
set -euo pipefail

# Locate root directory and script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="${UPDATE_ROOT_DIR:-$(cd "${SCRIPT_DIR}/.." && pwd)}"

# Configuration defaults
DOCKER_BIN="${DOCKER_BIN:-docker}"
APP_VERSION="${APP_VERSION:-$(tr -d '[:space:]' < "${ROOT_DIR}/VERSION" 2>/dev/null || echo dev)}"
export APP_VERSION

COMPOSE_PROJECT_NAME="adult-hub-standalone"
COMPOSE_FILE="compose.standalone.yml"
STANDALONE_MARKER="ADULT_HUB_STANDALONE=true"

SKIP_DOCKER_CHECK=0

print_usage() {
    cat <<EOF
Usage: scripts/update.sh [OPTIONS]

Adult Hub Standalone safe maintenance and update workflow.

Options:
  -h, --help               Show this help message and exit
  --docker-bin PATH        Path to Docker binary (default: docker)
  --root-dir PATH          Target installation directory root (default: repo root)
  --skip-docker-check      Skip Docker daemon connectivity check (for testing)
EOF
}

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            print_usage
            exit 0
            ;;
        --docker-bin)
            DOCKER_BIN="$2"
            shift 2
            ;;
        --root-dir)
            ROOT_DIR="$2"
            shift 2
            ;;
        --skip-docker-check)
            SKIP_DOCKER_CHECK=1
            shift
            ;;
        *)
            echo "Unknown option: $1" >&2
            print_usage >&2
            exit 1
            ;;
    esac
done

cd "${ROOT_DIR}"

if [ -f "${ROOT_DIR}/VERSION" ]; then
    APP_VERSION="$(tr -d '[:space:]' < "${ROOT_DIR}/VERSION" 2>/dev/null || echo dev)"
else
    APP_VERSION="${APP_VERSION:-dev}"
fi
export APP_VERSION

# -----------------------------------------------------------------------------
# 1. Environment and safety verification
# -----------------------------------------------------------------------------
echo "==> Verifying deployment environment safety for update..."

if [ ! -f .env ]; then
    echo "" >&2
    echo "================================================================================" >&2
    echo "[ERROR] No .env file found in '${ROOT_DIR}'!" >&2
    echo "================================================================================" >&2
    echo "Adult Hub Standalone is not installed in this directory." >&2
    echo "Please run 'make setup' or 'scripts/setup.sh' to perform the initial installation." >&2
    echo "================================================================================" >&2
    exit 1
fi

if ! grep -Eq '^[[:space:]]*ADULT_HUB_STANDALONE=true' .env; then
    echo "" >&2
    echo "================================================================================" >&2
    echo "[ERROR] Production deployment detected! 'make update' is standalone-only." >&2
    echo "================================================================================" >&2
    echo "The '.env' file in '${ROOT_DIR}' lacks the '${STANDALONE_MARKER}' marker." >&2
    echo "" >&2
    echo "To protect production services, credentials, and data:" >&2
    echo "  - For production releases, use: make deploy" >&2
    echo "  - For standalone updates, run 'make update' within a standalone installation." >&2
    echo "================================================================================" >&2
    exit 1
fi

# Refuse if active production Compose project 'adult-hub' is running
if command -v "${DOCKER_BIN}" >/dev/null 2>&1; then
    if "${DOCKER_BIN}" ps --filter "label=com.docker.compose.project=adult-hub" -q 2>/dev/null | grep -q .; then
        echo "" >&2
        echo "================================================================================" >&2
        echo "[ERROR] Active production Compose project 'adult-hub' is currently running." >&2
        echo "================================================================================" >&2
        echo "Refusing update to protect running production containers." >&2
        echo "================================================================================" >&2
        exit 1
    fi
fi

if [ ! -f "${COMPOSE_FILE}" ]; then
    echo "[ERROR] Standalone Compose file '${COMPOSE_FILE}' not found in '${ROOT_DIR}'." >&2
    exit 1
fi

# -----------------------------------------------------------------------------
# 2. Verify Docker CLI, Compose v2, and daemon availability
# -----------------------------------------------------------------------------
echo "==> Verifying system prerequisites..."

if ! command -v "${DOCKER_BIN}" >/dev/null 2>&1; then
    echo "[ERROR] Docker CLI '${DOCKER_BIN}' is not installed or not in PATH." >&2
    exit 1
fi

if ! "${DOCKER_BIN}" compose version >/dev/null 2>&1; then
    echo "[ERROR] Docker Compose v2 is required ('docker compose' plugin)." >&2
    exit 1
fi

if [ "${SKIP_DOCKER_CHECK}" -ne 1 ]; then
    if ! "${DOCKER_BIN}" info >/dev/null 2>&1; then
        echo "[ERROR] Cannot connect to the Docker daemon. Is Docker running?" >&2
        exit 1
    fi
fi

# Validate Compose configuration syntax
if ! "${DOCKER_BIN}" compose --env-file .env -p "${COMPOSE_PROJECT_NAME}" -f "${COMPOSE_FILE}" config --quiet; then
    echo "[ERROR] Docker Compose configuration validation failed for ${COMPOSE_FILE}." >&2
    exit 1
fi

echo "================================================================================"
echo "Updating Adult Hub Standalone to version ${APP_VERSION}"
echo "================================================================================"

# -----------------------------------------------------------------------------
# 3. Graceful shutdown of sync service
# -----------------------------------------------------------------------------
# The sync child answers SIGTERM between channels or stages; the runner waits up to
# sync.shutdown_grace_seconds (180s). We grant 240s (-t 240) to match stop_grace_period: 4m.
SYNC_WAS_RUNNING=0
if "${DOCKER_BIN}" compose --env-file .env -p "${COMPOSE_PROJECT_NAME}" -f "${COMPOSE_FILE}" ps --status running -q sync 2>/dev/null | grep -q .; then
    SYNC_WAS_RUNNING=1
    echo "==> Gracefully stopping sync worker (waiting up to 4m for current channel to finish)..."
    if ! "${DOCKER_BIN}" compose --env-file .env -p "${COMPOSE_PROJECT_NAME}" -f "${COMPOSE_FILE}" stop -t 240 sync; then
        echo "[ERROR] Failed to stop sync worker gracefully." >&2
        exit 1
    fi
    echo "[SUCCESS] Sync worker stopped cleanly."
fi

# -----------------------------------------------------------------------------
# 4. Rebuild updated Compose application images (resolver and sync)
# -----------------------------------------------------------------------------
echo "==> Rebuilding application container images (version ${APP_VERSION})..."
if ! "${DOCKER_BIN}" compose --env-file .env -p "${COMPOSE_PROJECT_NAME}" -f "${COMPOSE_FILE}" build resolver sync; then
    echo "[ERROR] Container image build failed." >&2
    if [ "${SYNC_WAS_RUNNING}" -eq 1 ]; then
        echo "==> Restoring sync worker since build failed before migrations..."
        "${DOCKER_BIN}" compose --env-file .env -p "${COMPOSE_PROJECT_NAME}" -f "${COMPOSE_FILE}" up -d --no-build sync || true
    fi
    exit 1
fi
echo "[SUCCESS] Container images built successfully."

# -----------------------------------------------------------------------------
# 5. Ensure database is running and healthy
# -----------------------------------------------------------------------------
echo "==> Ensuring database service is running..."
"${DOCKER_BIN}" compose --env-file .env -p "${COMPOSE_PROJECT_NAME}" -f "${COMPOSE_FILE}" up -d database

db_ready=0
for attempt in $(seq 1 30); do
    if "${DOCKER_BIN}" compose --env-file .env -p "${COMPOSE_PROJECT_NAME}" -f "${COMPOSE_FILE}" ps database --format json 2>/dev/null | grep -qi '"healthy"'; then
        db_ready=1
        break
    fi
    sleep 1
done

if [ "${db_ready}" -ne 1 ]; then
    echo "[WARN] Database readiness timed out. Continuing with migration attempt..."
fi

# -----------------------------------------------------------------------------
# 6. Execute database schema migrations using the migration service
# -----------------------------------------------------------------------------
echo "==> Running database schema migrations..."
if ! "${DOCKER_BIN}" compose --env-file .env -p "${COMPOSE_PROJECT_NAME}" -f "${COMPOSE_FILE}" up migration; then
    echo "" >&2
    echo "================================================================================" >&2
    echo "[ERROR] Database schema migration failed during update!" >&2
    echo "================================================================================" >&2
    echo "Application services have NOT been replaced to prevent database inconsistency." >&2
    echo "Check database logs and migration output above." >&2
    echo "================================================================================" >&2
    exit 1
fi
echo "[SUCCESS] Database migration completed successfully."

# -----------------------------------------------------------------------------
# 7. Check Telegram authorization before starting worker/resolver
# -----------------------------------------------------------------------------
HOST_DATA_DIR="$(grep -E '^[[:space:]]*STANDALONE_DATA_DIR=' .env | cut -d= -f2- | tr -d '\"'\'' ' || true)"
HOST_DATA_DIR="${HOST_DATA_DIR:-./standalone/data}"
if [[ "${HOST_DATA_DIR}" != /* ]]; then
    HOST_DATA_DIR="${ROOT_DIR}/${HOST_DATA_DIR#./}"
fi

# Stop running resolver if active to prevent SQLite session database lock conflicts
if "${DOCKER_BIN}" compose --env-file .env -p "${COMPOSE_PROJECT_NAME}" -f "${COMPOSE_FILE}" ps --status running -q resolver 2>/dev/null | grep -q .; then
    echo "==> Gracefully stopping running resolver before Telegram authorization check..."
    if ! "${DOCKER_BIN}" compose --env-file .env -p "${COMPOSE_PROJECT_NAME}" -f "${COMPOSE_FILE}" stop -t 30 resolver; then
        echo "[ERROR] Failed to stop resolver gracefully." >&2
        exit 1
    fi
    echo "[SUCCESS] Resolver stopped cleanly."
fi

is_authed=0
if "${DOCKER_BIN}" compose --env-file .env -p "${COMPOSE_PROJECT_NAME}" -f "${COMPOSE_FILE}" run --rm --no-deps resolver python3 -m resolver.providers.telegram_auth check 2>/dev/null; then
    is_authed=1
fi

# Ensure core services (database and jellyfin) are running
"${DOCKER_BIN}" compose --env-file .env -p "${COMPOSE_PROJECT_NAME}" -f "${COMPOSE_FILE}" up -d database jellyfin

if [ "${is_authed}" -eq 1 ]; then
    echo "==> Telegram session authorized. Updating and starting resolver and sync..."
    if ! "${DOCKER_BIN}" compose --env-file .env -p "${COMPOSE_PROJECT_NAME}" -f "${COMPOSE_FILE}" up -d --no-build resolver sync; then
        echo "[ERROR] Failed to start updated application services." >&2
        exit 1
    fi
else
    echo "==> Telegram session not authorized. Starting resolver (sync remains withheld)..."
    if ! "${DOCKER_BIN}" compose --env-file .env -p "${COMPOSE_PROJECT_NAME}" -f "${COMPOSE_FILE}" up -d --no-build resolver; then
        echo "[ERROR] Failed to start updated resolver service." >&2
        exit 1
    fi
fi

# -----------------------------------------------------------------------------
# 8. Readiness verification
# -----------------------------------------------------------------------------
echo "==> Verifying resolver readiness..."
resolver_ready=0
for attempt in $(seq 1 30); do
    if "${DOCKER_BIN}" compose --env-file .env -p "${COMPOSE_PROJECT_NAME}" -f "${COMPOSE_FILE}" ps resolver --format json 2>/dev/null | grep -qi '"healthy"'; then
        resolver_ready=1
        break
    fi
    sleep 1
done

if [ "${resolver_ready}" -eq 1 ]; then
    echo "[SUCCESS] Resolver readiness verified."
else
    echo "[WARN] Resolver did not reach healthy state within the expected interval. Check 'docker compose logs resolver'."
fi

RESOLVER_BIND_IP="$(grep -E '^[[:space:]]*RESOLVER_BIND_IP=' .env | cut -d= -f2- | tr -d '[:space:]"' || echo "127.0.0.1")"
RESOLVER_PORT="$(grep -E '^[[:space:]]*RESOLVER_PORT=' .env | cut -d= -f2- | tr -d '[:space:]"' || echo "8090")"
STANDALONE_RESOLVER_PUBLIC_URL="$(grep -E '^[[:space:]]*STANDALONE_RESOLVER_PUBLIC_URL=' .env | cut -d= -f2- | tr -d '[:space:]"' || echo "http://${RESOLVER_BIND_IP}:${RESOLVER_PORT}")"

echo ""
echo "================================================================================"
echo "             Adult Hub Standalone Update Succeeded                              "
echo "================================================================================"
echo "Version:            ${APP_VERSION}"
echo "Admin UI:           ${STANDALONE_RESOLVER_PUBLIC_URL}/admin/"
if [ "${is_authed}" -eq 1 ]; then
    echo "Services:           resolver, sync, database, jellyfin running"
else
    echo "Services:           database, jellyfin, resolver running (sync withheld)"
    echo "Next Steps:         Run 'make setup' to authenticate Telegram"
fi
echo "================================================================================"
echo ""
