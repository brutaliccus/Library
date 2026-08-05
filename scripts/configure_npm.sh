#!/usr/bin/env bash
# Bootstrap Nginx Proxy Manager via its REST API (idempotent).
# Creates/updates proxy hosts for Library (+ optional ABS/Kavita) and optional Let's Encrypt.
#
# Usage:
#   bash scripts/configure_npm.sh
#   LIBRARY_ENV_FILE=/path/to/.env bash scripts/configure_npm.sh
#
# Reads from .env:
#   NPM_ADMIN_EMAIL, NPM_ADMIN_PASSWORD, NPM_DOMAIN, NPM_ABS_DOMAIN, NPM_KAVITA_DOMAIN,
#   NPM_LETSENCRYPT_EMAIL, NPM_BASE_URL (default http://127.0.0.1:81)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${LIBRARY_ENV_FILE:-$ROOT/.env}"
NPM_BASE="${NPM_BASE_URL:-http://127.0.0.1:81}"
WAIT_SECONDS="${NPM_WAIT_SECONDS:-120}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "skip npm configure (no .env)"
  exit 0
fi

set_env() {
  local key="$1" value="$2"
  if grep -qE "^${key}=" "$ENV_FILE" 2>/dev/null; then
    local esc
    esc=$(printf '%s' "$value" | sed -e 's/[&|\\]/\\&/g')
    if sed --version >/dev/null 2>&1; then
      sed -i "s|^${key}=.*|${key}=${esc}|" "$ENV_FILE"
    else
      sed -i '' "s|^${key}=.*|${key}=${esc}|" "$ENV_FILE"
    fi
  else
    printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
}

get_env() {
  local key="$1"
  grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- || true
}

if ! command -v python3 >/dev/null 2>&1 && ! command -v python >/dev/null 2>&1; then
  echo "skip npm configure (python3 required for JSON)"
  exit 0
fi
PY="python3"
command -v python3 >/dev/null 2>&1 || PY="python"

ADMIN_EMAIL="$(get_env NPM_ADMIN_EMAIL)"
ADMIN_EMAIL="${ADMIN_EMAIL:-admin@example.com}"
ADMIN_PASS="$(get_env NPM_ADMIN_PASSWORD)"
ADMIN_PASS="${ADMIN_PASS:-changeme}"
DOMAIN="$(get_env NPM_DOMAIN)"
ABS_DOMAIN="$(get_env NPM_ABS_DOMAIN)"
KAVITA_DOMAIN="$(get_env NPM_KAVITA_DOMAIN)"
LE_EMAIL="$(get_env NPM_LETSENCRYPT_EMAIL)"

# Advanced nginx snippets for Library (search streams, ABS scans, websockets).
LIBRARY_ADVANCED="$(cat <<'EOF'
# Library Site — long timeouts + websockets
location /api/search/live-stream {
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_http_version 1.1;
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 600s;
    proxy_send_timeout 600s;
    proxy_pass $forward_scheme://$server:$port;
}
location /api/stream/ {
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;
    proxy_request_buffering off;
    proxy_read_timeout 300s;
    proxy_pass $forward_scheme://$server:$port;
}
location /api/requests/ws {
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 86400s;
    proxy_pass $forward_scheme://$server:$port;
}
EOF
)"

echo "Waiting for Nginx Proxy Manager API at ${NPM_BASE} ..."
ready=0
for _ in $(seq 1 $((WAIT_SECONDS / 2))); do
  if curl -fsS --max-time 3 "${NPM_BASE}/api/" >/dev/null 2>&1 \
    || curl -fsS --max-time 3 "${NPM_BASE}/api" >/dev/null 2>&1; then
    ready=1
    break
  fi
  # Fresh containers may return 401/404 on /api/ before fully up — treat as alive.
  code="$(curl -sS --max-time 3 -o /dev/null -w '%{http_code}' "${NPM_BASE}/api/tokens" 2>/dev/null || echo 000)"
  if [[ "$code" =~ ^(200|201|400|401|403|404|405)$ ]]; then
    ready=1
    break
  fi
  sleep 2
done
if [[ "$ready" -ne 1 ]]; then
  echo "skip npm configure (API timeout at ${NPM_BASE})"
  exit 0
fi

