#!/usr/bin/env bash
# Install / bootstrap Library on a Linux host (Docker Compose).
# Tuned for Ubuntu Server 24.04 LTS; works on Debian / Raspberry Pi OS too.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/brutaliccus/Library/main/scripts/install_library.sh | bash
#   ./scripts/install_library.sh [/opt/library]
#   LIBRARY_NONINTERACTIVE=1 ./scripts/install_library.sh /opt/library
#
# Env overrides (also used in non-interactive mode):
#   LIBRARY_SITE_REPO, LIBRARY_SITE_BRANCH, LIBRARY_APP_URL, LIBRARY_AUDIO_HOST,
#   LIBRARY_EBOOK_HOST, LIBRARY_OL_HOST, LIBRARY_APK_REPO, LIBRARY_TZ,
#   LIBRARY_SKIP_BUNDLED_MEDIA=1, LIBRARY_SKIP_NPM=1, LIBRARY_ENABLE_VPN=1,
#   LIBRARY_ENABLE_DEEP_SCRAPERS=1, LIBRARY_SKIP_DOCKER_INSTALL=1, LIBRARY_SKIP_BUILD=1,
#   LIBRARY_SKIP_JACKETT=1, LIBRARY_SKIP_PROWLARR=1, LIBRARY_JACKETT_URL, LIBRARY_JACKETT_API_KEY,
#   LIBRARY_PROWLARR_URL, LIBRARY_PROWLARR_API_KEY,
#   LIBRARY_OL_MODE=skip|build|download (Open Library catalog; default skip -- indexers are day-one),
#   LIBRARY_NPM_DOMAIN, LIBRARY_NPM_ABS_DOMAIN, LIBRARY_NPM_KAVITA_DOMAIN,
#   LIBRARY_NPM_LE_EMAIL, LIBRARY_NPM_ADMIN_EMAIL, LIBRARY_NPM_ADMIN_PASSWORD
set -euo pipefail

TARGET="${1:-/opt/library}"
REPO_URL="${LIBRARY_SITE_REPO:-https://github.com/brutaliccus/Library.git}"
BRANCH="${LIBRARY_SITE_BRANCH:-main}"
NONINTERACTIVE="${LIBRARY_NONINTERACTIVE:-0}"
[[ "${2:-}" == "--non-interactive" ]] && NONINTERACTIVE=1
for arg in "$@"; do
  [[ "$arg" == "--non-interactive" ]] && NONINTERACTIVE=1
done

c_cyan() { printf '\033[36m%s\033[0m\n' "$*"; }
c_green() { printf '\033[32m%s\033[0m\n' "$*"; }
c_yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
c_red() { printf '\033[31m%s\033[0m\n' "$*"; }
c_dim() { printf '\033[2m%s\033[0m\n' "$*"; }

STEP_N=0
step() {
  STEP_N=$((STEP_N + 1))
  echo ""
  c_cyan "==> Step ${STEP_N}: $*"
}

explain() { c_dim "    $*"; }

# Interactive reads must use /dev/tty so `curl|bash` does not steal script lines as answers.
_prompt_tty() {
  if [[ -r /dev/tty ]]; then
    printf '%s' /dev/tty
  else
    printf '%s' /dev/stdin
  fi
}

prompt() {
  local var="$1" msg="$2" def="${3-}"
  local val="" tty
  tty="$(_prompt_tty)"
  if [[ "$NONINTERACTIVE" == "1" ]]; then
    printf -v "$var" '%s' "$def"
    return 0
  fi
  if [[ -n "$def" ]]; then
    read -r -p "$msg [$def]: " val <"$tty" || true
    val="${val:-$def}"
  else
    read -r -p "$msg: " val <"$tty" || true
    val="${val-}"
  fi
  printf -v "$var" '%s' "$val"
}

prompt_secret() {
  local var="$1" msg="$2" def="${3-}"
  local val="" tty
  tty="$(_prompt_tty)"
  if [[ "$NONINTERACTIVE" == "1" ]]; then
    printf -v "$var" '%s' "$def"
    return 0
  fi
  if [[ -n "$def" ]]; then
    read -r -s -p "$msg [keep existing / Enter]: " val <"$tty" || true
    echo
    val="${val:-$def}"
  else
    read -r -s -p "$msg (optional, Enter to skip): " val <"$tty" || true
    echo
    val="${val-}"
  fi
  printf -v "$var" '%s' "$val"
}

yes_no() {
  local msg="$1" def="${2:-n}"
  local val="" tty
  tty="$(_prompt_tty)"
  if [[ "$NONINTERACTIVE" == "1" ]]; then
    [[ "$def" =~ ^[Yy] ]]
    return $?
  fi
  local hint="y/N"
  [[ "$def" =~ ^[Yy] ]] && hint="Y/n"
  read -r -p "$msg [$hint]: " val <"$tty" || true
  val="${val:-$def}"
  [[ "$val" =~ ^[Yy] ]]
}

seed_present() {
  local f="$1"
  [[ -f "$f" ]] && [[ "$(wc -c <"$f" 2>/dev/null || echo 0)" -gt 1048576 ]]
}

is_ubuntu_like() {
  [[ -f /etc/os-release ]] || return 1
  # shellcheck disable=SC1091
  . /etc/os-release
  [[ "${ID:-}" == "ubuntu" || "${ID_LIKE:-}" == *debian* || "${ID:-}" == "debian" ]]
}

detect_lan_url() {
  local ip
  ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  if [[ -n "$ip" ]]; then
    echo "http://${ip}:8085"
  else
    echo "http://127.0.0.1:8085"
  fi
}

