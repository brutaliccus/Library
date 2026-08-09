#!/usr/bin/env bash
# Install or update abs-agg as a sibling Docker stack and point LibraForge at it.
# Specialty metadata (Graphic Audio, Sound Booth Theater, Hardcover, ...).
#
# Usage (on Pi / Ubuntu host):
#   bash scripts/install_abs_agg.sh
#
# Env overrides:
#   ABS_AGG_STACK_DIR    default /opt/stacks/abs-agg
#   LIBRAFORGE_STACK_DIR default /opt/stacks/libraforge
#   LIBRARY_ENV          path to Library Site .env (for HARDCOVER_API_KEY)
#   ABS_AGG_LAN_IP       optional LAN IP to bind :3010 (in addition to 127.0.0.1)
#   BUNDLED_CONFIG_DIR   if set, also write abs-agg.json there (bundled-media)

set -euo pipefail

STACK_DIR="${ABS_AGG_STACK_DIR:-/opt/stacks/abs-agg}"
LF_STACK="${LIBRAFORGE_STACK_DIR:-/opt/stacks/libraforge}"
LF_NETWORK="${LIBRAFORGE_DOCKER_NETWORK:-libraforge_default}"
IMAGE="${ABS_AGG_IMAGE:-ghcr.io/vito0912/abs-agg:latest}"
LIBRARY_ENV="${LIBRARY_ENV:-}"
BUNDLED_CONFIG_DIR="${BUNDLED_CONFIG_DIR:-}"
ABS_AGG_LAN_IP="${ABS_AGG_LAN_IP:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [[ -z "${LIBRARY_ENV}" ]]; then
  for candidate in \
    "${REPO_ROOT}/.env" \
    "/opt/library/.env" \
    "${LIBRARY_HOST_ROOT:-}/.env"; do
    if [[ -n "${candidate}" && -f "${candidate}" ]]; then
      LIBRARY_ENV="${candidate}"
      break
    fi
  done
fi

echo "==> abs-agg install (stack: ${STACK_DIR})"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required but not found." >&2
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "docker compose plugin is required." >&2
  exit 1
fi

mkdir -p "${STACK_DIR}/data"

HARDCOVER_TOKEN=""
GOODREADS_API_KEY=""
if [[ -n "${LIBRARY_ENV}" && -f "${LIBRARY_ENV}" ]]; then
  HARDCOVER_TOKEN="$(grep -E '^(HARDCOVER_TOKEN|HARDCOVER_API_KEY)=' "${LIBRARY_ENV}" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '\r' || true)"
  GOODREADS_API_KEY="$(grep -E '^GOODREADS_API_KEY=' "${LIBRARY_ENV}" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '\r' || true)"
  HARDCOVER_TOKEN="${HARDCOVER_TOKEN#Bearer }"
  HARDCOVER_TOKEN="${HARDCOVER_TOKEN#bearer }"
fi

if [[ ! -f "${STACK_DIR}/.env" ]]; then
  echo "==> Creating ${STACK_DIR}/.env"
  cat >"${STACK_DIR}/.env" <<EOF
HARDCOVER_TOKEN=${HARDCOVER_TOKEN}
GOODREADS_API_KEY=${GOODREADS_API_KEY}
EOF
else
  echo "==> Keeping existing ${STACK_DIR}/.env (update HARDCOVER_TOKEN manually if needed)"
  if [[ -n "${HARDCOVER_TOKEN}" ]] && ! grep -qE '^HARDCOVER_TOKEN=.+' "${STACK_DIR}/.env" 2>/dev/null; then
    if grep -qE '^HARDCOVER_TOKEN=' "${STACK_DIR}/.env" 2>/dev/null; then
      sed -i "s|^HARDCOVER_TOKEN=.*|HARDCOVER_TOKEN=${HARDCOVER_TOKEN}|" "${STACK_DIR}/.env"
    else
      echo "HARDCOVER_TOKEN=${HARDCOVER_TOKEN}" >>"${STACK_DIR}/.env"
    fi
    echo "==> Seeded HARDCOVER_TOKEN from Library Site .env"
  fi
fi

PORTS_BLOCK='      - "127.0.0.1:3010:3000"'
if [[ -n "${ABS_AGG_LAN_IP}" ]]; then
  PORTS_BLOCK="${PORTS_BLOCK}
      - \"${ABS_AGG_LAN_IP}:3010:3000\""
fi

cat >"${STACK_DIR}/docker-compose.yml" <<EOF
# Managed by Library Site scripts/install_abs_agg.sh
services:
  abs-agg:
    image: ${IMAGE}
    container_name: abs-agg
    restart: unless-stopped
    ports:
${PORTS_BLOCK}
    environment:
      - NODE_ENV=production
      - HARDCOVER_TOKEN=\${HARDCOVER_TOKEN:-}
      - GOODREADS_API_KEY=\${GOODREADS_API_KEY:-}
    volumes:
      - ./data:/app/data
EOF

if ! docker network inspect "${LF_NETWORK}" >/dev/null 2>&1; then
  echo "==> Creating Docker network ${LF_NETWORK}"
  docker network create "${LF_NETWORK}" >/dev/null
fi

OVERRIDE="${STACK_DIR}/docker-compose.override.yml"
cat >"${OVERRIDE}" <<EOF
# Managed by Library Site scripts/install_abs_agg.sh
services:
  abs-agg:
    networks:
      default: {}
      libraforge_net:
        aliases:
          - abs-agg

networks:
  libraforge_net:
    external: true
    name: ${LF_NETWORK}
EOF

write_abs_agg_json() {
  local dest_dir="$1"
  local url="$2"
  mkdir -p "${dest_dir}"
  printf '%s\n' "{" "  \"url\": \"${url}\"" "}" >"${dest_dir}/abs-agg.json"
  echo "==> Wrote ${dest_dir}/abs-agg.json -> ${url}"
}

if [[ -d "${LF_STACK}" ]] || mkdir -p "${LF_STACK}/config" 2>/dev/null; then
  write_abs_agg_json "${LF_STACK}/config" "http://abs-agg:3000"
fi

if [[ -n "${BUNDLED_CONFIG_DIR}" ]]; then
  write_abs_agg_json "${BUNDLED_CONFIG_DIR}" "http://abs-agg:3000"
elif [[ -d "${REPO_ROOT}/libraforge-config" ]]; then
  write_abs_agg_json "${REPO_ROOT}/libraforge-config" "http://abs-agg:3000"
fi

echo "==> Starting abs-agg..."
cd "${STACK_DIR}"
docker compose up -d

OK=false
for _ in $(seq 1 30); do
  if curl -fsS --max-time 3 "http://127.0.0.1:3010/providers" >/dev/null 2>&1; then
    OK=true
    break
  fi
  sleep 1
done

if $OK; then
  echo ""
  echo "abs-agg is running on http://127.0.0.1:3010"
  echo "LibraForge should use http://abs-agg:3000 (config/abs-agg.json)."
  echo "Metadata chain: Audible -> Graphic Audio -> Sound Booth Theater -> other abs-agg providers."
else
  echo "WARN: abs-agg started but /providers did not respond yet - check: docker logs abs-agg" >&2
fi