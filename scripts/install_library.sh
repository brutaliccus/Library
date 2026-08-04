#!/usr/bin/env bash
# Install / bootstrap Library on a Linux host (Docker Compose).
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/brutaliccus/Library/main/scripts/install_library.sh | bash
#   ./scripts/install_library.sh [/opt/library]
set -euo pipefail

TARGET="${1:-/opt/library}"
REPO_URL="${LIBRARY_SITE_REPO:-https://github.com/brutaliccus/Library.git}"
BRANCH="${LIBRARY_SITE_BRANCH:-main}"

c_cyan() { printf '\033[36m%s\033[0m\n' "$*"; }
c_green() { printf '\033[32m%s\033[0m\n' "$*"; }
c_yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
c_red() { printf '\033[31m%s\033[0m\n' "$*"; }

prompt() {
  local var="$1" msg="$2" def="${3:-}"
  local val
  if [[ -n "$def" ]]; then
    read -r -p "$msg [$def]: " val || true
    val="${val:-$def}"
  else
    read -r -p "$msg: " val || true
  fi
  printf -v "$var" '%s' "$val"
}

prompt_secret() {
  local var="$1" msg="$2"
  local val
  read -r -s -p "$msg: " val || true
  echo
  printf -v "$var" '%s' "$val"
}

yes_no() {
  local msg="$1" def="${2:-n}"
  local val
  read -r -p "$msg [y/N]: " val || true
  val="${val:-$def}"
  [[ "$val" =~ ^[Yy] ]]
}

seed_present() {
  local f="$1"
  [[ -f "$f" ]] && [[ "$(wc -c <"$f" 2>/dev/null || echo 0)" -gt 1048576 ]]
}

ensure_indexer_seed() {
  # Warm torrent/indexer cache (~36 MB gzip → ~150 MB on first-boot import).
  # Prefer repo/LFS copy; else download the GitHub Release asset (optional if it fails).
  local seed_dir="$TARGET/seed"
  local seed_gz="$seed_dir/indexer_cache.db.gz"
  if seed_present "$seed_gz"; then
    local mb
    mb=$(awk "BEGIN {printf \"%.1f\", $(wc -c <"$seed_gz")/1024/1024}")
    c_green "Indexer cache seed present (${mb} MB compressed)"
    return 0
  fi

  if [[ -f "$TARGET/.gitattributes" ]] && command -v git >/dev/null 2>&1; then
    c_cyan "==> Pulling indexer cache seed via Git LFS (if tracked)"
    (cd "$TARGET" && git lfs pull --include "seed/indexer_cache.db.gz") >/dev/null 2>&1 || true
    if seed_present "$seed_gz"; then
      c_green "Indexer cache seed restored via Git LFS"
      return 0
    fi
  fi

  mkdir -p "$seed_dir" 2>/dev/null || sudo mkdir -p "$seed_dir" 2>/dev/null || true
  local url
  for url in \
    "https://github.com/brutaliccus/Library/releases/download/data-seed/indexer_cache.db.gz" \
    "https://github.com/brutaliccus/Library/releases/download/data-seed/seed-cache" \
    "https://github.com/brutaliccus/Library/releases/latest/download/indexer_cache.db.gz"
  do
    c_yellow "Downloading indexer cache seed from $url ..."
    if command -v curl >/dev/null 2>&1; then
      if curl -fsSL "$url" -o "$seed_gz"; then
        if seed_present "$seed_gz"; then
          c_green "Downloaded indexer cache seed"
          return 0
        fi
      fi
    elif command -v wget >/dev/null 2>&1; then
      if wget -q -O "$seed_gz" "$url"; then
        if seed_present "$seed_gz"; then
          c_green "Downloaded indexer cache seed"
          return 0
        fi
      fi
    fi
  done
  c_yellow "Indexer cache seed missing — install continues; first boot starts with an empty cache (optional)."
  c_yellow "Place seed/indexer_cache.db.gz manually or re-run after the data-seed GitHub Release is available."
  return 0
}