ensure_prereqs() {
  step "Prerequisites (Ubuntu Server 24.04+)"
  explain "Required: Docker Engine + Compose plugin, Git, curl, openssl"
  explain "Optional: git-lfs (indexer seed), python3 (host helpers)"

  local missing=()
  command -v curl >/dev/null 2>&1 || missing+=("curl")
  command -v git >/dev/null 2>&1 || missing+=("git")
  command -v openssl >/dev/null 2>&1 || missing+=("openssl")

  if [[ ${#missing[@]} -gt 0 ]]; then
    c_yellow "Missing packages: ${missing[*]}"
    if is_ubuntu_like && yes_no "Install missing apt packages now?" "y"; then
      sudo apt-get update -y
      sudo apt-get install -y "${missing[@]}" ca-certificates
    else
      c_red "Install: sudo apt-get install -y ${missing[*]}"
      exit 1
    fi
  fi

  if ! command -v docker >/dev/null 2>&1; then
    c_red "Docker is not installed."
    if [[ "${LIBRARY_SKIP_DOCKER_INSTALL:-0}" != "1" ]] && is_ubuntu_like \
      && yes_no "Install Docker Engine + Compose via official apt repo now?" "y"; then
      sudo apt-get update -y
      sudo apt-get install -y ca-certificates curl
      sudo install -m 0755 -d /etc/apt/keyrings
      if [[ ! -f /etc/apt/keyrings/docker.asc ]]; then
        sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
        sudo chmod a+r /etc/apt/keyrings/docker.asc
      fi
      # shellcheck disable=SC1091
      . /etc/os-release
      echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
        | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
      sudo apt-get update -y
      sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
      sudo systemctl enable --now docker
      if [[ -n "${SUDO_USER:-}" ]]; then
        sudo usermod -aG docker "$SUDO_USER" || true
        c_yellow "Added $SUDO_USER to docker group — re-login (or newgrp docker) if docker needs sudo."
      fi
    else
      c_red "Install Docker Engine + Compose, then re-run. See docs/ubuntu-server-install.md"
      exit 1
    fi
  fi

  if ! docker compose version >/dev/null 2>&1; then
    c_red "Docker Compose plugin required (docker compose)."
    exit 1
  fi

  if ! docker info >/dev/null 2>&1; then
    if sudo docker info >/dev/null 2>&1; then
      c_yellow "Docker needs sudo for this user — installer will use sudo for compose."
      DOCKER="sudo docker"
    else
      c_red "Docker Engine is not running. Try: sudo systemctl start docker"
      exit 1
    fi
  else
    DOCKER="docker"
  fi

  c_green "Docker OK: $($DOCKER compose version --short 2>/dev/null || echo compose)"
}

ensure_indexer_seed() {
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
    if curl -fsSL "$url" -o "$seed_gz"; then
      if seed_present "$seed_gz"; then
        c_green "Downloaded indexer cache seed"
        return 0
      fi
    fi
  done
  c_yellow "Indexer cache seed missing — install continues; first boot starts with an empty cache (optional)."
  return 0
}

# --- begin ---
echo ""
c_cyan "╔══════════════════════════════════════════════════════════╗"
c_cyan "║           Library Site — guided host installer           ║"
c_cyan "╚══════════════════════════════════════════════════════════╝"
echo "Target directory: $TARGET"
[[ "$NONINTERACTIVE" == "1" ]] && c_yellow "Non-interactive mode (LIBRARY_NONINTERACTIVE=1)"

DOCKER="docker"
ensure_prereqs

step "Repository checkout"
if [[ ! -d "$TARGET/.git" ]]; then
  sudo mkdir -p "$(dirname "$TARGET")"
  if [[ -d "$TARGET" ]] && [[ -z "$(ls -A "$TARGET" 2>/dev/null || true)" ]]; then
    sudo rmdir "$TARGET" 2>/dev/null || true
  fi
  if [[ -d "$TARGET" ]] && [[ -f "$TARGET/docker-compose.yml" ]]; then
    c_yellow "Directory exists with compose — using existing tree (not re-cloning)."
  elif [[ -d "$TARGET" ]]; then
    c_yellow "Directory exists — using existing tree (not re-cloning)."
  else
    c_cyan "Cloning $REPO_URL ($BRANCH) → $TARGET"
    sudo git clone --branch "$BRANCH" "$REPO_URL" "$TARGET"
  fi
else
  c_cyan "Updating existing checkout"
  (cd "$TARGET" && sudo git fetch --depth 1 origin "$BRANCH" && sudo git checkout "$BRANCH" && sudo git pull --ff-only) || true
fi

cd "$TARGET"
sudo chown -R "${SUDO_USER:-$USER}:${SUDO_USER:-$USER}" "$TARGET" 2>/dev/null || true

if [[ ! -f .env ]]; then
  cp .env.example .env
  c_green "Created .env from .env.example"
else
  c_yellow ".env already exists — will update selected keys only (secrets preserved when you press Enter)"
fi

set_env() {
  local key="$1" value="$2"
  if grep -qE "^${key}=" .env 2>/dev/null; then
    local esc
    esc=$(printf '%s' "$value" | sed -e 's/[&|\\]/\\&/g')
    sed -i "s|^${key}=.*|${key}=${esc}|" .env
  else
    printf '%s=%s\n' "$key" "$value" >> .env
  fi
}

set_env_if_empty() {
  local key="$1" value="$2"
  local cur
  cur="$(get_env "$key")"
  if [[ -z "$cur" || "$cur" =~ ^(your-|change-me|placeholder) ]]; then
    set_env "$key" "$value"
  fi
}

set_env_secret() {
  # Only write when non-empty so re-runs don't wipe secrets with blank prompts.
  local key="$1" value="$2"
  [[ -z "$value" ]] && return 0
  set_env "$key" "$value"
}

get_env() {
  local key="$1"
  grep -E "^${key}=" .env 2>/dev/null | head -1 | cut -d= -f2- || true
}

# Split COMPOSE_PROFILES into words (comma/space). Empty → no output.
compose_profiles_list() {
  local raw="${1-}"
  raw="${raw//,/ }"
  # shellcheck disable=SC2086
  printf '%s\n' $raw
}

# Join unique non-empty profile names with commas (stable order).
join_compose_profiles() {
  local out="" seen="|" p
  for p in "$@"; do
    [[ -z "$p" ]] && continue
    [[ "$seen" == *"|$p|"* ]] && continue
    seen="${seen}${p}|"
    if [[ -z "$out" ]]; then
      out="$p"
    else
      out="${out},${p}"
    fi
  done
  printf '%s' "$out"
}

# True if comma-separated profiles contain name $1.
compose_profile_has() {
  local needle="$1" raw="${2-}"
  local p
  while IFS= read -r p; do
    [[ "$p" == "$needle" ]] && return 0
  done < <(compose_profiles_list "$raw")
  return 1
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
  c_cyan "Cloning LibraForge companion into ./libraforge"
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

# ---------------------------------------------------------------------------
step "Core app settings [REQUIRED]"
explain "APP_URL — public URL friends open (invite links, CORS, push). Use LAN IP for now; change later for HTTPS."
explain "SECRET_KEY — JWT signing secret (random). DATABASE_URL defaults to SQLite under ./data."
DEFAULT_APP_URL="${LIBRARY_APP_URL:-$(detect_lan_url)}"
EXISTING_SECRET="$(get_env SECRET_KEY)"
EXISTING_SECRET="${EXISTING_SECRET-}"
if [[ -z "$EXISTING_SECRET" || "$EXISTING_SECRET" =~ change-me ]]; then
  EXISTING_SECRET="$(openssl rand -hex 32 2>/dev/null || true)"
  if [[ -z "$EXISTING_SECRET" ]]; then
    EXISTING_SECRET="$(head -c 32 /dev/urandom 2>/dev/null | xxd -p -c 32 2>/dev/null || true)"
  fi
  if [[ -z "$EXISTING_SECRET" ]]; then
    EXISTING_SECRET="$(tr -dc 'a-f0-9' </dev/urandom 2>/dev/null | head -c 64 || true)"
  fi
  if [[ -z "$EXISTING_SECRET" ]]; then
    EXISTING_SECRET="lib-$(date +%s)-${RANDOM}${RANDOM}"
  fi
fi
# Pre-bind under `set -u` so a skipped/failed prompt cannot trip unbound expansion.
APP_URL=""
SECRET_KEY=""
TZ_VAL=""
prompt APP_URL "Public site URL [REQUIRED]" "$DEFAULT_APP_URL"
prompt SECRET_KEY "Secret key [REQUIRED]" "$EXISTING_SECRET"
prompt TZ_VAL "Timezone (TZ)" "${LIBRARY_TZ:-$(cat /etc/timezone 2>/dev/null || echo UTC)}"
APP_URL="${APP_URL:-$DEFAULT_APP_URL}"
SECRET_KEY="${SECRET_KEY:-$EXISTING_SECRET}"
TZ_VAL="${TZ_VAL:-UTC}"
set_env APP_URL "$APP_URL"
set_env SECRET_KEY "$SECRET_KEY"
set_env DATABASE_URL "sqlite+aiosqlite:///data/app.db"
set_env TZ "$TZ_VAL"
# Match LibraForge default UID so M4B/Folder Forge can write shared media.
set_env PUID "1000"
set_env PGID "1000"
# Admin Health Start/Stop/Restart needs docker.sock + docker group membership.
_docker_gid="$(getent group docker 2>/dev/null | cut -d: -f3 || true)"
set_env DOCKER_GID "${_docker_gid:-998}"
set_env AUDIOBOOK_DIR "/audiobooks"
set_env EBOOK_DIR "/ebooks"
set_env AUDIOBOOK_STAGING_DIRNAME ".unorganized"
set_env AUDIOBOOK_STAGING_LEGACY_DIRNAME "_unorganized"
set_env EBOOK_STAGING_DIRNAME "unorganized"

# ---------------------------------------------------------------------------
step "Host media mounts [REQUIRED]"
explain "These host paths are bind-mounted into the app (and bundled ABS/Kavita/LibraForge)."
explain "Use absolute paths for real libraries (e.g. /mnt/Audiobooks). Defaults create ./media/*."
AUDIO_HOST=""
EBOOK_HOST=""
OL_HOST=""
prompt AUDIO_HOST "Host audiobooks path [REQUIRED]" "${LIBRARY_AUDIO_HOST:-./media/audiobooks}"
prompt EBOOK_HOST "Host ebooks path [REQUIRED]" "${LIBRARY_EBOOK_HOST:-./media/ebooks}"
prompt OL_HOST "Host Open Library dumps path [OPTIONAL]" "${LIBRARY_OL_HOST:-./media/openlibrary}"
AUDIO_HOST="${AUDIO_HOST:-./media/audiobooks}"
EBOOK_HOST="${EBOOK_HOST:-./media/ebooks}"
OL_HOST="${OL_HOST:-./media/openlibrary}"
for p in "$AUDIO_HOST" "$EBOOK_HOST"; do
  if [[ ! -d "$p" ]]; then
    c_yellow "Creating $p"
    mkdir -p "$p" 2>/dev/null || sudo mkdir -p "$p"
  fi
done
mkdir -p "$OL_HOST" 2>/dev/null || sudo mkdir -p "$OL_HOST" 2>/dev/null || true
mkdir -p "$AUDIO_HOST/.unorganized" 2>/dev/null || sudo mkdir -p "$AUDIO_HOST/.unorganized" 2>/dev/null || true
mkdir -p "$EBOOK_HOST/unorganized" 2>/dev/null || sudo mkdir -p "$EBOOK_HOST/unorganized" 2>/dev/null || true
touch "$AUDIO_HOST/.unorganized/.ignore" 2>/dev/null || true

set_env AUDIOBOOK_HOST_DIR "$AUDIO_HOST"
set_env EBOOK_HOST_DIR "$EBOOK_HOST"
set_env OPENLIBRARY_HOST_DIR "$OL_HOST"

# ---------------------------------------------------------------------------
step "Bundled media stack (ABS + Kavita + LibraForge) [RECOMMENDED]"
EXISTING_ABS_URL="$(get_env ABS_URL)"
EXISTING_ABS_KEY="$(get_env ABS_API_KEY)"
USE_BUNDLED=true
if [[ "${LIBRARY_SKIP_BUNDLED_MEDIA:-0}" == "1" ]]; then
  USE_BUNDLED=false
  c_yellow "LIBRARY_SKIP_BUNDLED_MEDIA=1 — bundled media off"
elif looks_external_media_url "$EXISTING_ABS_URL" || { [[ -n "$EXISTING_ABS_KEY" ]] && [[ ! "$EXISTING_ABS_KEY" =~ your-|placeholder ]]; }; then
  c_yellow "Existing external ABS/Kavita settings detected — bundled media off by default."
  if yes_no "Start bundled Audiobookshelf + Kavita + LibraForge (Docker profile bundled-media)?" "n"; then
    USE_BUNDLED=true
  else
    USE_BUNDLED=false
  fi
else
  explain "Starts Audiobookshelf (:13378), Kavita (:5000), and LibraForge (:5056) on the same Docker network."
  explain "API keys are bootstrapped into .env after first start — no manual key entry."
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
  step "External ABS / Kavita / LibraForge [REQUIRED for libraries]"
  explain "Press Enter to skip keys — finish them later in Admin → Instance setup."
  prompt ABS_URL "Audiobookshelf URL" "${EXISTING_ABS_URL:-http://172.17.0.1:13378}"
  prompt_secret ABS_API_KEY "Audiobookshelf API key" "$(get_env ABS_API_KEY)"
  prompt ABS_LIBRARY_ID "Audiobookshelf library ID" "$(get_env ABS_LIBRARY_ID)"
  prompt KAVITA_URL "Kavita URL" "$(get_env KAVITA_URL)"
  KAVITA_URL="${KAVITA_URL:-http://172.17.0.1:5000}"
  prompt_secret KAVITA_API_KEY "Kavita API key" "$(get_env KAVITA_API_KEY)"
  prompt KAVITA_LIBRARY_ID "Kavita library ID (0 = first)" "$(get_env KAVITA_LIBRARY_ID)"
  KAVITA_LIBRARY_ID="${KAVITA_LIBRARY_ID:-0}"
  prompt LF_URL "LibraForge public URL" "$(get_env LIBRAFORGE_URL)"
  LF_URL="${LF_URL:-http://127.0.0.1:5056}"
  prompt LF_INTERNAL "LibraForge internal URL (from Library container)" "$(get_env LIBRAFORGE_INTERNAL_URL)"
  LF_INTERNAL="${LF_INTERNAL:-http://172.17.0.1:5056}"
  set_env ABS_URL "$ABS_URL"
  set_env_secret ABS_API_KEY "$ABS_API_KEY"
  set_env_secret ABS_LIBRARY_ID "$ABS_LIBRARY_ID"
  set_env KAVITA_URL "$KAVITA_URL"
  set_env_secret KAVITA_API_KEY "$KAVITA_API_KEY"
  set_env KAVITA_LIBRARY_ID "$KAVITA_LIBRARY_ID"
  set_env LIBRAFORGE_URL "$LF_URL"
  set_env LIBRAFORGE_INTERNAL_URL "$LF_INTERNAL"
fi

# ---------------------------------------------------------------------------
step "Jackett (AudioBook Bay Torznab) [RECOMMENDED]"
explain "Bundled Jackett is preconfigured for AudioBookBay (audiobookbay.is) + FlareSolverr."
explain "Already run Jackett elsewhere? Connect URL + API key instead of using the local container."
USE_BUNDLED_JACKETT=true
JACKETT_EXT_URL=""
JACKETT_EXT_KEY=""
if [[ "${LIBRARY_SKIP_JACKETT:-0}" == "1" ]]; then
  USE_BUNDLED_JACKETT=false
  c_yellow "LIBRARY_SKIP_JACKETT=1 — will connect external Jackett"
elif [[ -n "${LIBRARY_JACKETT_URL:-}" && -n "${LIBRARY_JACKETT_API_KEY:-}" ]]; then
  USE_BUNDLED_JACKETT=false
  JACKETT_EXT_URL="$LIBRARY_JACKETT_URL"
  JACKETT_EXT_KEY="$LIBRARY_JACKETT_API_KEY"
elif yes_no "Deploy + preconfigure bundled Jackett? (Already have Jackett? answer n)" "y"; then
  USE_BUNDLED_JACKETT=true
else
  USE_BUNDLED_JACKETT=false
fi
if $USE_BUNDLED_JACKETT; then
  set_env JACKETT_URL "http://audiobook-jackett:9117"
  c_green "Bundled Jackett — ABB indexer + FlareSolverr wired after first start"
else
  prompt JACKETT_EXT_URL "Existing Jackett URL" "${JACKETT_EXT_URL:-$(get_env JACKETT_URL)}"
  prompt_secret JACKETT_EXT_KEY "Existing Jackett API key" "${JACKETT_EXT_KEY:-$(get_env JACKETT_API_KEY)}"
  if [[ -n "$JACKETT_EXT_URL" && -n "$JACKETT_EXT_KEY" ]]; then
    set_env JACKETT_URL "$JACKETT_EXT_URL"
    set_env JACKETT_API_KEY "$JACKETT_EXT_KEY"
    c_green "External Jackett credentials saved"
  else
    c_yellow "No Jackett URL/key — falling back to bundled Jackett"
    USE_BUNDLED_JACKETT=true
    set_env JACKETT_URL "http://audiobook-jackett:9117"
  fi
fi

# ---------------------------------------------------------------------------
step "Prowlarr (ABB + Knaben indexers) [RECOMMENDED]"
explain "Bundled Prowlarr gets native Knaben + AudioBookBay Torznab → Jackett (same as production)."
explain "Already run Prowlarr elsewhere? Connect URL + API key; installer still tries to add ABB/Knaben."
USE_BUNDLED_PROWLARR=true
PROWLARR_EXT_URL=""
PROWLARR_EXT_KEY=""
if [[ "${LIBRARY_SKIP_PROWLARR:-0}" == "1" ]]; then
  USE_BUNDLED_PROWLARR=false
  c_yellow "LIBRARY_SKIP_PROWLARR=1 — will connect external Prowlarr"
elif [[ -n "${LIBRARY_PROWLARR_URL:-}" && -n "${LIBRARY_PROWLARR_API_KEY:-}" ]]; then
  USE_BUNDLED_PROWLARR=false
  PROWLARR_EXT_URL="$LIBRARY_PROWLARR_URL"
  PROWLARR_EXT_KEY="$LIBRARY_PROWLARR_API_KEY"
elif yes_no "Deploy + preconfigure bundled Prowlarr? (Already have Prowlarr? answer n)" "y"; then
  USE_BUNDLED_PROWLARR=true
else
  USE_BUNDLED_PROWLARR=false
fi
if $USE_BUNDLED_PROWLARR; then
  set_env PROWLARR_URL "http://prowlarr:9696"
  c_green "Bundled Prowlarr — Knaben + ABB wired after first start"
else
  prompt PROWLARR_EXT_URL "Existing Prowlarr URL" "${PROWLARR_EXT_URL:-$(get_env PROWLARR_URL)}"
  prompt_secret PROWLARR_EXT_KEY "Existing Prowlarr API key" "${PROWLARR_EXT_KEY:-$(get_env PROWLARR_API_KEY)}"
  if [[ -n "$PROWLARR_EXT_URL" && -n "$PROWLARR_EXT_KEY" ]]; then
    set_env PROWLARR_URL "$PROWLARR_EXT_URL"
    set_env PROWLARR_API_KEY "$PROWLARR_EXT_KEY"
    c_green "External Prowlarr credentials saved"
  else
    c_yellow "No Prowlarr URL/key — falling back to bundled Prowlarr"
    USE_BUNDLED_PROWLARR=true
    set_env PROWLARR_URL "http://prowlarr:9696"
  fi
fi

set_env FLARESOLVERR_URL "http://flaresolverr:8191"
set_env_if_empty SCRAPER_ENABLED "true"
set_env_if_empty SCRAPER_RSS_EVERY_N_JOBS "1"

# ---------------------------------------------------------------------------
step "Open Library catalog [ADVANCED / OPTIONAL]"
explain "Day-one search uses Jackett/Prowlarr + the indexer cache seed (~36 MB) — not Open Library."
explain "A local OL SQLite DB is multi-GB and optional (Admin → Catalog later). Skip is recommended."
OL_MODE="${LIBRARY_OL_MODE:-}"
if [[ -z "$OL_MODE" ]]; then
  if [[ "$NONINTERACTIVE" == "1" ]]; then
    OL_MODE="skip"
  else
    echo "  [1] Skip for now (recommended — indexers cover search)"
    echo "  [2] Build locally from Open Library dumps (hours + multi-GB disk)"
    echo "  [3] Download a prebuilt OL DB if a maintainer published one (very large; usually unavailable)"
    _ol_choice=""
    prompt _ol_choice "Open Library catalog setup" "1"
    case "${_ol_choice}" in
      2|b|B|build|Build) OL_MODE="build" ;;
      3|d|D|download|Download|prebuilt) OL_MODE="download" ;;
      *) OL_MODE="skip" ;;
    esac
  fi
fi
OL_MODE="$(printf '%s' "$OL_MODE" | tr '[:upper:]' '[:lower:]')"
set_env_if_empty OL_CATALOG_DB_PATH "/app/data/ol_catalog.db"
set_env_if_empty OL_DUMPS_DIR "/openlibrary/dumps"
c_green "Open Library mode: ${OL_MODE}"

# ---------------------------------------------------------------------------
step "Debrid providers [OPTIONAL — can set later]"
explain "Server defaults for downloads. Users can also set keys per library group."
explain "TorBox uses qBittorrent-style states internally — no separate qBittorrent container."
prompt_secret RD_TOKEN "Real-Debrid API token" "$(get_env REAL_DEBRID_API_TOKEN)"
prompt_secret TORBOX_TOKEN "TorBox API token" "$(get_env TORBOX_API_TOKEN)"
set_env_secret REAL_DEBRID_API_TOKEN "$RD_TOKEN"
set_env_secret TORBOX_API_TOKEN "$TORBOX_TOKEN"

# ---------------------------------------------------------------------------
step "Pipelines & Library Sweep defaults"
explain "Audiobooks: .unorganized → Metadata → M4B → Chapter Forge → Folder Forge → ABS"
explain "Ebooks: unorganized → identify → Author/Series/Title → Kavita"
explain "Sweep scan cadence defaults to every 25 books (ABS / Kavita)."
if yes_no "Enable automated LibraForge audiobook pipeline?" "y"; then
  set_env LIBRAFORGE_PIPELINE_ENABLED "true"
else
  set_env LIBRAFORGE_PIPELINE_ENABLED "false"
fi
if yes_no "Enable ebook organizer pipeline?" "y"; then
  set_env EBOOK_PIPELINE_ENABLED "true"
else
  set_env EBOOK_PIPELINE_ENABLED "false"
fi
set_env LIBRAFORGE_M4B_JOBS "1"
set_env_if_empty LIBRAFORGE_MIN_SCORE "0.70"
set_env_if_empty EBOOK_MIN_SCORE "0.70"
set_env_if_empty LIBRAFORGE_NAMING_TEMPLATE "{author}/{series} [{edition}]/{title}/{filename}"
set_env_if_empty LIBRAFORGE_METADATA_PROVIDER "audible"
set_env_if_empty LIBRARY_SWEEP_ABS_SCAN_EVERY "25"
set_env_if_empty EBOOK_SWEEP_KAVITA_SCAN_EVERY "25"
set_env_if_empty EBOOK_SWEEP_CONVERT_ALL_TO_EPUB "true"
set_env_if_empty EBOOK_SWEEP_FORCE_METADATA "true"

# ---------------------------------------------------------------------------
step "Catalog APIs & LLM assist [OPTIONAL]"
explain "Hardcover = store ratings/series. OpenRouter = LLM assist for forge/identify (off by default)."
explain "Anna's Archive membership cookie speeds AA ebook downloads. NYT/ISBNdb/Google Books optional."
if yes_no "Configure catalog / LLM keys now?" "n"; then
  prompt_secret HARDCOVER_KEY "Hardcover API key" "$(get_env HARDCOVER_API_KEY)"
  prompt_secret OPENROUTER_KEY "OpenRouter API key" "$(get_env OPENROUTER_API_KEY)"
  prompt_secret AA_COOKIE "Anna's Archive membership cookie" "$(get_env AA_ACCOUNT_ID)"
  prompt_secret NYT_KEY "NYT Books API key" "$(get_env NYT_API_KEY)"
  prompt_secret ISBNDB_KEY "ISBNdb API key" "$(get_env ISBNDB_API_KEY)"
  prompt_secret GBOOKS_KEY "Google Books API key" "$(get_env GOOGLE_BOOKS_API_KEY)"
  set_env_secret HARDCOVER_API_KEY "$HARDCOVER_KEY"
  set_env_secret OPENROUTER_API_KEY "$OPENROUTER_KEY"
  set_env_secret AA_ACCOUNT_ID "$AA_COOKIE"
  set_env_secret NYT_API_KEY "$NYT_KEY"
  set_env_secret ISBNDB_API_KEY "$ISBNDB_KEY"
  set_env_secret GOOGLE_BOOKS_API_KEY "$GBOOKS_KEY"
  if [[ -n "$OPENROUTER_KEY" ]]; then
    set_env OPENROUTER_ENABLED "true"
    set_env_if_empty OPENROUTER_MODEL "openai/gpt-4o-mini"
    set_env_if_empty OPENROUTER_CONFIDENCE_THRESHOLD "0.85"
  else
    set_env_if_empty OPENROUTER_ENABLED "false"
  fi
else
  set_env_if_empty OPENROUTER_ENABLED "false"
  c_green "Skipped — configure later in Admin → Integrations / Catalog"
fi

# ---------------------------------------------------------------------------
step "Android APK updates [OPTIONAL]"
explain "In-app updater reads GitHub Releases for the latest .apk. OPDS for ereaders needs no extra env (uses Kavita key)."
prompt APK_REPO "GitHub owner/repo for Library APK releases" "${LIBRARY_APK_REPO:-brutaliccus/Library}"
set_env ANDROID_APK_GITHUB_REPO "$APK_REPO"
set_env_if_empty ANDROID_MIN_VERSION_CODE "59"
set_env_if_empty ANDROID_FORCE_UPDATES "true"

# ---------------------------------------------------------------------------
step "Scraper / Flare usage [RECOMMENDED: RSS-only]"
c_yellow "Deep FlareSolverr crawls are HIGH USAGE (CPU + RAM)."
explain "Recommended: RSS-only (ABB + Knaben) — live Jackett ABB search still works."
if [[ "${LIBRARY_ENABLE_DEEP_SCRAPERS:-0}" == "1" ]] || yes_no "Enable high-usage deep scrapers (ABB author crawl / Knaben full crawl)?" "n"; then
  set_env ABB_RSS_ONLY "false"
  set_env ABB_AUTHOR_CRAWL_ENABLED "true"
  set_env SCRAPER_KNABEN_CRAWL_TASKS_PER_JOB "8"
  c_yellow "Deep scrapers enabled — monitor CPU/temperature."
else
  set_env ABB_RSS_ONLY "true"
  set_env ABB_AUTHOR_CRAWL_ENABLED "false"
  set_env ABB_DEEP_SEARCH_ENABLED "false"
  set_env ABB_LIVE_SEARCH_ENABLED "false"
  c_green "RSS-only defaults written to .env (Knaben RSS-only applied in Admin setup defaults)"
fi

mkdir -p data prowlarr-config jackett-config \
  audiobookshelf-config audiobookshelf-metadata kavita-config \
  libraforge-auth libraforge-config libraforge-reports \
  npm-data npm-letsencrypt \
  media/audiobooks media/ebooks media/openlibrary

# ---------------------------------------------------------------------------
step "Nginx Proxy Manager (reverse proxy) [RECOMMENDED]"
explain "Remote HTTPS needs a reverse proxy. Fresh installs start NPM (compose profile npm)."
explain "Answer No only if you already run NPM / Caddy / Traefik / nginx on ports 80/443."
_EXISTING_PROFILES="$(get_env COMPOSE_PROFILES)"
_NPM_DEF="y"
# Re-runs: keep NPM on by default when .env already enabled it.
if compose_profile_has "npm" "$_EXISTING_PROFILES"; then
  _NPM_DEF="y"
elif [[ -n "$(get_env NPM_ADMIN_PASSWORD)" && "$(get_env NPM_ADMIN_PASSWORD)" != "changeme" ]]; then
  _NPM_DEF="y"
fi
USE_NPM=false
if [[ "${LIBRARY_SKIP_NPM:-0}" == "1" ]]; then
  c_yellow "LIBRARY_SKIP_NPM=1 — Nginx Proxy Manager off"
elif yes_no "Enable Nginx Proxy Manager (publishes 80/443 + admin :81)?" "$_NPM_DEF"; then
  USE_NPM=true
else
  c_yellow "Skipped NPM. For remote HTTPS later, point your reverse proxy at http://127.0.0.1:8085"
  c_yellow "APP_URL should be the public https:// URL friends open (e.g. https://library.example.com)."
fi

NPM_DOMAIN=""
NPM_ABS_DOMAIN=""
NPM_KAVITA_DOMAIN=""
NPM_LE_EMAIL=""
NPM_ADMIN_EMAIL=""
NPM_ADMIN_PASSWORD=""
if $USE_NPM; then
  explain "Ports 80 + 443 (public) and 81 (NPM admin UI). Container name: library-npm."
  # Soft conflict check — do not fail install yet; hard-fail after compose if :81 never listens.
  if command -v ss >/dev/null 2>&1; then
    if ss -tln 2>/dev/null | grep -qE ':80\s'; then
      c_yellow "Port 80 looks busy — NPM may fail to bind. Free the port or answer No to skip NPM."
    fi
    if ss -tln 2>/dev/null | grep -qE ':443\s'; then
      c_yellow "Port 443 looks busy — NPM may fail to bind HTTPS."
    fi
  fi
  _def_npm_domain="${LIBRARY_NPM_DOMAIN:-$(get_env NPM_DOMAIN)}"
  _def_npm_abs="${LIBRARY_NPM_ABS_DOMAIN:-$(get_env NPM_ABS_DOMAIN)}"
  _def_npm_kavita="${LIBRARY_NPM_KAVITA_DOMAIN:-$(get_env NPM_KAVITA_DOMAIN)}"
  _def_npm_le="${LIBRARY_NPM_LE_EMAIL:-$(get_env NPM_LETSENCRYPT_EMAIL)}"
  _def_npm_admin="$(get_env NPM_ADMIN_EMAIL)"
  _def_npm_admin="${LIBRARY_NPM_ADMIN_EMAIL:-${_def_npm_admin:-admin@example.com}}"
  prompt NPM_DOMAIN "Library public domain (blank = LAN / configure hosts later)" "$_def_npm_domain"
  if $USE_BUNDLED; then
    prompt NPM_ABS_DOMAIN "Audiobookshelf domain (optional)" "$_def_npm_abs"
    prompt NPM_KAVITA_DOMAIN "Kavita domain (optional)" "$_def_npm_kavita"
  fi
  prompt NPM_LE_EMAIL "Let's Encrypt email (blank = HTTP only, no SSL yet)" "$_def_npm_le"
  prompt NPM_ADMIN_EMAIL "NPM admin email" "$_def_npm_admin"
  EXISTING_NPM_PASS="$(get_env NPM_ADMIN_PASSWORD)"
  if [[ -z "$EXISTING_NPM_PASS" || "$EXISTING_NPM_PASS" == "changeme" ]]; then
    EXISTING_NPM_PASS="$(openssl rand -hex 12 2>/dev/null || echo "library-npm-$(date +%s)")"
  fi
  prompt_secret NPM_ADMIN_PASSWORD "NPM admin password" "${LIBRARY_NPM_ADMIN_PASSWORD:-$EXISTING_NPM_PASS}"
  NPM_ADMIN_EMAIL="${NPM_ADMIN_EMAIL:-admin@example.com}"
  NPM_ADMIN_PASSWORD="${NPM_ADMIN_PASSWORD:-$EXISTING_NPM_PASS}"
  set_env NPM_ADMIN_EMAIL "$NPM_ADMIN_EMAIL"
  set_env NPM_ADMIN_PASSWORD "$NPM_ADMIN_PASSWORD"
  set_env NPM_DOMAIN "$NPM_DOMAIN"
  set_env NPM_ABS_DOMAIN "$NPM_ABS_DOMAIN"
  set_env NPM_KAVITA_DOMAIN "$NPM_KAVITA_DOMAIN"
  set_env NPM_LETSENCRYPT_EMAIL "$NPM_LE_EMAIL"
  set_env NPM_DISABLE_IPV6 "true"
  if [[ -n "$NPM_DOMAIN" ]]; then
    if [[ -n "$NPM_LE_EMAIL" ]]; then
      set_env APP_URL "https://${NPM_DOMAIN}"
      APP_URL="https://${NPM_DOMAIN}"
      c_green "APP_URL → https://${NPM_DOMAIN} (Let's Encrypt after DNS points here)"
    else
      set_env APP_URL "http://${NPM_DOMAIN}"
      APP_URL="http://${NPM_DOMAIN}"
      c_green "APP_URL → http://${NPM_DOMAIN} (HTTP; add LE email later for HTTPS)"
    fi
  else
    c_yellow "No domain — NPM still starts; admin on :81 + LAN HTTP proxy on :80."
    c_yellow "Later: set NPM_DOMAIN (+ NPM_LETSENCRYPT_EMAIL) in .env, then: bash scripts/configure_npm.sh"
  fi
fi

# ---------------------------------------------------------------------------
step "Mullvad VPN sidecar (gluetun) [OPTIONAL]"
c_yellow "gluetun is behind Docker Compose profile 'vpn' — fresh installs work without WireGuard keys."
_has_wg=false
if grep -qE '^WIREGUARD_PRIVATE_KEY=.+' .env 2>/dev/null && grep -qE '^WIREGUARD_ADDRESSES=.+' .env 2>/dev/null; then
  _has_wg=true
fi
# Rebuild known profiles from prompts; preserve any other COMPOSE_PROFILES entries.
PROFILE_PARTS=()
_OTHER_PROFILES=()
while IFS= read -r _p; do
  case "$_p" in
    ""|bundled-media|npm|vpn) ;;
    *) _OTHER_PROFILES+=("$_p") ;;
  esac
