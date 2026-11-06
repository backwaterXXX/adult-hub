#!/usr/bin/env bash
# =============================================================================
# Adult Hub Standalone Setup Installer (Stage 2)
# =============================================================================
set -euo pipefail

# Locate repository root and files
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="${SETUP_ROOT_DIR:-$(cd "${SCRIPT_DIR}/.." && pwd)}"

# Configuration defaults
DOCKER_BIN="${DOCKER_BIN:-docker}"
APP_VERSION="${APP_VERSION:-$(tr -d '[:space:]' < "${ROOT_DIR}/VERSION" 2>/dev/null || echo dev)}"
export APP_VERSION

COMPOSE_PROJECT_NAME="adult-hub-standalone"
COMPOSE_FILE="compose.standalone.yml"
STANDALONE_MARKER="ADULT_HUB_STANDALONE=true"

# Options
NON_INTERACTIVE=0
STATUS_ONLY=0
BIND_MODE=""
SELECTED_BIND_IP=""
RESOLVER_PORT_OVERRIDE=""
JELLYFIN_PORT_OVERRIDE=""
PUBLIC_URL_OVERRIDE=""
SKIP_DOCKER_START=0
SKIP_DOCKER_CHECK=0

print_usage() {
    cat <<EOF
Usage: scripts/setup.sh [OPTIONS]

Adult Hub Standalone interactive and repeatable installer.

Options:
  -h, --help               Show this help message and exit
  -y, --non-interactive    Run non-interactively using defaults or environment
  --status                 Show readiness and status summary without changing configuration
  --bind-mode MODE         Network binding mode: 'loopback' (default) or 'lan'
  --bind-ip IP             Host IP address to bind (required if bind-mode is 'lan')
  --resolver-port PORT     Port for resolver API and admin (default: 8090)
  --jellyfin-port PORT     Port for Jellyfin UI and playback (default: 8096)
  --public-url URL         Client-facing resolver/playback URL for LAN playback
  --skip-docker-start      Generate config, .env, and runtime dirs without launching containers
  --docker-bin PATH        Path to Docker binary (default: docker)
  --root-dir PATH          Target installation directory root (default: repo root)
EOF
}

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            print_usage
            exit 0
            ;;
        -y|--non-interactive)
            NON_INTERACTIVE=1
            shift
            ;;
        --status)
            STATUS_ONLY=1
            shift
            ;;
        --bind-mode)
            BIND_MODE="$2"
            shift 2
            ;;
        --bind-ip)
            SELECTED_BIND_IP="$2"
            shift 2
            ;;
        --resolver-port)
            RESOLVER_PORT_OVERRIDE="$2"
            shift 2
            ;;
        --jellyfin-port)
            JELLYFIN_PORT_OVERRIDE="$2"
            shift 2
            ;;
        --public-url)
            PUBLIC_URL_OVERRIDE="$2"
            shift 2
            ;;
        --skip-docker-start)
            SKIP_DOCKER_START=1
            shift
            ;;
        --skip-docker-check)
            SKIP_DOCKER_CHECK=1
            shift
            ;;
        --docker-bin)
            DOCKER_BIN="$2"
            shift 2
            ;;
        --root-dir)
            ROOT_DIR="$2"
            shift 2
            ;;
        *)
            echo "Unknown option: $1" >&2
            print_usage >&2
            exit 1
            ;;
    esac
done

cd "${ROOT_DIR}"

# -----------------------------------------------------------------------------
# 1. Detect existing production deployment and safety checks
# -----------------------------------------------------------------------------
echo "==> Checking deployment environment safety..."

# Refuse if .env exists without the standalone marker
if [ -f .env ]; then
    if ! grep -Eq '^[[:space:]]*ADULT_HUB_STANDALONE=true' .env; then
        echo "" >&2
        echo "================================================================================" >&2
        echo "[ERROR] Existing production deployment detected!" >&2
        echo "================================================================================" >&2
        echo "A root '.env' file already exists in '${ROOT_DIR}' without the standalone" >&2
        echo "installer marker (${STANDALONE_MARKER})." >&2
        echo "" >&2
        echo "To protect your production database, credentials, media, and running containers," >&2
        echo "the standalone installer will not run in this checkout." >&2
        echo "" >&2
        echo "Options:" >&2
        echo "  - To install Adult Hub Standalone: clone the repository into a separate empty" >&2
        echo "    directory and run 'make setup' there." >&2
        echo "  - To update the existing production stack: run 'make deploy'." >&2
        echo "================================================================================" >&2
        exit 1
    fi
fi

# Refuse if active production Compose project 'adult-hub' is running
if command -v "${DOCKER_BIN}" >/dev/null 2>&1; then
    if "${DOCKER_BIN}" ps --filter "label=com.docker.compose.project=adult-hub" -q 2>/dev/null | grep -q .; then
        if [ ! -f .env ] || ! grep -Eq '^[[:space:]]*ADULT_HUB_STANDALONE=true' .env; then
            echo "" >&2
            echo "[ERROR] Active production Compose project 'adult-hub' is currently running." >&2
            echo "Refusing to run standalone setup in this checkout to protect production services." >&2
            exit 1
        fi
    fi
fi

