#!/usr/bin/env bash
# Enable Mullvad gluetun (compose profile vpn) for ABB-only proxying.
#
# Usage (from install root, e.g. /opt/library):
#   MULLVAD_ACCOUNT=1234567890123456 bash scripts/enable_vpn.sh
#   bash scripts/enable_vpn.sh --account 1234567890123456
#   bash scripts/enable_vpn.sh --skip-register   # keys already in data/mullvad.env
#
# What it does:
#   1) Registers a WireGuard device with Mullvad (unless --skip-register)
#   2) Writes data/mullvad.env + WIREGUARD_* / ABB_PROXY_URL / vpn profile into .env
#   3) Starts gluetun and verifies https://am.i.mullvad.net via the proxy

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

ACCOUNT="${MULLVAD_ACCOUNT:-}"
SKIP_REGISTER=0
COUNTRY="${MULLVAD_COUNTRY:-USA}"
PROXY_URL="http://gluetun:8888"

c_green() { printf '\033[32m%s\033[0m\n' "$*"; }
c_yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
c_red() { printf '\033[31m%s\033[0m\n' "$*"; }

usage() {
  sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --account)
      ACCOUNT="${2:-}"
      shift 2
      ;;
    --skip-register) SKIP_REGISTER=1; shift ;;
    --country)
      COUNTRY="${2:-USA}"
      shift 2
      ;;
    -h|--help) usage 0 ;;
    *)
      c_red "Unknown option: $1"
      usage 1
      ;;
  esac
done

# Prefer host install path so compose bind mounts resolve on the Docker daemon
# (UI/setup sidecar binds the checkout at /library and sets LIBRARY_HOST_ROOT_BIND).
compose() {
  if [[ -n "${LIBRARY_HOST_ROOT_BIND:-}" ]]; then
    docker compose --project-directory "$LIBRARY_HOST_ROOT_BIND" "$@"
  else
    docker compose "$@"
  fi
}

set_env() {
  local key="$1" val="$2"
  python3 - "$key" "$val" <<'PY'
import pathlib, sys
key, val = sys.argv[1], sys.argv[2]
p = pathlib.Path(".env")
text = p.read_text(encoding="utf-8") if p.exists() else ""
lines = text.splitlines()
out, seen = [], False
for line in lines:
    if line.startswith(key + "="):
        out.append(f"{key}={val}")
        seen = True
    else:
        out.append(line)
if not seen:
    out.append(f"{key}={val}")
p.write_text("\n".join(out) + ("\n" if out else ""), encoding="utf-8")
PY
}

get_env() {
  local key="$1"
  python3 - "$key" <<'PY'
import pathlib, sys
key = sys.argv[1]
p = pathlib.Path(".env")
if not p.exists():
    raise SystemExit
for line in p.read_text(encoding="utf-8").splitlines():
    if line.startswith(key + "="):
        print(line.split("=", 1)[1].strip().strip('"').strip("'"))
        break
PY
}

merge_profiles() {
  python3 - <<'PY'
import pathlib, re
p = pathlib.Path(".env")
text = p.read_text(encoding="utf-8") if p.exists() else ""
cur = ""
for line in text.splitlines():
    if line.startswith("COMPOSE_PROFILES="):
        cur = line.split("=", 1)[1].strip().strip('"').strip("'")
        break
parts = [x.strip() for x in re.split(r"[,\s]+", cur) if x.strip()]
if "vpn" not in parts:
    parts.append("vpn")
seen, out = set(), []
for x in parts:
    if x not in seen:
        seen.add(x)
        out.append(x)
print(",".join(out))
PY
}

if [[ ! -f docker-compose.yml ]]; then
  c_red "error: run from Library install root (docker-compose.yml missing)"
  exit 1
fi
if ! command -v docker >/dev/null; then
  c_red "error: docker not found"
  exit 1
fi
# gluetun mounts /dev/net/tun itself. The Admin/setup sidecar (docker:cli) does not
# see host TUN devices, so only hard-fail when we look like a bare-metal host shell.
if [[ ! -e /dev/net/tun ]]; then
  if [[ -f /.dockerenv ]] || [[ -n "${LIBRARY_HOST_ROOT_BIND:-}" ]]; then
    c_yellow "note: /dev/net/tun not visible here (sidecar) - gluetun will mount it on the host"
  else
    c_red "error: /dev/net/tun missing - enable TUN on this host"
    exit 1
  fi
fi

mkdir -p data/gluetun
PRIV=""
ADDR=""

read_kv() {
  local file="$1" key="$2"
  [[ -f "$file" ]] || return 0
  grep -E "^${key}=" "$file" | head -1 | cut -d= -f2-
}