done < <(compose_profiles_list "$_EXISTING_PROFILES")
if $USE_BUNDLED; then
  PROFILE_PARTS+=("bundled-media")
fi
if $USE_NPM; then
  PROFILE_PARTS+=("npm")
fi
_enable_vpn=false
_vpn_def="n"
compose_profile_has "vpn" "$_EXISTING_PROFILES" && _has_wg && _vpn_def="y"
if [[ "${LIBRARY_ENABLE_VPN:-0}" == "1" ]]; then
  _enable_vpn=true
elif $_has_wg || yes_no "Enable Mullvad VPN sidecar (gluetun) now? Optional — not required." "$_vpn_def"; then
  _enable_vpn=true
fi
if $_enable_vpn; then
  if ! $_has_wg; then
    c_yellow "Add WIREGUARD_PRIVATE_KEY and WIREGUARD_ADDRESSES to .env (or Admin → Integrations)."
  fi
  PROFILE_PARTS+=("vpn")
  set_env ABB_PROXY_URL "http://gluetun:8888"
else
  set_env ABB_PROXY_URL ""
  # Present empty keys so compose does not warn on unset WIREGUARD_* vars.
  if [[ -z "$(get_env WIREGUARD_PRIVATE_KEY)" ]]; then
    set_env WIREGUARD_PRIVATE_KEY ""
  fi
  if [[ -z "$(get_env WIREGUARD_ADDRESSES)" ]]; then
    set_env WIREGUARD_ADDRESSES ""
  fi
  c_green "VPN profile off — stack starts without gluetun (configure Mullvad later)."