export NPM_BASE ADMIN_EMAIL ADMIN_PASS DOMAIN ABS_DOMAIN KAVITA_DOMAIN LE_EMAIL LIBRARY_ADVANCED

"$PY" - <<'PY'
import json, os, sys, urllib.error, urllib.request

base = os.environ["NPM_BASE"].rstrip("/")
admin_email = os.environ["ADMIN_EMAIL"].strip()
admin_pass = os.environ["ADMIN_PASS"]
domain = os.environ.get("DOMAIN", "").strip()
abs_domain = os.environ.get("ABS_DOMAIN", "").strip()
kavita_domain = os.environ.get("KAVITA_DOMAIN", "").strip()
le_email = os.environ.get("LE_EMAIL", "").strip()
library_advanced = os.environ.get("LIBRARY_ADVANCED", "")

DEFAULT_EMAIL = "admin@example.com"
DEFAULT_PASS = "changeme"


def req(method, path, body=None, token=None, timeout=60):
    data = None
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    r = urllib.request.Request(f"{base}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8") or "{}"
            return resp.status, json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except Exception:
            payload = {"error": raw[:500]}
        return e.code, payload


def login(identity, secret):
    code, payload = req("POST", "/api/tokens", {"identity": identity, "secret": secret})
    if code in (200, 201) and payload.get("token"):
        return payload["token"]
    return None


token = login(admin_email, admin_pass)
used_default = False
if not token and (admin_email != DEFAULT_EMAIL or admin_pass != DEFAULT_PASS):
    token = login(DEFAULT_EMAIL, DEFAULT_PASS)
    used_default = bool(token)

if not token:
    print("skip npm configure (login failed — check NPM_ADMIN_EMAIL / NPM_ADMIN_PASSWORD)")
    sys.exit(0)

# Replace factory admin@example.com / changeme when we authenticated with those defaults.
if used_default:
    code, me = req("GET", "/api/users/me", token=token)
    if code == 200 and me.get("id"):
        update = {
            "email": admin_email,
            "name": me.get("name") or "Admin",
            "nickname": me.get("nickname") or "Admin",
            "is_disabled": False,
            "password": admin_pass,
        }
        ucode, _ = req("PUT", f"/api/users/{me['id']}", update, token=token)
        if ucode in (200, 201):
            print(f"NPM admin credentials set ({admin_email})")
            new_token = login(admin_email, admin_pass)
            if new_token:
                token = new_token
        else:
            print(f"warn: could not update default NPM admin (HTTP {ucode})")

print("NPM API authenticated")


def list_hosts():
    code, payload = req("GET", "/api/nginx/proxy-hosts", token=token)
    if code != 200:
        return []
    if isinstance(payload, list):
        return payload
    return payload.get("data") or []


def find_host(hosts, name):
    name_l = name.lower()
    for h in hosts:
        for d in h.get("domain_names") or []:
            if str(d).lower() == name_l:
                return h
    return None


def ensure_cert(domain_names):
    """Request Let's Encrypt cert; return certificate_id or 0."""
    if not le_email:
        return 0
    # Reuse existing cert covering the same domains if present.
    code, certs = req("GET", "/api/nginx/certificates", token=token)
    items = certs if isinstance(certs, list) else (certs.get("data") or []) if code == 200 else []
    want = {d.lower() for d in domain_names}
    for c in items:
        have = {str(x).lower() for x in (c.get("domain_names") or [])}
        if want and want.issubset(have):
            return c.get("id") or 0
    body = {
        "provider": "letsencrypt",
        "domain_names": domain_names,
        "meta": {
            "letsencrypt_email": le_email,
            "letsencrypt_agree": True,
            "dns_challenge": False,
        },
    }
    code, payload = req("POST", "/api/nginx/certificates", body, token=token, timeout=180)
    if code in (200, 201) and payload.get("id"):
        print(f"Let's Encrypt cert issued for {', '.join(domain_names)} (id={payload['id']})")
        return payload["id"]
    err = payload.get("error") or payload.get("message") or payload
    print(f"warn: Let's Encrypt failed for {', '.join(domain_names)}: {err}")
    print("      Ensure DNS A/AAAA points at this host and ports 80/443 are reachable, then re-run:")
    print("      bash scripts/configure_npm.sh")
    return 0


