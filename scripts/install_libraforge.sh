#!/usr/bin/env bash
# Install or update LibraForge as a sibling Docker stack on the Pi.
# Uses the same audiobook library mount as Audiobookshelf (/mnt/Audiobooks).
#
# Usage (on Pi):
#   bash scripts/install_libraforge.sh
#
# From Windows dev machine:
#   scp scripts/install_libraforge.sh pihole@192.168.68.76:/tmp/
#   ssh pihole@192.168.68.76 "bash /tmp/install_libraforge.sh"

set -euo pipefail

STACK_DIR="/opt/stacks/libraforge"
REPO_URL="${LIBRAFORGE_REPO_URL:-https://github.com/brutaliccus/LibraForge.git}"
FALLBACK_REPO_URL="https://github.com/coconautilus17/LibraForge.git"
AUDIOBOOKS_HOST="/mnt/Audiobooks"

echo "==> LibraForge install (stack: ${STACK_DIR})"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required but not found." >&2
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "docker compose plugin is required." >&2
  exit 1
fi

mkdir -p "${STACK_DIR}"

clone_or_update() {
  local url="$1"
  if [[ ! -d "${STACK_DIR}/.git" ]]; then
    echo "==> Cloning LibraForge from ${url}..."
    # Allow cloning into a pre-created empty dir
    git clone "${url}" "${STACK_DIR}"
  else
    echo "==> Updating LibraForge (${url})..."
    git -C "${STACK_DIR}" remote set-url origin "${url}"
    git -C "${STACK_DIR}" pull --ff-only
  fi
}

stack_looks_valid() {
  [[ -f "${STACK_DIR}/docker-compose.yml" ]] || [[ -f "${STACK_DIR}/compose.yml" ]]
}

if ! clone_or_update "${REPO_URL}"; then
  echo "WARN: primary clone/update failed; trying upstream ${FALLBACK_REPO_URL}" >&2
  rm -rf "${STACK_DIR}"
  mkdir -p "${STACK_DIR}"
  clone_or_update "${FALLBACK_REPO_URL}"
elif ! stack_looks_valid; then
  echo "WARN: ${REPO_URL} looks empty/broken; falling back to ${FALLBACK_REPO_URL}" >&2
  rm -rf "${STACK_DIR}"
  mkdir -p "${STACK_DIR}"
  clone_or_update "${FALLBACK_REPO_URL}"
fi

if ! stack_looks_valid; then
  echo "ERROR: LibraForge clone has no docker-compose.yml" >&2
  exit 1
fi

# Library container probes via Docker bridge gateway. Upstream binds
# 127.0.0.1:5056 only, which containers cannot reach — add bridge bind.
OVERRIDE="${STACK_DIR}/docker-compose.override.yml"
if [[ ! -f "${OVERRIDE}" ]]; then
  echo "==> Writing ${OVERRIDE} (127.0.0.1 + 172.17.0.1 :5056)"
  cat >"${OVERRIDE}" <<'EOF'
# Managed by Library Site scripts/install_libraforge.sh
# Keep localhost bind for NPM/local; add Docker bridge for Library health probe.
services:
  libraforge:
    ports:
      - "127.0.0.1:5056:5056"
      - "172.17.0.1:5056:5056"
EOF
else
  echo "==> Keeping existing ${OVERRIDE}"
fi

if [[ ! -f "${STACK_DIR}/.env" ]]; then
  echo "==> Creating ${STACK_DIR}/.env"
  cat >"${STACK_DIR}/.env" <<EOF
UID=1000
GID=1000
AUDIOBOOKS_PATH=${AUDIOBOOKS_HOST}
AUDIBLE_AUTH_PATH=${STACK_DIR}/audible-auth
EOF
else
  echo "==> Keeping existing ${STACK_DIR}/.env"
fi

mkdir -p "${STACK_DIR}/audible-auth"
mkdir -p "${STACK_DIR}/reports"

# Staging folder for messy imports (Metadata/Folder Forge source).
if [[ -d "${AUDIOBOOKS_HOST}" ]]; then
  mkdir -p "${AUDIOBOOKS_HOST}/_unorganized"
  echo "==> Staging folder: ${AUDIOBOOKS_HOST}/_unorganized"
else
  echo "WARN: ${AUDIOBOOKS_HOST} not found — create it before using LibraForge." >&2
fi

echo "==> Building and starting container (first build can take several minutes on ARM)..."
cd "${STACK_DIR}"
docker compose up -d --build

echo ""
echo "LibraForge is running on http://127.0.0.1:5056"
echo ""
echo "Next steps:"
echo "  1. Add Audible auth: ${STACK_DIR}/audible-auth/audible-metadata.json"
echo "     (see docs/libraforge.md in the Library Site repo)"
echo "  2. Expose via Nginx Proxy Manager: forge.library.freiverse.com -> http://127.0.0.1:5056"
echo "     Restrict access (VPN / IP allowlist / access list) — LibraForge can modify files."
echo "  3. Set Library Site .env: LIBRAFORGE_URL + LIBRAFORGE_INTERNAL_URL, then redeploy Library."
echo "  4. In LibraForge UI, set source to /audiobooks/_unorganized and dest to /audiobooks"
echo "  5. Always dry-run before Apply; then trigger ABS library scan from Library Admin → Health."