fi
# Append preserved unknown profiles last (never wipe custom entries).
PROFILE_PARTS+=("${_OTHER_PROFILES[@]+"${_OTHER_PROFILES[@]}"}")
COMPOSE_PROFILES_VAL="$(join_compose_profiles "${PROFILE_PARTS[@]+"${PROFILE_PARTS[@]}"}")"
set_env COMPOSE_PROFILES "$COMPOSE_PROFILES_VAL"
# Also export so this shell's compose invocations honor profiles even if .env load fails.
export COMPOSE_PROFILES="$COMPOSE_PROFILES_VAL"
_profile_note=""
$USE_BUNDLED && _profile_note="${_profile_note}bundled-media "
$USE_NPM && _profile_note="${_profile_note}npm "
$_enable_vpn && _profile_note="${_profile_note}vpn "
if [[ -n "$COMPOSE_PROFILES_VAL" ]]; then
  c_green "COMPOSE_PROFILES=${COMPOSE_PROFILES_VAL}${_profile_note:+ (${_profile_note})}"
else
  c_yellow "COMPOSE_PROFILES empty — core stack only (no bundled-media / npm / vpn)"
fi
# Build --profile args for explicit compose up (belt + suspenders with COMPOSE_PROFILES).
COMPOSE_PROFILE_ARGS=()
for _p in "${PROFILE_PARTS[@]+"${PROFILE_PARTS[@]}"}"; do
  [[ -n "$_p" ]] && COMPOSE_PROFILE_ARGS+=(--profile "$_p")