# Check for existing standalone PostgreSQL volume with missing credentials
STANDALONE_VOLUME="adult-hub-standalone_postgres_data"
if command -v "${DOCKER_BIN}" >/dev/null 2>&1; then
    if "${DOCKER_BIN}" volume inspect "${STANDALONE_VOLUME}" >/dev/null 2>&1; then
        if [ ! -f .env ]; then
            echo "" >&2
            echo "================================================================================" >&2
            echo "[ERROR] Existing standalone database volume found with missing .env!" >&2
            echo "================================================================================" >&2
            echo "Docker volume '${STANDALONE_VOLUME}' exists, but '.env' is missing." >&2
            echo "PostgreSQL will reject newly generated passwords against an already initialized" >&2
            echo "database cluster." >&2
            echo "" >&2
            echo "Please restore your previous .env file to recover credentials, or delete" >&2
            echo "the volume if starting clean:" >&2
            echo "  ${DOCKER_BIN} volume rm ${STANDALONE_VOLUME}" >&2
            echo "================================================================================" >&2
            exit 1
        elif ! grep -Eq '^[[:space:]]*POSTGRES_PASSWORD=[^[:space:]]+' .env; then
            echo "" >&2
            echo "[ERROR] Standalone database volume '${STANDALONE_VOLUME}' exists, but POSTGRES_PASSWORD" >&2
            echo "is missing or empty in .env. Please restore POSTGRES_PASSWORD to match the volume." >&2
            exit 1
        fi
    fi
fi

print_summary() {
    echo ""
    echo "================================================================================"
    echo "                   Adult Hub Standalone Setup Summary                           "
    echo "================================================================================"
    echo "Deployment Mode:    Standalone Compose (${COMPOSE_PROJECT_NAME})"
    echo "Version:            ${APP_VERSION}"
    echo "Bind Address:       ${RESOLVER_BIND_IP}"
    echo "Resolver Port:      ${RESOLVER_PORT}"
    echo "Jellyfin Port:      ${JELLYFIN_PORT}"
    echo ""
    echo "Service Endpoints:"
    echo "  - Admin UI:       ${STANDALONE_RESOLVER_PUBLIC_URL}/admin/"
    echo "  - Jellyfin UI:    http://${JELLYFIN_BIND_IP}:${JELLYFIN_PORT}/"
    echo ""
    echo "Status:"
    echo "  - Core Services:  ${SERVICES_STATUS}"
    echo "  - Telegram Auth:  ${TG_AUTH_STATE}"
    local jf_api_key="${STANDALONE_JELLYFIN_API_KEY:-${JELLYFIN_API_KEY:-}}"
    if [ -z "${jf_api_key}" ] && [ -f .env ]; then
        jf_api_key="$(grep -E '^[[:space:]]*(STANDALONE_)?JELLYFIN_API_KEY=' .env | head -n1 | cut -d= -f2- | tr -d '[:space:]"' || true)"
    fi
    if [ -n "${jf_api_key}" ]; then
        echo "  - Jellyfin API:   ACTIVE"
    else
        echo "  - Jellyfin API:   INACTIVE (API key optional; basic playback available)"
    fi
    echo ""
    echo "Next Steps:"
    if [[ "${TG_AUTH_STATE}" == *"AUTHORIZED"* ]] && [[ "${TG_AUTH_STATE}" != *"NOT_AUTHORIZED"* ]] && [[ "${TG_AUTH_STATE}" != *"FAILED"* ]]; then
        echo "  1. Open Admin UI at ${STANDALONE_RESOLVER_PUBLIC_URL}/admin/#sources to add your first channel."
        echo "  2. Access Jellyfin UI at http://${JELLYFIN_BIND_IP}:${JELLYFIN_PORT}/ for media playback."
    else
        echo "  1. If Telegram credentials are not yet configured, set TELEGRAM_API_ID and"
        echo "     TELEGRAM_API_HASH in .env."
        echo "  2. Complete Telegram authentication by running 'make setup' interactively"
        echo "     (or place an authenticated session in ${STANDALONE_DATA_DIR}/telegram.session)."
        echo "  3. Open Admin UI at ${STANDALONE_RESOLVER_PUBLIC_URL}/admin/#sources to add your first channel."
        echo "  4. Access Jellyfin UI at http://${JELLYFIN_BIND_IP}:${JELLYFIN_PORT}/ for media playback."
    fi
    echo "================================================================================"
    echo ""
}