if [[ "$SKIP_REGISTER" -eq 1 ]]; then
  PRIV="$(read_kv data/mullvad.env WIREGUARD_PRIVATE_KEY)"
  ADDR="$(read_kv data/mullvad.env WIREGUARD_ADDRESSES)"
  PRIV="${PRIV:-$(get_env WIREGUARD_PRIVATE_KEY)}"
  ADDR="${ADDR:-$(get_env WIREGUARD_ADDRESSES)}"
  if [[ -z "$PRIV" || -z "$ADDR" ]]; then
    c_red "error: --skip-register needs WIREGUARD_PRIVATE_KEY + WIREGUARD_ADDRESSES in data/mullvad.env or .env"
    exit 1
  fi
else
  ACCOUNT="$(printf '%s' "$ACCOUNT" | tr -cd '0-9')"
  if [[ ${#ACCOUNT} -ne 16 ]]; then
    c_red "error: pass a 16-digit Mullvad account (MULLVAD_ACCOUNT=... or --account ...)"
    exit 1
  fi
  c_green "[1/4] Registering WireGuard device with Mullvad..."
  if compose ps --status running --services 2>/dev/null | grep -qx app \
     || docker ps --format '{{.Names}}' | grep -qx audiobook-request; then
    OUT="$(
      docker exec -e "MULLVAD_ACCOUNT=$ACCOUNT" -w /app audiobook-request \
        python scripts/mullvad_register_wg.py
    )"
  else
    OUT="$(MULLVAD_ACCOUNT="$ACCOUNT" python3 scripts/mullvad_register_wg.py)"
  fi
  PRIV="$(printf '%s\n' "$OUT" | grep '^WIREGUARD_PRIVATE_KEY=' | head -1 | cut -d= -f2-)"
  ADDR="$(printf '%s\n' "$OUT" | grep '^WIREGUARD_ADDRESSES=' | head -1 | cut -d= -f2- | cut -d, -f1)"
  if [[ -z "$PRIV" || -z "$ADDR" ]]; then
    c_red "error: Mullvad registration did not return keys"
    printf '%s\n' "$OUT" >&2
    exit 1
  fi
  [[ "$ADDR" == */* ]] || ADDR="${ADDR}/32"
fi

c_green "[2/4] Writing data/mullvad.env + .env..."
umask 077
cat > data/mullvad.env <<EOF
WIREGUARD_PRIVATE_KEY=${PRIV}
WIREGUARD_ADDRESSES=${ADDR}
MULLVAD_ACCOUNT_NUMBER=${ACCOUNT:-}
EOF
chmod 600 data/mullvad.env

PROFILES="$(merge_profiles)"
set_env COMPOSE_PROFILES "$PROFILES"
set_env ABB_PROXY_URL "$PROXY_URL"
set_env WIREGUARD_PRIVATE_KEY "$PRIV"
set_env WIREGUARD_ADDRESSES "$ADDR"
set_env MULLVAD_COUNTRY "$COUNTRY"
if [[ -n "${ACCOUNT:-}" ]]; then
  set_env MULLVAD_ACCOUNT_NUMBER "$ACCOUNT"
fi
export COMPOSE_PROFILES="$PROFILES"

c_green "[3/4] Starting gluetun (profile vpn)..."
compose --profile vpn up -d gluetun

c_green "[4/4] Waiting for Mullvad exit IP..."
ok=0
for i in $(seq 1 36); do
  if docker exec audiobook-gluetun wget -qO- https://am.i.mullvad.net/json 2>/dev/null \
    | grep -q '"mullvad_exit_ip":true'; then
    ok=1
    break
  fi
  # Fallback: probe via HTTP proxy from the gluetun compose network
  if docker run --rm --network "$(docker inspect audiobook-gluetun -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}' | head -1)" \
      curlimages/curl:8.5.0 -fsS --proxy http://gluetun:8888 https://am.i.mullvad.net/json 2>/dev/null \
      | grep -q '"mullvad_exit_ip":true'; then
    ok=1
    break
  fi
  sleep 5
done

if [[ "$ok" -ne 1 ]]; then
  c_yellow "gluetun is up but Mullvad exit not confirmed yet - check: docker logs audiobook-gluetun"
  docker logs audiobook-gluetun --tail 40 || true
  exit 2
fi

c_green "VPN ready. ABB traffic -> ${PROXY_URL}"
c_yellow "Recreate app so it picks up ABB_PROXY_URL:"
echo "  docker compose up -d --force-recreate --no-deps app"
compose up -d --force-recreate --no-deps app || true
c_green "Done. COMPOSE_PROFILES=${PROFILES}"