c_cyan "==> Library installer"
echo "Target directory: $TARGET"

if ! command -v docker >/dev/null 2>&1; then
  c_red "Docker is required. Install Docker Engine + Compose plugin first."
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  c_red "Docker Compose plugin required (docker compose)."
  exit 1
fi

if [[ ! -d "$TARGET/.git" ]]; then
  c_cyan "==> Cloning repository"
  sudo mkdir -p "$(dirname "$TARGET")"
  if [[ -d "$TARGET" ]] && [[ -z "$(ls -A "$TARGET" 2>/dev/null || true)" ]]; then
    sudo rmdir "$TARGET" 2>/dev/null || true
  fi
  if [[ -d "$TARGET" ]]; then
    c_yellow "Directory exists — using existing tree (not re-cloning)."
  else
    sudo git clone --branch "$BRANCH" "$REPO_URL" "$TARGET"
  fi
else
  c_cyan "==> Updating existing checkout"
  (cd "$TARGET" && sudo git fetch --depth 1 origin "$BRANCH" && sudo git checkout "$BRANCH" && sudo git pull --ff-only) || true
fi

cd "$TARGET"
sudo chown -R "${SUDO_USER:-$USER}:${SUDO_USER:-$USER}" "$TARGET" 2>/dev/null || true

if [[ ! -f .env ]]; then
  cp .env.example .env
  c_green "Created .env from .env.example"
else
  c_yellow ".env already exists — will update selected keys only"
fi

set_env() {
  local key="$1" value="$2"
  if grep -qE "^${key}=" .env 2>/dev/null; then
    # Escape sed specials in value lightly
    local esc
    esc=$(printf '%s' "$value" | sed -e 's/[&|\\]/\\&/g')
    sed -i "s|^${key}=.*|${key}=${esc}|" .env
  else
    printf '%s=%s\n' "$key" "$value" >> .env
  fi
}

c_cyan "==> Core settings"
prompt APP_URL "Public site URL" "https://library.local"
prompt SECRET_KEY "Secret key (random string)" "$(openssl rand -hex 32 2>/dev/null || head -c 32 /dev/urandom | xxd -p -c 32)"
set_env APP_URL "$APP_URL"
set_env SECRET_KEY "$SECRET_KEY"
# Match LibraForge default UID so M4B/Folder Forge can write shared media.
set_env PUID "1000"
set_env PGID "1000"
# Admin Health Start/Stop/Restart needs docker.sock + docker group membership.
_docker_gid="$(getent group docker 2>/dev/null | cut -d: -f3 || true)"
set_env DOCKER_GID "${_docker_gid:-998}"

c_cyan "==> Host media mounts (must exist)"
prompt AUDIO_HOST "Host audiobooks path" "./media/audiobooks"
prompt EBOOK_HOST "Host ebooks path" "./media/ebooks"
prompt OL_HOST "Host Open Library dumps path (optional)" "./media/openlibrary"
for p in "$AUDIO_HOST" "$EBOOK_HOST"; do
  if [[ ! -d "$p" ]]; then
    c_yellow "Creating $p"
    mkdir -p "$p" 2>/dev/null || sudo mkdir -p "$p"
  fi
done
mkdir -p "$OL_HOST" 2>/dev/null || sudo mkdir -p "$OL_HOST" 2>/dev/null || true
# Pipeline staging (ABS skips dot dirs; Kavita must exclude non-dot unorganized)
mkdir -p "$AUDIO_HOST/.unorganized" 2>/dev/null || sudo mkdir -p "$AUDIO_HOST/.unorganized" 2>/dev/null || true
mkdir -p "$EBOOK_HOST/unorganized" 2>/dev/null || sudo mkdir -p "$EBOOK_HOST/unorganized" 2>/dev/null || true
touch "$AUDIO_HOST/.unorganized/.ignore" 2>/dev/null || true