if [ "${STATUS_ONLY}" -eq 1 ]; then
    if [ ! -f .env ]; then
        echo "[NOTICE] Standalone deployment is not initialized yet in '${ROOT_DIR}'." >&2
        echo "         Run 'make setup' or 'scripts/setup.sh' to install Adult Hub." >&2
        exit 1
    fi
    RESOLVER_BIND_IP="$(grep -E '^[[:space:]]*RESOLVER_BIND_IP=' .env | cut -d= -f2- | tr -d '[:space:]"' || echo "127.0.0.1")"
    RESOLVER_PORT="$(grep -E '^[[:space:]]*RESOLVER_PORT=' .env | cut -d= -f2- | tr -d '[:space:]"' || echo "8090")"
    JELLYFIN_BIND_IP="$(grep -E '^[[:space:]]*JELLYFIN_BIND_IP=' .env | cut -d= -f2- | tr -d '[:space:]"' || echo "127.0.0.1")"
    JELLYFIN_PORT="$(grep -E '^[[:space:]]*JELLYFIN_PORT=' .env | cut -d= -f2- | tr -d '[:space:]"' || echo "8096")"
    STANDALONE_RESOLVER_PUBLIC_URL="$(grep -E '^[[:space:]]*STANDALONE_RESOLVER_PUBLIC_URL=' .env | cut -d= -f2- | tr -d '[:space:]"' || echo "http://${RESOLVER_BIND_IP}:${RESOLVER_PORT}")"
    STANDALONE_DATA_DIR="./standalone/data"
    STANDALONE_CONFIG_PATH="./standalone/config.yaml"

    SERVICES_STATUS="Not running / withheld"
    TG_AUTH_STATE="NOT_CONFIGURED"
    if command -v "${DOCKER_BIN}" >/dev/null 2>&1; then
        if "${DOCKER_BIN}" compose --env-file .env -p "${COMPOSE_PROJECT_NAME}" -f "${COMPOSE_FILE}" ps --status running -q resolver sync 2>/dev/null | grep -q .; then
            SERVICES_STATUS="Running"
        fi
        if [ -f "${STANDALONE_DATA_DIR}/telegram.session" ]; then
            if "${DOCKER_BIN}" compose --env-file .env -p "${COMPOSE_PROJECT_NAME}" -f "${COMPOSE_FILE}" run --rm --no-deps resolver python3 -m providers.telegram_auth check 2>/dev/null; then
                TG_AUTH_STATE="AUTHORIZED"
            else
                TG_AUTH_STATE="UNAUTHORIZED (Session check failed)"
            fi
        else
            TG_API_ID="$(grep -E '^[[:space:]]*TELEGRAM_API_ID=' .env | cut -d= -f2- | tr -d '[:space:]"' || true)"
            TG_API_HASH="$(grep -E '^[[:space:]]*TELEGRAM_API_HASH=' .env | cut -d= -f2- | tr -d '[:space:]"' || true)"
            if [ -n "${TG_API_ID}" ] && [ -n "${TG_API_HASH}" ]; then
                TG_AUTH_STATE="PENDING_INTERACTIVE_AUTH (Run 'make setup' to authenticate)"
            else
                TG_AUTH_STATE="UNCONFIGURED"
            fi
        fi
    fi
    print_summary
    exit 0
fi

# -----------------------------------------------------------------------------
# 2. Verify Docker CLI, Compose v2, daemon availability, and permissions
# -----------------------------------------------------------------------------
echo "==> Verifying system prerequisites..."

if ! command -v "${DOCKER_BIN}" >/dev/null 2>&1; then
    echo "[ERROR] Docker CLI '${DOCKER_BIN}' is not installed or not in PATH." >&2
    echo "Please install Docker: https://docs.docker.com/engine/install/" >&2
    exit 1
fi

if ! "${DOCKER_BIN}" compose version >/dev/null 2>&1; then
    echo "[ERROR] Docker Compose v2 is required ('docker compose' plugin)." >&2
    echo "Please install Docker Compose v2: https://docs.docker.com/compose/install/" >&2
    exit 1
fi

if [ "${SKIP_DOCKER_START}" -ne 1 ] && [ "${SKIP_DOCKER_CHECK}" -ne 1 ]; then
    if ! "${DOCKER_BIN}" info >/dev/null 2>&1; then
        echo "[ERROR] Cannot connect to the Docker daemon. Is Docker running?" >&2
        echo "Check 'systemctl status docker' or verify user permissions in the 'docker' group." >&2
        exit 1
    fi
fi

# Filesystem permissions check
if ! touch .setup_write_test 2>/dev/null; then
    echo "[ERROR] Working directory '${ROOT_DIR}' is not writable. Check filesystem permissions." >&2
    exit 1
fi
rm -f .setup_write_test

# -----------------------------------------------------------------------------
# 3. Network binding selection and port conflict detection
# -----------------------------------------------------------------------------
echo "==> Configuring network and ports..."

# Helper: check port availability using python socket
is_port_available() {
    local ip="$1"
    local port="$2"
    python3 -c "
import socket, sys, errno
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind(('$ip', int('$port')))
    sys.exit(0)
except OSError as e:
    if e.errno == errno.EADDRNOTAVAIL:
        s2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s2.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s2.bind(('0.0.0.0', int('$port')))
            sys.exit(0)
        except OSError as e2:
            if e2.errno == errno.EADDRINUSE:
                sys.exit(1)
            sys.exit(0)
        finally:
            s2.close()
    elif e.errno == errno.EADDRINUSE:
        sys.exit(1)
    sys.exit(0)
finally:
    s.close()
" 2>/dev/null
    return $?
}

