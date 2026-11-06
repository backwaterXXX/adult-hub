#!/usr/bin/env bash
# =============================================================================
# Adult Hub Standalone Release Bundle Packager
# =============================================================================
# Packages the standalone prebuilt Docker Compose distribution into
# downloadable .tar.gz and .zip archives for release distribution.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

VERSION="${1:-$(tr -d '[:space:]' < "${ROOT_DIR}/VERSION" 2>/dev/null || echo "0.4.14")}"
SOURCE_DIR="${ROOT_DIR}/release/standalone"
DIST_DIR="${ROOT_DIR}/dist"

if [ ! -d "${SOURCE_DIR}" ]; then
    echo "[ERROR] Release source directory not found: ${SOURCE_DIR}" >&2
    exit 1
fi

# Verify no build directives or repository mounts exist in distributable compose
if grep -Eq '^[[:space:]]*build:' "${SOURCE_DIR}/docker-compose.yml"; then
    echo "[ERROR] Distributable compose must NOT contain 'build:' directives." >&2
    exit 1
fi

if grep -Eq '(\.\./|\./scripts/|\./resolver/|\./sync/|\./common/)' "${SOURCE_DIR}/docker-compose.yml"; then
    echo "[ERROR] Distributable compose must NOT contain repository-only bind mounts." >&2
    exit 1
fi

mkdir -p "${DIST_DIR}"

TMP_STAGE="$(mktemp -d "${DIST_DIR}/.stage_XXXXXX")"
trap 'rm -rf -- "${TMP_STAGE}"' EXIT

BUNDLE_DIR="${TMP_STAGE}/adult-hub-standalone-v${VERSION}"
mkdir -p "${BUNDLE_DIR}"

# Copy release bundle files
cp "${SOURCE_DIR}/docker-compose.yml" "${BUNDLE_DIR}/docker-compose.yml"
cp "${SOURCE_DIR}/.env.example" "${BUNDLE_DIR}/.env.example"
cp "${SOURCE_DIR}/config.example.yaml" "${BUNDLE_DIR}/config.example.yaml"
cp "${SOURCE_DIR}/README.md" "${BUNDLE_DIR}/README.md"

# Align versions in copied bundle files if needed
sed -i "s/:0.4.14/:${VERSION}/g" "${BUNDLE_DIR}/docker-compose.yml"
sed -i "s/APP_VERSION=0.4.14/APP_VERSION=${VERSION}/g" "${BUNDLE_DIR}/.env.example"
sed -i "s/:0.4.14/:${VERSION}/g" "${BUNDLE_DIR}/.env.example"

# Set standard permissions: 0755 dirs, 0644 files
find "${BUNDLE_DIR}" -type d -exec chmod 0755 {} +
find "${BUNDLE_DIR}" -type f -exec chmod 0644 {} +

TAR_VERSIONED="${DIST_DIR}/adult-hub-standalone-v${VERSION}.tar.gz"
TAR_LATEST="${DIST_DIR}/adult-hub-standalone.tar.gz"
ZIP_VERSIONED="${DIST_DIR}/adult-hub-standalone-v${VERSION}.zip"
ZIP_LATEST="${DIST_DIR}/adult-hub-standalone.zip"

echo "==> Creating release bundle archives for version ${VERSION}..."

# Create tar.gz (extracts directly into current directory without nesting prefix)
tar -czf "${TAR_VERSIONED}" -C "${BUNDLE_DIR}" docker-compose.yml .env.example config.example.yaml README.md
cp -f "${TAR_VERSIONED}" "${TAR_LATEST}"

# Create zip archive if zip is available
if command -v zip >/dev/null 2>&1; then
    (cd "${BUNDLE_DIR}" && zip -q -9 "${ZIP_VERSIONED}" docker-compose.yml .env.example config.example.yaml README.md)
    cp -f "${ZIP_VERSIONED}" "${ZIP_LATEST}"
fi

echo "[SUCCESS] Packaged standalone release bundles in ${DIST_DIR}:"
ls -lh "${DIST_DIR}"/*.tar.gz 2>/dev/null || true
ls -lh "${DIST_DIR}"/*.zip 2>/dev/null || true