done

# ---------------------------------------------------------------------------
step "Indexer cache seed"
ensure_indexer_seed

# ---------------------------------------------------------------------------
step "Open Library catalog action"
case "$OL_MODE" in
  download|prebuilt|d)
    if [[ -f data/ol_catalog.db ]] && [[ "$(wc -c <data/ol_catalog.db 2>/dev/null || echo 0)" -gt 1048576 ]]; then
      c_green "OL catalog already present at data/ol_catalog.db"
    elif [[ -f scripts/fetch_ol_catalog.py ]]; then
      c_cyan "Attempting optional prebuilt Open Library catalog download (large; soft-fail if missing) ..."
      if ! python3 scripts/fetch_ol_catalog.py 2>/dev/null && ! python scripts/fetch_ol_catalog.py 2>/dev/null; then
        c_yellow "No prebuilt OL DB on the release — continuing. Indexer search still works."
        c_yellow "Advanced: Admin → Catalog, or scripts/ol_import_dumps.py / scripts/fetch_ol_catalog.sh later."
      fi
    fi
    ;;
  build|b)
    c_yellow "Local OL build starts after the app container is up (can take many hours)."
    ;;
  *)
    c_dim "Skipped Open Library catalog — store browse still works via Google Books / live APIs."
    ;;
esac