# Helper: validate LAN playback URL
validate_lan_url() {
    local url="$1"
    python3 -c "
import sys, urllib.parse, ipaddress
url = sys.argv[1].strip()
parsed = urllib.parse.urlparse(url)
if parsed.scheme not in ('http', 'https'):
    sys.exit('URL must begin with http:// or https://')
if not parsed.hostname:
    sys.exit('URL must include a valid hostname or IP')
host = parsed.hostname.lower()
if host in ('localhost', '0.0.0.0', '::'):
    sys.exit(f'Host \"{host}\" is invalid for LAN playback (must not be localhost or wildcard)')
try:
    ip = ipaddress.ip_address(host)
    if ip.is_loopback:
        sys.exit(f'Loopback IP \"{host}\" cannot be reached by other devices on LAN')
    if ip.is_unspecified:
        sys.exit(f'Wildcard IP \"{host}\" is invalid for playback clients')
except ValueError:
    if host.endswith('.localhost') or host == 'localhost':
        sys.exit(f'Localhost domain \"{host}\" cannot be reached by other devices on LAN')
" "$url"
}

# If .env exists with standalone marker, preserve existing configuration
PRESERVING_EXISTING_ENV=0
if [ -f .env ] && grep -Eq '^[[:space:]]*ADULT_HUB_STANDALONE=true' .env; then
    PRESERVING_EXISTING_ENV=1
    echo "[INFO] Existing standalone .env detected. Preserving credentials and settings."
    RESOLVER_BIND_IP="$(grep -E '^[[:space:]]*RESOLVER_BIND_IP=' .env | cut -d= -f2- | tr -d '\"'\'' ' || true)"
    RESOLVER_PORT="$(grep -E '^[[:space:]]*RESOLVER_PORT=' .env | cut -d= -f2- | tr -d '\"'\'' ' || true)"
    JELLYFIN_BIND_IP="$(grep -E '^[[:space:]]*JELLYFIN_BIND_IP=' .env | cut -d= -f2- | tr -d '\"'\'' ' || true)"
    JELLYFIN_PORT="$(grep -E '^[[:space:]]*JELLYFIN_PORT=' .env | cut -d= -f2- | tr -d '\"'\'' ' || true)"
    STANDALONE_RESOLVER_PUBLIC_URL="$(grep -E '^[[:space:]]*STANDALONE_RESOLVER_PUBLIC_URL=' .env | cut -d= -f2- | tr -d '\"'\'' ' || true)"
fi