def upsert_host(domain_name, forward_host, forward_port, advanced="", want_ssl=False):
    if not domain_name:
        return
    hosts = list_hosts()
    existing = find_host(hosts, domain_name)
    cert_id = 0
    if want_ssl and le_email:
        cert_id = ensure_cert([domain_name])
    body = {
        "domain_names": [domain_name],
        "forward_scheme": "http",
        "forward_host": forward_host,
        "forward_port": int(forward_port),
        "certificate_id": cert_id,
        "ssl_forced": bool(cert_id),
        "http2_support": bool(cert_id),
        "hsts_enabled": False,
        "hsts_subdomains": False,
        "block_exploits": True,
        "caching_enabled": False,
        "allow_websocket_upgrade": True,
        "access_list_id": 0,
        "advanced_config": advanced or "",
        "enabled": True,
        "meta": {
            "letsencrypt_agree": False,
            "dns_challenge": False,
        },
        "locations": [],
    }
    if existing and existing.get("id"):
        # Preserve an existing cert if we did not obtain a new one.
        if not cert_id and existing.get("certificate_id"):
            body["certificate_id"] = existing["certificate_id"]
            body["ssl_forced"] = bool(existing.get("ssl_forced"))
            body["http2_support"] = bool(existing.get("http2_support"))
        code, payload = req("PUT", f"/api/nginx/proxy-hosts/{existing['id']}", body, token=token)
        action = "updated"
    else:
        code, payload = req("POST", "/api/nginx/proxy-hosts", body, token=token)
        action = "created"
    if code in (200, 201):
        scheme = "https" if body.get("ssl_forced") else "http"
        print(f"Proxy host {action}: {scheme}://{domain_name} -> {forward_host}:{forward_port}")
    else:
        print(f"warn: proxy host {domain_name} failed (HTTP {code}): {payload}")


want_ssl = bool(le_email)

if not domain and not abs_domain and not kavita_domain:
    # LAN-only: create an HTTP proxy host for the host LAN IP / hostname so
    # first-time installs work via port 80 without opening the NPM GUI.
    import socket

    lan_names = []
    try:
        hostname = socket.gethostname().strip()
        if hostname:
            lan_names.append(hostname)
            fqdn = socket.getfqdn().strip()
            if fqdn and fqdn != hostname:
                lan_names.append(fqdn)
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("1.1.1.1", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            lan_names.append(ip)
    except Exception:
        pass
    seen = set()
    lan_names = [n for n in lan_names if not (n in seen or seen.add(n))]
    if lan_names:
        primary = lan_names[0]
        print(f"No NPM_DOMAIN — creating LAN HTTP proxy host(s): {', '.join(lan_names)}")
        for name in lan_names:
            upsert_host(name, "app", 8080, advanced=library_advanced, want_ssl=False)
        print(f"LAN Library URL via NPM: http://{primary}/  (also http://<host>:8085)")
        print(f"  Admin UI: {base}")
        sys.exit(0)
    print("NPM ready (no domains set — LAN / HTTP-only).")
    print(f"  Admin UI: {base}")
    print("  Set NPM_DOMAIN (+ optional NPM_LETSENCRYPT_EMAIL) and re-run this script to create hosts.")
    sys.exit(0)

# Upstream service names on the compose network (internal ports).
upsert_host(domain, "app", 8080, advanced=library_advanced, want_ssl=want_ssl)
upsert_host(abs_domain, "audiobookshelf", 80, want_ssl=want_ssl)
upsert_host(kavita_domain, "kavita", 5000, want_ssl=want_ssl)

if domain:
    scheme = "https" if (want_ssl and le_email) else "http"
    # Prefer https in APP_URL when LE email was provided (even if cert pending — DNS next step).
    app_url = f"{scheme}://{domain}"
    print(f"Suggested APP_URL={app_url}")
PY

# Update APP_URL when a Library domain was configured.
if [[ -n "$DOMAIN" ]]; then
  if [[ -n "$LE_EMAIL" ]]; then
    set_env APP_URL "https://${DOMAIN}"
  else
    set_env APP_URL "http://${DOMAIN}"
  fi
  echo "APP_URL updated for NPM domain"
fi

echo "NPM configure complete"