# ---------------------------------------------------------------------------
step "Start Docker stack"
c_yellow "First boot imports seed/indexer_cache.db.gz into an empty DB (~150 MB decompressed)."
if $USE_BUNDLED; then
  c_yellow "First LibraForge image build can take several minutes."
  c_yellow "Bundled media keys sync automatically after services are healthy."
else
  c_yellow "After create-admin / create-library / offline PIN, /admin/setup configures ABS, Kavita, and LibraForge."
fi
if $USE_NPM; then
  c_yellow "Starting Nginx Proxy Manager (library-npm) on 80/443/81 — required when Enable NPM = Yes."
fi
# Explicit --profile flags + COMPOSE_PROFILES in .env / environment (resume-safe).
if [[ "${LIBRARY_SKIP_BUILD:-0}" == "1" ]]; then
  $DOCKER compose "${COMPOSE_PROFILE_ARGS[@]+"${COMPOSE_PROFILE_ARGS[@]}"}" up -d
else
  $DOCKER compose "${COMPOSE_PROFILE_ARGS[@]+"${COMPOSE_PROFILE_ARGS[@]}"}" up -d --build
fi

# Resume / harden: if npm was requested, force the profile service up even when a prior
# compose run left COMPOSE_PROFILES incomplete or the container was never created.
if $USE_NPM; then
  c_cyan "Ensuring library-npm is up (compose profile npm) ..."
  $DOCKER compose --profile npm up -d nginx-proxy-manager
fi

# When operators connected external Jackett/Prowlarr, stop the unused local containers
# so they do not compete for RAM (compose still defines them for easy re-enable).
if ! $USE_BUNDLED_JACKETT; then
  c_yellow "Stopping bundled Jackett (using external JACKETT_URL)"
  $DOCKER compose stop jackett >/dev/null 2>&1 || true
fi
if ! $USE_BUNDLED_PROWLARR; then
  c_yellow "Stopping bundled Prowlarr (using external PROWLARR_URL)"
  $DOCKER compose stop prowlarr >/dev/null 2>&1 || true
fi