if [ "${PRESERVING_EXISTING_ENV}" -eq 0 ]; then
    # Mode selection: loopback vs LAN
    if [ -z "${BIND_MODE}" ]; then
        if [ "${NON_INTERACTIVE}" -eq 1 ] || [ ! -t 0 ]; then
            BIND_MODE="loopback"
        else
            echo ""
            echo "Select network binding mode for Adult Hub Standalone:"
            echo "  1) Host-only loopback (127.0.0.1) [Default - safe, local access only]"
            echo "  2) Local Network (LAN) [Accessible from devices on your home network]"
            read -rp "Enter choice [1]: " mode_choice
            case "${mode_choice}" in
                2|lan|LAN)
                    BIND_MODE="lan"
                    ;;
                *)
                    BIND_MODE="loopback"
                    ;;
            esac
        fi
    fi

    if [ "${BIND_MODE}" = "lan" ]; then
        # Detect host LAN IPs
        detected_ips=()
        if command -v ip >/dev/null 2>&1; then
            while IFS= read -r ip; do
                [[ -n "$ip" && "$ip" != 127.* ]] && detected_ips+=("$ip")
            done < <(ip -4 addr show scope global 2>/dev/null | awk '/inet / {print $2}' | cut -d/ -f1)
        elif command -v hostname >/dev/null 2>&1; then
            for ip in $(hostname -I 2>/dev/null); do
                [[ -n "$ip" && "$ip" != 127.* ]] && detected_ips+=("$ip")
            done
        fi

        if [ -n "${SELECTED_BIND_IP}" ]; then
            BIND_IP="${SELECTED_BIND_IP}"
        elif [ "${NON_INTERACTIVE}" -eq 1 ] || [ ! -t 0 ]; then
            if [ ${#detected_ips[@]} -gt 0 ]; then
                BIND_IP="${detected_ips[0]}"
            else
                echo "[ERROR] No LAN IP detected. Specify --bind-ip <IP> in non-interactive mode." >&2
                exit 1
            fi
        else
            echo ""
            echo "Detected LAN IP address(es):"
            for i in "${!detected_ips[@]}"; do
                echo "  $((i+1))) ${detected_ips[$i]}"
            done
            if [ ${#detected_ips[@]} -gt 0 ]; then
                default_ip="${detected_ips[0]}"
                read -rp "Enter LAN IP to bind [${default_ip}]: " user_ip
                BIND_IP="${user_ip:-${default_ip}}"
            else
                read -rp "Enter LAN IP to bind: " BIND_IP
            fi
        fi

        # Validate that BIND_IP is not loopback, wildcard, or empty
        if [[ -z "${BIND_IP}" || "${BIND_IP}" =~ ^127\. || "${BIND_IP}" == "0.0.0.0" || "${BIND_IP}" == "localhost" ]]; then
            echo "[ERROR] Invalid LAN IP address: '${BIND_IP}'. Must be a specific private host IP, never 0.0.0.0 or loopback." >&2
            exit 1
        fi

        RESOLVER_BIND_IP="${BIND_IP}"
        JELLYFIN_BIND_IP="${BIND_IP}"
    else
        RESOLVER_BIND_IP="127.0.0.1"
        JELLYFIN_BIND_IP="127.0.0.1"
        echo "[INFO] Host-only (loopback) mode selected. Services will bind to 127.0.0.1."
        echo "       They will NOT be accessible from other devices on the LAN."
    fi

    # Port selection and conflict detection for Resolver (default 8090)
    RESOLVER_PORT="${RESOLVER_PORT_OVERRIDE:-8090}"
    if ! is_port_available "${RESOLVER_BIND_IP}" "${RESOLVER_PORT}"; then
        alt_port=$((RESOLVER_PORT + 1))
        for _ in $(seq 1 50); do
            if is_port_available "${RESOLVER_BIND_IP}" "${alt_port}"; then
                break
            fi
            alt_port=$((alt_port + 1))
        done
        echo "[WARN] Port ${RESOLVER_PORT} is in use on ${RESOLVER_BIND_IP}."
        echo "       Suggested alternate port: ${alt_port}"
        if [ "${NON_INTERACTIVE}" -eq 1 ] || [ ! -t 0 ]; then
            RESOLVER_PORT="${alt_port}"
            echo "[INFO] Automatically selected alternate resolver port: ${RESOLVER_PORT}"
        else
            read -rp "Enter resolver port [${alt_port}]: " user_port
            RESOLVER_PORT="${user_port:-${alt_port}}"
        fi
    fi

    # Port selection and conflict detection for Jellyfin (default 8096)
    JELLYFIN_PORT="${JELLYFIN_PORT_OVERRIDE:-8096}"
    if ! is_port_available "${JELLYFIN_BIND_IP}" "${JELLYFIN_PORT}"; then
        alt_jf_port=$((JELLYFIN_PORT + 1))
        for _ in $(seq 1 50); do
            if is_port_available "${JELLYFIN_BIND_IP}" "${alt_jf_port}" && [ "${alt_jf_port}" -ne "${RESOLVER_PORT}" ]; then
                break
            fi
            alt_jf_port=$((alt_jf_port + 1))
        done
        echo "[WARN] Port ${JELLYFIN_PORT} is in use on ${JELLYFIN_BIND_IP}."
        echo "       Suggested alternate port: ${alt_jf_port}"
        if [ "${NON_INTERACTIVE}" -eq 1 ] || [ ! -t 0 ]; then
            JELLYFIN_PORT="${alt_jf_port}"
            echo "[INFO] Automatically selected alternate Jellyfin port: ${JELLYFIN_PORT}"
        else
            read -rp "Enter Jellyfin port [${alt_jf_port}]: " user_jf_port
            JELLYFIN_PORT="${user_jf_port:-${alt_jf_port}}"
        fi
    fi

    # Refuse port collision when overrides or defaults request the same host port
    if [ -n "${RESOLVER_PORT_OVERRIDE}" ] && [ -n "${JELLYFIN_PORT_OVERRIDE}" ] && [ "${RESOLVER_PORT_OVERRIDE}" -eq "${JELLYFIN_PORT_OVERRIDE}" ]; then
        echo "[ERROR] Port collision: Resolver and Jellyfin overrides request the same host port (${RESOLVER_PORT_OVERRIDE}). Specify distinct ports for each service." >&2
        exit 1
    fi
    if [ "${RESOLVER_PORT}" -eq "${JELLYFIN_PORT}" ]; then
        echo "[ERROR] Port collision: Resolver and Jellyfin cannot both bind to host port ${RESOLVER_PORT}. Specify distinct ports for each service." >&2
        exit 1
    fi

    # Client-facing playback URL
    if [ "${RESOLVER_BIND_IP}" = "127.0.0.1" ]; then
        STANDALONE_RESOLVER_PUBLIC_URL="http://127.0.0.1:${RESOLVER_PORT}"
    else
        DEFAULT_PUBLIC_URL="http://${RESOLVER_BIND_IP}:${RESOLVER_PORT}"
        if [ -n "${PUBLIC_URL_OVERRIDE}" ]; then
            if ! err_msg=$(validate_lan_url "${PUBLIC_URL_OVERRIDE}" 2>&1); then
                echo "[ERROR] Invalid --public-url '${PUBLIC_URL_OVERRIDE}': ${err_msg}" >&2
                exit 1
            fi
            STANDALONE_RESOLVER_PUBLIC_URL="${PUBLIC_URL_OVERRIDE}"
        elif [ "${NON_INTERACTIVE}" -eq 1 ] || [ ! -t 0 ]; then
            STANDALONE_RESOLVER_PUBLIC_URL="${DEFAULT_PUBLIC_URL}"
        else
            while true; do
                read -rp "Enter client-facing resolver/playback URL [${DEFAULT_PUBLIC_URL}]: " user_url
                url_candidate="${user_url:-${DEFAULT_PUBLIC_URL}}"
                if err_msg=$(validate_lan_url "${url_candidate}" 2>&1); then
                    STANDALONE_RESOLVER_PUBLIC_URL="${url_candidate}"
                    break
                else
                    echo "[ERROR] ${err_msg}. Please enter a reachable LAN URL." >&2
                fi
            done
        fi
    fi
fi

# Ensure sensible defaults if preserved values were partially empty
RESOLVER_BIND_IP="${RESOLVER_BIND_IP:-127.0.0.1}"
RESOLVER_PORT="${RESOLVER_PORT:-8090}"
JELLYFIN_BIND_IP="${JELLYFIN_BIND_IP:-127.0.0.1}"
JELLYFIN_PORT="${JELLYFIN_PORT:-8096}"
STANDALONE_RESOLVER_PUBLIC_URL="${STANDALONE_RESOLVER_PUBLIC_URL:-http://${RESOLVER_BIND_IP}:${RESOLVER_PORT}}"

# -----------------------------------------------------------------------------
# 4. Create isolated standalone runtime directories
# -----------------------------------------------------------------------------
echo "==> Setting up standalone runtime directories..."

STANDALONE_DATA_DIR="./standalone/data"
STANDALONE_CACHE_DIR="./standalone/cache"
STANDALONE_MEDIA_DIR="./standalone/media"
STANDALONE_CONFIG_PATH="./standalone/config.yaml"

mkdir -p "${STANDALONE_DATA_DIR}/backups"
mkdir -p "${STANDALONE_CACHE_DIR}"
mkdir -p "${STANDALONE_MEDIA_DIR}"
mkdir -p "$(dirname "${STANDALONE_CONFIG_PATH}")"

# Safe non-destructive permissions on newly created directories
chmod 750 "${STANDALONE_DATA_DIR}" "${STANDALONE_CACHE_DIR}" "${STANDALONE_MEDIA_DIR}" 2>/dev/null || true

# -----------------------------------------------------------------------------
# 5. Generate .env atomically when absent
# -----------------------------------------------------------------------------
if [ ! -f .env ]; then
    echo "==> Generating standalone .env configuration..."

    # Cryptographically secure secrets
    PG_PASSWORD="$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")"
    ADMIN_TOKEN="$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")"

    TMP_ENV=".env.tmp.$$"
    cat <<EOF > "${TMP_ENV}"
# =============================================================================
# Adult Hub Standalone Environment Configuration
# =============================================================================
ADULT_HUB_STANDALONE=true

# Database credentials and connection
POSTGRES_DB=adulthub
POSTGRES_USER=adulthub
POSTGRES_PASSWORD=${PG_PASSWORD}
STANDALONE_DATABASE_URL=postgresql://adulthub:${PG_PASSWORD}@database:5432/adulthub

# Isolated standalone storage directories
STANDALONE_CONFIG_PATH=${STANDALONE_CONFIG_PATH}
STANDALONE_DATA_DIR=${STANDALONE_DATA_DIR}
STANDALONE_CACHE_DIR=${STANDALONE_CACHE_DIR}
STANDALONE_MEDIA_DIR=${STANDALONE_MEDIA_DIR}

# Network bindings and ports
RESOLVER_BIND_IP=${RESOLVER_BIND_IP}
RESOLVER_PORT=${RESOLVER_PORT}
JELLYFIN_BIND_IP=${JELLYFIN_BIND_IP}
JELLYFIN_PORT=${JELLYFIN_PORT}

# Public URLs and access control
STANDALONE_RESOLVER_PUBLIC_URL=${STANDALONE_RESOLVER_PUBLIC_URL}
STANDALONE_JELLYFIN_URL=http://jellyfin:8096
STANDALONE_JELLYFIN_API_KEY=
STANDALONE_ADMIN_PUBLIC=false
ADMIN_WRITE_TOKEN=${ADMIN_TOKEN}

# Telegram credentials (configure at setup or via Stage 3)
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
TELEGRAM_SESSION_PATH=/data/telegram.session
EOF
    chmod 0600 "${TMP_ENV}" 2>/dev/null || true
    mv -f "${TMP_ENV}" .env
    echo "[SUCCESS] Standalone .env created with secure secrets."
else
    echo "[INFO] Standalone .env already exists. Secrets and configuration preserved."
fi

# -----------------------------------------------------------------------------
# 6. Generate standalone config.yaml when absent
# -----------------------------------------------------------------------------
if [ ! -f "${STANDALONE_CONFIG_PATH}" ]; then
    echo "==> Generating initial standalone configuration at ${STANDALONE_CONFIG_PATH}..."
    cat <<'EOF' > "${STANDALONE_CONFIG_PATH}"
# =============================================================================
# Adult Hub Standalone Configuration
# =============================================================================

sync:
  interval_minutes: 60
  daily_at: "03:30"
  run_on_startup: true
  retention_days: 30
  retry_backoff_base_seconds: 30
  retry_backoff_max_seconds: 1800
  shutdown_grace_seconds: 180
  stream_mode_default: proxy
  category_fetch_delay_seconds: 2.5
  pbc_backfill_per_run: 50
  tpdb_backfill_per_run: 50
  scene_backfill_per_run: 500
  jellyfin_people_per_run: 25

sources:
  - provider: telegram
    enabled: true
    defaults:
      stream_mode: proxy
      max_pages: 1
      min_duration_seconds: 420
      history_batch_size: 100
      tags_whitelist: []
      tags_blacklist: []
    channels: []
EOF
    echo "[SUCCESS] Generated ${STANDALONE_CONFIG_PATH} with empty channels list."
else
    echo "==> Validating existing configuration ${STANDALONE_CONFIG_PATH}..."
    if ! python3 -c "
import yaml, sys
from common.config_loader import parse_and_validate_config
try:
    with open('${STANDALONE_CONFIG_PATH}', 'r', encoding='utf-8') as f:
        data = yaml.safe_load(f)
    parse_and_validate_config(data)
except Exception as e:
    sys.exit(str(e))
" 2>&1; then
        echo "[ERROR] Existing config ${STANDALONE_CONFIG_PATH} is incompatible with Adult Hub schema." >&2
        exit 1
    fi
    echo "[INFO] Existing configuration is valid and preserved."
fi

# -----------------------------------------------------------------------------
# 7. Validate Compose configuration
# -----------------------------------------------------------------------------
echo "==> Validating Compose standalone configuration..."
if ! "${DOCKER_BIN}" compose --env-file .env -p "${COMPOSE_PROJECT_NAME}" -f "${COMPOSE_FILE}" config --quiet; then
    echo "[ERROR] Docker Compose configuration validation failed for ${COMPOSE_FILE}." >&2
    exit 1
fi
echo "[SUCCESS] Compose standalone configuration is valid."

# -----------------------------------------------------------------------------
# 8. Service orchestration and Telegram authorization check
# -----------------------------------------------------------------------------
TG_AUTH_STATE="NOT_CONFIGURED"
SERVICES_STATUS="Database and Jellyfin ready"

if [ "${SKIP_DOCKER_START}" -eq 1 ]; then
    echo "[INFO] --skip-docker-start specified. Skipping container startup."
    SERVICES_STATUS="Skipped (--skip-docker-start)"
    TG_AUTH_STATE="Pending"
else
    echo "==> Starting core database and Jellyfin services..."
    "${DOCKER_BIN}" compose --env-file .env -p "${COMPOSE_PROJECT_NAME}" -f "${COMPOSE_FILE}" up -d database jellyfin

    echo "==> Waiting for database readiness..."
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

    echo "==> Running database schema migrations..."
    if ! "${DOCKER_BIN}" compose --env-file .env -p "${COMPOSE_PROJECT_NAME}" -f "${COMPOSE_FILE}" up migration; then
        echo "[ERROR] Database schema migration failed." >&2
        exit 1
    fi
    echo "[SUCCESS] Database migration completed successfully."

    # Telegram authentication inspection and first-run login
    TG_API_ID="$(grep -E '^[[:space:]]*TELEGRAM_API_ID=' .env | cut -d= -f2- | tr -d '\"'\'' ' || true)"
    TG_API_HASH="$(grep -E '^[[:space:]]*TELEGRAM_API_HASH=' .env | cut -d= -f2- | tr -d '\"'\'' ' || true)"
    HOST_DATA_DIR="$(grep -E '^[[:space:]]*STANDALONE_DATA_DIR=' .env | cut -d= -f2- | tr -d '\"'\'' ' || true)"
    HOST_DATA_DIR="${HOST_DATA_DIR:-./standalone/data}"
    if [[ "${HOST_DATA_DIR}" != /* ]]; then
        HOST_DATA_DIR="${ROOT_DIR}/${HOST_DATA_DIR#./}"
    fi
    TG_SESSION_FILE="${HOST_DATA_DIR}/telegram.session"

    # 1. Prompt for API credentials if missing and running interactively
    if [ -z "${TG_API_ID}" ] || [ -z "${TG_API_HASH}" ]; then
        if [ "${NON_INTERACTIVE}" -eq 0 ]; then
            echo ""
            echo "================================================================================"
            echo "                      Telegram API Configuration                                "
            echo "================================================================================"
            echo "Adult Hub connects to Telegram MTProto via your user account."
            echo "You can obtain your Telegram API ID and API Hash from: https://my.telegram.org"
            echo ""
            read -rp "Enter Telegram API ID: " input_api_id
            read -rp "Enter Telegram API Hash: " input_api_hash
            echo ""

            valid_creds=0
            if python3 -c "
import sys
try:
    aid = int(sys.argv[1].strip())
    ahash = sys.argv[2].strip()
    if aid <= 0 or len(ahash) < 16 or not all(c in '0123456789abcdefABCDEF' for c in ahash):
        sys.exit(1)
    sys.exit(0)
except Exception:
    sys.exit(1)
" "${input_api_id}" "${input_api_hash}" 2>/dev/null; then
                valid_creds=1
            fi

            if [ "${valid_creds}" -eq 1 ]; then
                python3 -c "
import sys
fpath, aid, ahash = sys.argv[1], sys.argv[2], sys.argv[3]
with open(fpath, 'r', encoding='utf-8') as f:
    lines = f.readlines()
new_lines = []
for line in lines:
    if line.strip().startswith('TELEGRAM_API_ID='):
        new_lines.append(f'TELEGRAM_API_ID={aid}\n')
    elif line.strip().startswith('TELEGRAM_API_HASH='):
        new_lines.append(f'TELEGRAM_API_HASH={ahash}\n')
    else:
        new_lines.append(line)
with open(fpath, 'w', encoding='utf-8') as f:
    f.writelines(new_lines)
" .env "${input_api_id}" "${input_api_hash}"
                chmod 0600 .env 2>/dev/null || true
                TG_API_ID="${input_api_id}"
                TG_API_HASH="${input_api_hash}"
                echo "[SUCCESS] Telegram API credentials saved to .env."
            else
                echo "[ERROR] Invalid Telegram API credentials. API ID must be a positive integer and API Hash a valid hex string." >&2
                TG_AUTH_STATE="FAILED_CREDENTIAL_VALIDATION"
            fi
        fi
    fi

    # 2. Check if credentials are ready
    if [ -z "${TG_API_ID}" ] || [ -z "${TG_API_HASH}" ]; then
        if [ "${TG_AUTH_STATE}" = "NOT_CONFIGURED" ]; then
            TG_AUTH_STATE="UNCONFIGURED (TELEGRAM_API_ID / TELEGRAM_API_HASH missing in .env)"
        fi
        echo ""
        echo "[NOTICE] Telegram credentials (TELEGRAM_API_ID / TELEGRAM_API_HASH) are not configured."
        echo "         Services 'resolver', 'sync', and 'backup' are withheld until Telegram is authorized."
    else
        # 3. Prevent concurrent session process access (Requirement 5)
        has_concurrent=0
        if "${DOCKER_BIN}" compose --env-file .env -p "${COMPOSE_PROJECT_NAME}" -f "${COMPOSE_FILE}" ps --status running -q resolver sync 2>/dev/null | grep -q .; then
            has_concurrent=1
        fi

        if [ "${has_concurrent}" -eq 1 ]; then
            echo "" >&2
            echo "================================================================================" >&2
            echo "[ERROR] Cannot authenticate while resolver or sync service is running!" >&2
            echo "================================================================================" >&2
            echo "Active resolver or sync containers detected in project '${COMPOSE_PROJECT_NAME}'." >&2
            echo "To prevent SQLite session database lock conflicts, stop running services before" >&2
            echo "attempting authentication." >&2
            echo "================================================================================" >&2
            TG_AUTH_STATE="BLOCKED (Concurrent resolver/sync process running)"
        else
            # 4. Check if session is already authorized
            is_authed=0
            if "${DOCKER_BIN}" compose --env-file .env -p "${COMPOSE_PROJECT_NAME}" -f "${COMPOSE_FILE}" run --rm --no-deps resolver python3 -m providers.telegram_auth check 2>/dev/null; then
                is_authed=1
            fi

            if [ "${is_authed}" -eq 1 ]; then
                TG_AUTH_STATE="AUTHORIZED"
                echo "==> Telegram session authorized. Starting resolver, sync, and backup..."
                "${DOCKER_BIN}" compose --env-file .env -p "${COMPOSE_PROJECT_NAME}" -f "${COMPOSE_FILE}" up -d resolver sync backup
                SERVICES_STATUS="All services up and running"
            elif [ -f "${TG_SESSION_FILE}" ]; then
                # Existing session file exists, but failed authorization!
                # Requirement 6: never delete or overwrite existing session file.
                echo "" >&2
                echo "================================================================================" >&2
                echo "[ERROR] Existing Telegram session file is present but not authorized!" >&2
                echo "================================================================================" >&2
                echo "Session file '${TG_SESSION_FILE}' exists, but authorization check failed." >&2
                echo "To protect existing session data, setup will never delete or overwrite this file." >&2
                echo "" >&2
                echo "Please back up or remove '${TG_SESSION_FILE}' manually before re-authenticating." >&2
                echo "================================================================================" >&2
                TG_AUTH_STATE="FAILED (Existing session file unauthorized; preserved)"
            elif [ "${NON_INTERACTIVE}" -eq 1 ]; then
                # Non-interactive mode: cannot prompt for SMS/2FA
                TG_AUTH_STATE="PENDING_INTERACTIVE_AUTH (Run 'make setup' interactively to authenticate)"
                echo ""
                echo "[NOTICE] Telegram credentials are set, but interactive authentication is required."
                echo "         Run 'make setup' interactively to complete login with SMS/2FA code."
                echo "         Services 'resolver', 'sync', and 'backup' are withheld until authorized."
            else
                # 5. Interactive first-run authentication
                echo ""
                echo "==> Starting first-run Telegram authentication..."
                auth_ok=0
                DOCKER_RUN_TTY="-i"
                if [ -t 0 ]; then
                    DOCKER_RUN_TTY="-it"
                fi
                if "${DOCKER_BIN}" compose --env-file .env -p "${COMPOSE_PROJECT_NAME}" -f "${COMPOSE_FILE}" run --rm ${DOCKER_RUN_TTY} --no-deps resolver python3 -m providers.telegram_auth auth; then
                    auth_ok=1
                fi

                if [ "${auth_ok}" -eq 1 ]; then
                    TG_AUTH_STATE="AUTHORIZED"
                    echo "==> Telegram authentication completed successfully. Starting resolver, sync, and backup..."
                    "${DOCKER_BIN}" compose --env-file .env -p "${COMPOSE_PROJECT_NAME}" -f "${COMPOSE_FILE}" up -d resolver sync backup
                    SERVICES_STATUS="All services up and running"
                else
                    TG_AUTH_STATE="NOT_AUTHORIZED (Authentication failed or cancelled)"
                    echo "" >&2
                    echo "[ERROR] Telegram authentication failed or was cancelled." >&2
                    echo "        Services 'resolver', 'sync', and 'backup' remain withheld." >&2
                fi
            fi
        fi
    fi
fi

# -----------------------------------------------------------------------------
# 9. Final Summary Display (never print secrets)
# -----------------------------------------------------------------------------
print_summary