set_env AUDIOBOOK_HOST_DIR "$AUDIO_HOST"
set_env EBOOK_HOST_DIR "$EBOOK_HOST"
set_env OPENLIBRARY_HOST_DIR "$OL_HOST"

get_env() {
  local key="$1"
  grep -E "^${key}=" .env 2>/dev/null | head -1 | cut -d= -f2- || true
}

looks_external_media_url() {
  local url="$1"
  [[ -z "$url" ]] && return 1
  [[ "$url" =~ your-|placeholder|changeme ]] && return 1
  [[ "$url" =~ audiobookshelf|://kavita(:|/|$)|libraforge|host\.docker\.internal|127\.0\.0\.1|localhost|172\.17\.0\.1 ]] && return 1
  return 0
}

ensure_libraforge_clone() {
  local lf_dir="$TARGET/libraforge"
  local url="${LIBRAFORGE_REPO_URL:-https://github.com/coconautilus17/LibraForge.git}"
  if [[ -f "$lf_dir/Dockerfile" ]]; then
    c_green "LibraForge companion present at $lf_dir"
    return 0
  fi
  if ! command -v git >/dev/null 2>&1; then
    c_yellow "Git required to clone LibraForge — bundled LibraForge skipped"
    return 1
  fi
  c_cyan "==> Cloning LibraForge companion into ./libraforge"
  if [[ -d "$lf_dir" ]] && [[ -z "$(ls -A "$lf_dir" 2>/dev/null || true)" ]]; then
    rmdir "$lf_dir" 2>/dev/null || true
  fi
  if [[ -d "$lf_dir" ]]; then
    c_yellow "libraforge/ exists but has no Dockerfile — not overwriting"
    return 1
  fi
  if git clone --depth 1 "$url" "$lf_dir" && [[ -f "$lf_dir/Dockerfile" ]]; then
    c_green "LibraForge cloned"
    return 0
  fi
  c_yellow "LibraForge clone failed"
  return 1
}

EXISTING_ABS_URL="$(get_env ABS_URL)"
EXISTING_ABS_KEY="$(get_env ABS_API_KEY)"
USE_BUNDLED=true
if looks_external_media_url "$EXISTING_ABS_URL" || { [[ -n "$EXISTING_ABS_KEY" ]] && [[ ! "$EXISTING_ABS_KEY" =~ your-|placeholder ]]; }; then
  c_yellow "Existing external ABS/Kavita settings detected — bundled media off by default."
  if yes_no "Start bundled Audiobookshelf + Kavita + LibraForge (Docker profile bundled-media)?" "n"; then
    USE_BUNDLED=true
  else
    USE_BUNDLED=false
  fi
else
  c_cyan "==> Bundled media stack (recommended for new installs)"
  echo "Starts Audiobookshelf (:13378), Kavita (:5000), and LibraForge (:5056) on the same Docker network."
  echo "API keys are bootstrapped into .env after first start — no manual key entry."
  c_yellow "Adds ~1–2 GB RAM vs core indexer stack alone. Pi production with external ABS should say n."
  if yes_no "Enable bundled media stack (profile bundled-media)?" "y"; then
    USE_BUNDLED=true
  else
    USE_BUNDLED=false
  fi
fi

if $USE_BUNDLED; then
  if ! ensure_libraforge_clone; then
    c_yellow "LibraForge companion unavailable — bundled-media disabled for this run"
    USE_BUNDLED=false
  fi
fi

if $USE_BUNDLED; then
  set_env ABS_URL "http://audiobookshelf:80"
  set_env KAVITA_URL "http://kavita:5000"
  set_env LIBRAFORGE_URL "http://127.0.0.1:5056"
  set_env LIBRAFORGE_INTERNAL_URL "http://libraforge:5056"
  c_green "Bundled-media URLs written (keys sync after containers start)"
else
  c_cyan "==> Optional integrations (press Enter to skip — configure later in Admin → Integrations / Settings)"
  prompt PROWLARR_API_KEY "Prowlarr API key" ""
  prompt ABS_URL "Audiobookshelf URL" "${EXISTING_ABS_URL:-http://172.17.0.1:13378}"
  prompt ABS_API_KEY "Audiobookshelf API key" ""
  prompt ABS_LIBRARY_ID "Audiobookshelf library ID" ""
  prompt KAVITA_URL "Kavita URL" "$(get_env KAVITA_URL)"
  KAVITA_URL="${KAVITA_URL:-http://172.17.0.1:5000}"
  prompt KAVITA_API_KEY "Kavita API key" ""
  prompt RD_TOKEN "Real-Debrid API token (server default)" ""
  prompt TORBOX_TOKEN "TorBox API token (optional second debrid)" ""

  [[ -n "$PROWLARR_API_KEY" ]] && set_env PROWLARR_API_KEY "$PROWLARR_API_KEY"
  set_env ABS_URL "$ABS_URL"
  [[ -n "$ABS_API_KEY" ]] && set_env ABS_API_KEY "$ABS_API_KEY"
  [[ -n "$ABS_LIBRARY_ID" ]] && set_env ABS_LIBRARY_ID "$ABS_LIBRARY_ID"
  set_env KAVITA_URL "$KAVITA_URL"
  [[ -n "$KAVITA_API_KEY" ]] && set_env KAVITA_API_KEY "$KAVITA_API_KEY"
  [[ -n "$RD_TOKEN" ]] && set_env REAL_DEBRID_API_TOKEN "$RD_TOKEN"
  [[ -n "$TORBOX_TOKEN" ]] && set_env TORBOX_API_TOKEN "$TORBOX_TOKEN"
fi

c_cyan "==> LibraForge audiobook pipeline (see docs/libraforge.md)"
echo "Flow: .unorganized → Metadata → M4B → Chapter Forge (ASIN) → Folder Forge → ABS"
echo "M4B: Library Site serializes encodes (concurrency 1) across auto-forge + Quick Review."
if ! $USE_BUNDLED; then
  prompt LF_URL "LibraForge public URL" "$(get_env LIBRAFORGE_URL)"
  LF_URL="${LF_URL:-http://127.0.0.1:5056}"
  prompt LF_INTERNAL "LibraForge internal URL (from Library container)" "$(get_env LIBRAFORGE_INTERNAL_URL)"
  LF_INTERNAL="${LF_INTERNAL:-http://172.17.0.1:5056}"
  set_env LIBRAFORGE_URL "$LF_URL"
  set_env LIBRAFORGE_INTERNAL_URL "$LF_INTERNAL"
fi
set_env LIBRAFORGE_M4B_JOBS "1"
if yes_no "Enable automated LibraForge audiobook pipeline?" "y"; then
  set_env LIBRAFORGE_PIPELINE_ENABLED "true"
else
  set_env LIBRAFORGE_PIPELINE_ENABLED "false"
fi

c_cyan "==> Ebook pipeline (DIY organizer — see docs/ebooks.md)"
echo "Flow: unorganized → identify → Author/Series/Title → Kavita"
if yes_no "Enable ebook organizer pipeline?" "y"; then
  set_env EBOOK_PIPELINE_ENABLED "true"
else
  set_env EBOOK_PIPELINE_ENABLED "false"
fi

c_cyan "==> Android APK updates (GitHub Releases)"
prompt APK_REPO "GitHub owner/repo for Library APK releases" "brutaliccus/Library"
set_env ANDROID_APK_GITHUB_REPO "$APK_REPO"

c_cyan "==> Scraper mode"
c_yellow "Deep FlareSolverr crawls are HIGH USAGE on a Pi."
echo "Recommended: RSS-only (ABB + Knaben) — live Jackett search still works."
if yes_no "Enable high-usage deep scrapers (ABB author crawl / Knaben full crawl)?" "n"; then
  set_env ABB_RSS_ONLY "false"
  set_env ABB_AUTHOR_CRAWL_ENABLED "true"
  set_env SCRAPER_KNABEN_CRAWL_TASKS_PER_JOB "8"
  c_yellow "Deep scrapers enabled — monitor CPU/temperature."
else
  set_env ABB_RSS_ONLY "true"
  set_env ABB_AUTHOR_CRAWL_ENABLED "false"
  set_env ABB_DEEP_SEARCH_ENABLED "false"
  set_env ABB_LIVE_SEARCH_ENABLED "false"
  c_green "RSS-only defaults written to .env"
fi

mkdir -p data prowlarr-config jackett-config \
  audiobookshelf-config audiobookshelf-metadata kavita-config \
  libraforge-auth libraforge-config libraforge-reports \
  media/audiobooks media/ebooks media/openlibrary

c_cyan "==> Mullvad VPN sidecar (optional — not required)"
c_yellow "gluetun is behind Docker Compose profile 'vpn' so fresh installs work without WireGuard keys."
# Enable VPN if keys already present, or when the operator opts in now.
_has_wg=false
if grep -qE '^WIREGUARD_PRIVATE_KEY=.+' .env 2>/dev/null && grep -qE '^WIREGUARD_ADDRESSES=.+' .env 2>/dev/null; then
  _has_wg=true
fi
PROFILE_PARTS=()
if $USE_BUNDLED; then
  PROFILE_PARTS+=("bundled-media")
fi
if $_has_wg || yes_no "Enable Mullvad VPN sidecar (gluetun) now? Optional — not required. Needs WireGuard keys in .env" "n"; then
  if ! $_has_wg; then
    c_yellow "Add WIREGUARD_PRIVATE_KEY and WIREGUARD_ADDRESSES to .env (or Admin → Integrations)."
  fi
  PROFILE_PARTS+=("vpn")
  set_env ABB_PROXY_URL "http://gluetun:8888"
else
  set_env ABB_PROXY_URL ""
  set_env WIREGUARD_PRIVATE_KEY ""
  set_env WIREGUARD_ADDRESSES ""
  c_green "VPN profile off — stack starts without gluetun (configure Mullvad later)."
fi
# Join profiles with commas
COMPOSE_PROFILES_VAL=""
for p in "${PROFILE_PARTS[@]+"${PROFILE_PARTS[@]}"}"; do
  if [[ -z "$COMPOSE_PROFILES_VAL" ]]; then
    COMPOSE_PROFILES_VAL="$p"
  else
    COMPOSE_PROFILES_VAL="${COMPOSE_PROFILES_VAL},${p}"
  fi
done
set_env COMPOSE_PROFILES "$COMPOSE_PROFILES_VAL"
if $USE_BUNDLED; then
  c_green "COMPOSE_PROFILES=${COMPOSE_PROFILES_VAL} (bundled-media starts ABS/Kavita/LibraForge)"
fi

c_cyan "==> Ensuring indexer cache seed"
ensure_indexer_seed

c_cyan "==> Starting Docker stack"
c_yellow "First boot imports seed/indexer_cache.db.gz into an empty DB (~150 MB decompressed)."
if $USE_BUNDLED; then
  c_yellow "First LibraForge image build can take several minutes."
  c_yellow "Bundled media keys sync automatically after services are healthy."
else
  c_yellow "After create-admin / create-library / offline PIN, /admin/setup configures ABS, Kavita, and LibraForge."
fi
c_yellow "Optional Open Library catalog (multi-GB) can be skipped, built, or scheduled in that wizard."
docker compose up -d --build

c_cyan "==> Waiting for app health"
for i in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:8085/api/health" >/dev/null 2>&1; then
    c_green "App is healthy"
    break
  fi
  sleep 2
  if [[ "$i" -eq 60 ]]; then
    c_yellow "Health check timed out — check: docker compose logs app"
  fi
done

if [[ -f scripts/sync_jackett_env.sh ]]; then
  c_cyan "==> Syncing Jackett API key into .env"
  bash scripts/sync_jackett_env.sh || true
fi
if [[ -f scripts/sync_prowlarr_env.sh ]]; then
  c_cyan "==> Syncing Prowlarr API key into .env"
  bash scripts/sync_prowlarr_env.sh || true
fi

if $USE_BUNDLED; then
  if [[ -f scripts/sync_abs_env.sh ]]; then
    c_cyan "==> Bootstrapping Audiobookshelf API key + library"
    bash scripts/sync_abs_env.sh || true
  fi
  if [[ -f scripts/sync_kavita_env.sh ]]; then
    c_cyan "==> Bootstrapping Kavita API key + library"
    bash scripts/sync_kavita_env.sh || true
  fi
  if [[ -f scripts/sync_libraforge_env.sh ]]; then
    c_cyan "==> Wiring LibraForge URLs"
    bash scripts/sync_libraforge_env.sh || true
  fi
fi
docker compose up -d app || true

# Host cron helpers are Linux-oriented; skip quietly on non-Linux.
if [[ "$(uname -s 2>/dev/null || true)" == "Linux" ]]; then
  if [[ -x scripts/install_backup_cron.sh ]]; then
    bash scripts/install_backup_cron.sh || true
  fi
  if [[ -x scripts/install_ol_catalog_cron.sh ]]; then
    bash scripts/install_ol_catalog_cron.sh || true
  fi
else
  c_yellow "Skipping host cron installers (non-Linux). Use Admin → Catalog schedule or Task Scheduler."
fi

if [[ -f "${TARGET}/data/app.db" ]]; then
  c_yellow "Existing data/app.db found — first-run admin create only appears when there are zero users."
  c_yellow "To reset first-run: stop the stack, delete data/app.db (+ -wal/-shm), then docker compose up -d."
fi

c_green ""
c_green "Install complete."
echo ""
echo "Next steps:"
echo "  1. Open ${APP_URL%/}/login (or http://<host>:8085/login)"
echo "  2. Create the admin account (shown automatically when the DB has zero users)"
echo "  3. Create library + offline PIN, then /admin/setup"
if $USE_BUNDLED; then
  echo "     Stack step should show Using bundled stack (keys already synced) — Continue"
  echo "  4. Optional Open Library in that wizard (skip freely — indexer seed is enough for search)"
  echo "  5. Optional Mullvad later: WireGuard keys + add vpn to COMPOSE_PROFILES"
else
  echo "     Stack step: ABS / Kavita / LibraForge presets + soft health probes"
  echo "  4. Optional Open Library in that wizard (skip freely — indexer seed is enough for search)"
  echo "  5. ABS: confirm audiobook staging (default .unorganized) is ignored"
  echo "  6. Kavita: exclude ebook staging folder (default unorganized)"
  echo "  7. Optional LibraForge sibling: bash scripts/install_libraforge.sh (docs/libraforge.md)"
  echo "  8. Optional Mullvad later: WireGuard keys + COMPOSE_PROFILES=vpn"
fi
echo ""
echo "Notes:"
echo "  - Profile bundled-media = ABS (:13378) + Kavita (:5000) + LibraForge (:5056) on compose network"
echo "  - Existing Pi with external ABS/Kavita: leave bundled-media off; keep env URLs"
echo "  - TorBox/RD: unique cache wins; both/neither → user preferred provider"
echo "  - PUID/PGID=1000 written so app matches typical LibraForge UID"
echo ""
echo "Stack dir: $TARGET"
echo "Logs:      cd \"$TARGET\" && docker compose logs -f app"
if $USE_BUNDLED; then
  echo "Ports:     app 8085 | ABS 13378 | Kavita 5000 | LibraForge 5056 | prowlarr 9696 | flare 8191 | jackett 9117"
fi