# ---------------------------------------------------------------------------
step "Wait for app health"
APP_OK=false
for i in $(seq 1 90); do
  if curl -fsS "http://127.0.0.1:8085/api/health" >/dev/null 2>&1; then
    c_green "App is healthy (http://127.0.0.1:8085/api/health)"
    APP_OK=true
    break
  fi
  sleep 2
  if [[ "$i" -eq 90 ]]; then
    c_yellow "Health check timed out — check: $DOCKER compose logs app"
  fi
done

# ---------------------------------------------------------------------------
step "Configure Jackett / Prowlarr / sync keys"
if $USE_BUNDLED_JACKETT; then
  if [[ -f scripts/configure_jackett.sh ]]; then
    c_cyan "Preconfiguring Jackett (FlareSolverr + AudioBookBay)"
    bash scripts/configure_jackett.sh --force-bundled || true
  elif [[ -f scripts/sync_jackett_env.sh ]]; then
    bash scripts/sync_jackett_env.sh || true
  fi
else
  if [[ -f scripts/configure_jackett.sh && -n "$(get_env JACKETT_API_KEY)" ]]; then
    bash scripts/configure_jackett.sh \
      --external-url "$(get_env JACKETT_URL)" \
      --external-api-key "$(get_env JACKETT_API_KEY)" || true
  fi
fi
if $USE_BUNDLED_PROWLARR; then
  if [[ -f scripts/configure_prowlarr.sh ]]; then
    c_cyan "Preconfiguring Prowlarr (Knaben + AudioBookBay → Jackett)"
    bash scripts/configure_prowlarr.sh --force-bundled || true
  elif [[ -f scripts/sync_prowlarr_env.sh ]]; then
    bash scripts/sync_prowlarr_env.sh || true
  fi
else
  if [[ -f scripts/configure_prowlarr.sh && -n "$(get_env PROWLARR_API_KEY)" ]]; then
    bash scripts/configure_prowlarr.sh \
      --external-url "$(get_env PROWLARR_URL)" \
      --external-api-key "$(get_env PROWLARR_API_KEY)" || true
  fi
fi
if $USE_BUNDLED; then
  if [[ -f scripts/sync_abs_env.sh ]]; then
    c_cyan "Bootstrapping Audiobookshelf API key + library"
    bash scripts/sync_abs_env.sh || true
  fi
  if [[ -f scripts/sync_kavita_env.sh ]]; then
    c_cyan "Bootstrapping Kavita API key + library"
    bash scripts/sync_kavita_env.sh || true
  fi
  if [[ -f scripts/sync_libraforge_env.sh ]]; then
    c_cyan "Wiring LibraForge URLs"
    bash scripts/sync_libraforge_env.sh || true
  fi
fi
if $USE_NPM; then
  step "Verify Nginx Proxy Manager is listening on :81"
  NPM_OK=false
  for i in $(seq 1 60); do
    code="$(curl -sS --max-time 3 -o /dev/null -w '%{http_code}' "http://127.0.0.1:81/" 2>/dev/null || echo 000)"
    # Any HTTP response means the admin port is bound (NPM may return 200/401/404 while booting).
    if [[ "$code" =~ ^[2345] ]]; then
      NPM_OK=true
      c_green "NPM admin port :81 is listening (HTTP ${code})"
      break
    fi
    sleep 2
  done
  if ! $NPM_OK; then
    c_red "FATAL: Nginx Proxy Manager was enabled but http://127.0.0.1:81 is not listening."
    c_red "library-npm did not publish ports 80/443/81 — remote HTTPS cannot work."
    c_yellow "Debug:"
    echo "  $DOCKER compose --profile npm ps nginx-proxy-manager"
    echo "  $DOCKER compose --profile npm logs --tail=80 nginx-proxy-manager"
    echo "  ss -tln | grep -E ':80|:443|:81'"
    echo "  grep '^COMPOSE_PROFILES=' .env"
    c_yellow "Fix port conflicts (80/443), then re-run this installer or:"
    echo "  cd $TARGET && $DOCKER compose --profile npm up -d nginx-proxy-manager"
    echo "  bash scripts/configure_npm.sh"
    c_yellow "Or skip NPM permanently: LIBRARY_SKIP_NPM=1 (proxy :8085 yourself)."
    exit 1
  fi
  if [[ -f scripts/configure_npm.sh ]]; then
    c_cyan "Configuring Nginx Proxy Manager (admin + proxy hosts via API — no GUI required)"
    if ! bash scripts/configure_npm.sh; then
      c_yellow "configure_npm.sh reported errors — NPM is up; re-run: bash scripts/configure_npm.sh"
    fi
    # Reload APP_URL if configure_npm updated it.
    APP_URL="$(get_env APP_URL)"
    APP_URL="${APP_URL:-$DEFAULT_APP_URL}"
  fi
fi

# Optional: kick off local OL build now that the app container exists.
if [[ "$OL_MODE" == "build" || "$OL_MODE" == "b" ]]; then
  c_cyan "Starting Open Library catalog build inside the app container (background) ..."
  $DOCKER compose exec -d -e PYTHONPATH=/app app \
    python /app/scripts/ol_import_dumps.py || \
    c_yellow "Could not start OL build — run later: docker compose exec app python /app/scripts/ol_import_dumps.py"
fi

$DOCKER compose up -d app || true

# ---------------------------------------------------------------------------
step "Web Push (VAPID) keys [OPTIONAL]"
EXISTING_VAPID="$(get_env VAPID_PUBLIC_KEY)"
if [[ -n "$EXISTING_VAPID" ]]; then
  c_green "VAPID keys already present — leaving unchanged"
elif yes_no "Generate Web Push VAPID keys now (needed for browser notifications)?" "y"; then
  if VAPID_OUT="$($DOCKER compose exec -T app python scripts/generate_vapid.py 2>/dev/null || true)"; then
    PRIV_LINE="$(printf '%s\n' "$VAPID_OUT" | grep '^VAPID_PRIVATE_KEY=' | head -1 || true)"
    PUB_LINE="$(printf '%s\n' "$VAPID_OUT" | grep '^VAPID_PUBLIC_KEY=' | head -1 || true)"
    if [[ -n "$PRIV_LINE" && -n "$PUB_LINE" ]]; then
      # strip KEY= and surrounding quotes for set_env
      PRIV_VAL="${PRIV_LINE#VAPID_PRIVATE_KEY=}"
      PRIV_VAL="${PRIV_VAL#\"}"
      PRIV_VAL="${PRIV_VAL%\"}"
      PUB_VAL="${PUB_LINE#VAPID_PUBLIC_KEY=}"
      set_env VAPID_PRIVATE_KEY "$PRIV_VAL"
      set_env VAPID_PUBLIC_KEY "$PUB_VAL"
      $DOCKER compose up -d app || true
      c_green "VAPID keys written to .env"
    else
      c_yellow "Could not parse VAPID output — run later: docker compose exec app python scripts/generate_vapid.py"
    fi
  else
    c_yellow "VAPID generation skipped — run later: docker compose exec app python scripts/generate_vapid.py"
  fi
else
  c_dim "Skipped — generate later with: docker compose exec app python scripts/generate_vapid.py"
fi

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

# ---------------------------------------------------------------------------
step "Post-install health report"
_ok=$'\033[32mOK\033[0m'
_warn=$'\033[33mwarming / unreachable\033[0m'
_bad=$'\033[31mmissing\033[0m'
probe_http() {
  local name="$1" url="$2"
  if curl -fsS --max-time 5 "$url" >/dev/null 2>&1; then
    printf '  %-16s %s\n' "$name" "$_ok"
  else
    local code
    code="$(curl -sS --max-time 5 -o /dev/null -w '%{http_code}' "$url" 2>/dev/null || echo 000)"
    if [[ "$code" =~ ^[23] ]]; then
      printf '  %-16s %s\n' "$name" "$_ok"
    else
      printf '  %-16s %s\n' "$name" "$_warn"
    fi
  fi
}
echo "Host probes (soft — warming services may show yellow):"
if $APP_OK; then
  printf '  %-16s %s\n' "app" "$_ok"
