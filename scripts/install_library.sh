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

c_cyan "==> Optional integrations (press Enter to skip — configure later in Admin → Config)"
prompt PROWLARR_API_KEY "Prowlarr API key" ""
prompt ABS_URL "Audiobookshelf URL" "http://172.17.0.1:13378"
prompt ABS_API_KEY "Audiobookshelf API key" ""
prompt ABS_LIBRARY_ID "Audiobookshelf library ID" ""
prompt KAVITA_URL "Kavita URL" "http://172.17.0.1:5000"
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

c_cyan "==> LibraForge audiobook pipeline (sibling stack — see docs/libraforge.md)"
echo "Flow: .unorganized → Metadata → M4B (queue concurrency 1) → Chapter Forge (ASIN) → Folder Forge → ABS"
prompt LF_URL "LibraForge public URL" "https://forge.library.freiverse.com"
prompt LF_INTERNAL "LibraForge internal URL (from Library container)" "http://172.17.0.1:5056"
set_env LIBRAFORGE_URL "$LF_URL"
set_env LIBRAFORGE_INTERNAL_URL "$LF_INTERNAL"
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

mkdir -p data prowlarr-config jackett-config

c_cyan "==> Starting Docker stack"
c_yellow "First boot imports the shipped indexer cache seed (~150 MB decompressed) if the DB is empty."
c_yellow "Optional Open Library catalog (multi-GB) can be built later from Admin → Config."
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

if [[ -x scripts/sync_jackett_env.sh ]]; then
  c_cyan "==> Syncing Jackett API key into .env"
  bash scripts/sync_jackett_env.sh || true
  docker compose up -d app || true
fi

if [[ -x scripts/install_backup_cron.sh ]]; then
  bash scripts/install_backup_cron.sh || true
fi

c_green ""
c_green "Install complete."
echo ""
echo "Next steps:"
echo "  1. Open ${APP_URL} (or http://<host>:8085)"
echo "  2. Create the admin account"
echo "  3. Complete /admin/setup (libraries, pipelines, debrid, scraper, APK)"
echo "  4. ABS: confirm .unorganized is ignored (dot folder + .ignore)"
echo "  5. Kavita: exclude folder named unorganized from the ebook library"
echo "  6. Optional LibraForge sibling: bash scripts/install_libraforge.sh (docs/libraforge.md)"
echo "  7. Optional magnet extension: load unpacked browser-extension/ (see its README)"
echo "  8. Android APK: GitHub Releases for ${APK_REPO}"
echo "  9. Share invite link from Settings; friends set offline PIN on join"
echo ""
echo "Notes:"
echo "  - TorBox/RD: unique cache wins; both/neither → user preferred provider"
echo "  - My Library uses ABS/local metadata (series/author/genre/sequence); Hardcover genres fill-empty only"
echo "  - Admin → Health: Scan ABS & clean orphans (no metadata rewrite); Open LibraForge"
echo "  - PUID/PGID=1000 written so app matches typical LibraForge UID"
echo ""
echo "Stack dir: $TARGET"
echo "Logs:      cd \"$TARGET\" && docker compose logs -f app"