else
  probe_http "app" "http://127.0.0.1:8085/api/health"
fi
if $USE_BUNDLED_PROWLARR; then
  probe_http "prowlarr" "http://127.0.0.1:9696/ping"
else
  printf '  %-16s %s\n' "prowlarr" $'\033[33mexternal\033[0m'
fi
if $USE_BUNDLED_JACKETT; then
  probe_http "jackett" "http://127.0.0.1:9117/"
else
  printf '  %-16s %s\n' "jackett" $'\033[33mexternal\033[0m'
fi
probe_http "flaresolverr" "http://127.0.0.1:8191/"
if $USE_BUNDLED; then
  probe_http "audiobookshelf" "http://127.0.0.1:13378/"
  probe_http "kavita" "http://127.0.0.1:5000/"
  probe_http "libraforge" "http://127.0.0.1:5056/health"
fi
if $USE_NPM; then
  probe_http "npm-admin" "http://127.0.0.1:81/"
  # Soft probe of NPM :80 when a domain or LAN host was configured.
  code80="$(curl -sS --max-time 5 -o /dev/null -w '%{http_code}' "http://127.0.0.1/" 2>/dev/null || echo 000)"
  if [[ "$code80" =~ ^[23] ]]; then
    printf '  %-16s %s\n' "npm-proxy:80" "$_ok"
  else
    printf '  %-16s %s\n' "npm-proxy:80" "$_warn"
  fi
fi
if [[ -f "${TARGET}/data/ol_catalog.db" ]] && [[ "$(wc -c <"${TARGET}/data/ol_catalog.db" 2>/dev/null || echo 0)" -gt 1048576 ]]; then
  _ol_mb=$(awk "BEGIN {printf \"%.0f\", $(wc -c <"${TARGET}/data/ol_catalog.db")/1024/1024}")
  printf '  %-16s %s (%s MB)\n' "ol-catalog" "$_ok" "$_ol_mb"
else
  printf '  %-16s %s\n' "ol-catalog" $'\033[33mabsent (optional)\033[0m'
fi
_jk="$(get_env JACKETT_API_KEY)"
_pk="$(get_env PROWLARR_API_KEY)"
if [[ -n "$_jk" && ! "$_jk" =~ your- ]]; then
  printf '  %-16s %s\n' "jackett-key" "$_ok"
else
  printf '  %-16s %s\n' "jackett-key" "$_bad"
fi
if [[ -n "$_pk" && ! "$_pk" =~ your- ]]; then
  printf '  %-16s %s\n' "prowlarr-key" "$_ok"
else
  printf '  %-16s %s\n' "prowlarr-key" "$_bad"
fi
if [[ -S /var/run/docker.sock ]]; then
  if $DOCKER info >/dev/null 2>&1; then
    printf '  %-16s %s (DOCKER_GID=%s)\n' "docker.sock" "$_ok" "$(get_env DOCKER_GID)"
  else
    printf '  %-16s %s\n' "docker.sock" $'\033[33mpresent but permission denied for this user\033[0m'
  fi
else
  printf '  %-16s %s\n' "docker.sock" "$_bad"
fi

if [[ -f "${TARGET}/data/app.db" ]]; then
  c_yellow "Existing data/app.db found — first-run admin create only appears when there are zero users."
  c_yellow "To reset first-run: stop the stack, delete data/app.db (+ -wal/-shm), then docker compose up -d."
fi

c_green ""
c_green "Install complete."
echo ""
echo "Next steps:"
echo "  1. Open ${APP_URL%/}/login  (or http://<host>:8085/login)"
echo "  2. Create the admin account (shown automatically when the DB has zero users)"
echo "  3. Create library + offline PIN, then continue to /admin/setup"
if $USE_BUNDLED; then
  echo "     Stack step should show Using bundled stack (keys already synced) — Continue"
  echo "  4. Optional: Audible login (Metadata/Chapter Forge), debrid, Hardcover, OpenRouter"
  echo "  5. Optional Mullvad later: WireGuard keys + add vpn to COMPOSE_PROFILES"
else
  echo "     Stack step: ABS / Kavita / LibraForge presets + soft health probes"
  echo "  4. ABS: confirm audiobook staging (default .unorganized) is ignored"
  echo "  5. Kavita: exclude ebook staging folder (default unorganized)"
  echo "  6. Optional LibraForge sibling: bash scripts/install_libraforge.sh (docs/libraforge.md)"
  echo "  7. Optional Mullvad later: WireGuard keys + COMPOSE_PROFILES=vpn"
fi
echo ""
echo "Indexers (auto-configured when bundled):"
echo "  - Jackett: AudioBookBay → $(get_env JACKETT_URL)"
echo "  - Prowlarr: Knaben + AudioBookBay Torznab → $(get_env PROWLARR_URL)"
echo "  - Re-run anytime: bash scripts/configure_jackett.sh && bash scripts/configure_prowlarr.sh"
if [[ "$OL_MODE" == "build" || "$OL_MODE" == "b" ]]; then
  echo "  - Open Library: local build running in background (docker compose logs -f app)"
elif [[ -f data/ol_catalog.db ]]; then
  echo "  - Open Library: data/ol_catalog.db present"
else
  echo "  - Open Library: skipped (optional) — Admin → Catalog later if you want a local OL DB"
fi
if $USE_NPM; then
  echo ""
  echo "Nginx Proxy Manager:"
  echo "  - Admin UI: http://<host>:81  (email/password from prompts / .env NPM_ADMIN_*)"
  if [[ -n "$(get_env NPM_DOMAIN)" ]]; then
    echo "  - Library proxy: $(get_env APP_URL)  (upstream app:8080 on compose network)"
    if [[ -n "$(get_env NPM_LETSENCRYPT_EMAIL)" ]]; then
      echo "  - DNS: point A/AAAA for $(get_env NPM_DOMAIN) at this host before LE succeeds"
      echo "  - Re-run: bash scripts/configure_npm.sh  (idempotent — refreshes certs/hosts)"
    else
      echo "  - HTTP only — add NPM_LETSENCRYPT_EMAIL to .env and re-run configure_npm.sh for HTTPS"
    fi
  else
    echo "  - LAN proxy hosts created for hostname/IP on :80 (also use :8085 directly)"
    echo "  - Set NPM_DOMAIN (+ NPM_LETSENCRYPT_EMAIL) then: bash scripts/configure_npm.sh"
  fi
else
  echo ""
  echo "Reverse proxy: skipped. Remote HTTPS needs NPM/Caddy/nginx → http://127.0.0.1:8085"
fi
echo ""
echo "Notes:"
echo "  - Profile bundled-media = ABS (:13378) + Kavita (:5000) + LibraForge (:5056)"
echo "  - Profile npm = Nginx Proxy Manager (library-npm) on 80/443/81 — default Yes; No only if 80/443 already taken"
echo "  - Jackett/Prowlarr/Flare are core sidecars; external URL skip paths stop the local container"
echo "  - FlareSolverr resource limits are in docker-compose.yml (safe defaults for Pi/laptop)"
echo "  - Re-run this script anytime — it updates selected .env keys without wiping secrets"
echo "  - Docs: docs/ubuntu-server-install.md"
echo ""
echo "Stack dir: $TARGET"
echo "Logs:      cd \"$TARGET\" && $DOCKER compose logs -f app"
if $USE_BUNDLED; then
  _ports="app 8085 | ABS 13378 | Kavita 5000 | LibraForge 5056 | prowlarr 9696 | flare 8191 | jackett 9117"
else
  _ports="app 8085 | prowlarr 9696 | flaresolverr 8191 | jackett 9117"
fi
if $USE_NPM; then
  _ports="${_ports} | npm 80/443/81"
fi
echo "Ports:     $_ports"
echo ""
