# Library

Self-hosted audiobook and ebook library with catalog browsing, torrent discovery, debrid downloads, and in-app listening/reading.

Search books across Google Books, Open Library, Hardcover, NYT, and ISBNdb. Find releases through a local indexer cache (AudioBook Bay, Knaben, Prowlarr/Jackett). Download via Real-Debrid and/or TorBox (or Anna's Archive for ebooks). Files land in your Audiobookshelf and Kavita libraries. Listen and read in the web app or Android client with progress sync, offline cache, and live status.

**Repository:** [github.com/brutaliccus/Library](https://github.com/brutaliccus/Library)

---

## Features

### Catalog & discovery
- Book search and detail pages with covers, descriptions, ratings, and series
- Metadata from Google Books, a local Open Library catalog database, Hardcover, NYT bestsellers, and ISBNdb
- Home shelves: curated lists, trending, new releases (daily snapshots persist across restarts)
- Genre hubs and series drill-down pages
- Availability badges: in your library, in the indexer cache, and/or cached on debrid
- Optional magnet browser extension — [browser-extension/README.md](browser-extension/README.md)

### Search & indexer cache
- Cache-first search against a local torrent index (fast, no indexer hammering)
- **Shipped warm cache** (~36 MB compressed / ~150 MB on import) so cached books show on first boot — see [Indexer cache seed](#indexer-cache-seed)
- Live Prowlarr search when you need fresher results (SSE live-stream)
- AudioBook Bay integration (RSS ingest + Jackett live search); Find Downloads auto-runs ABB search
- Knaben RSS ingest (optional full crawl)
- Anna's Archive ebook search and download (optional membership cookie)
- Background scraper matches releases to catalog volumes and refreshes debrid "cached" badges

### Downloads & debrid
- One-click requests through Real-Debrid and/or TorBox (TorBox optional)
- Provider pick: unique cache wins; both/neither → user preferred debrid
- Server-wide defaults plus per-library-group API keys
- Smart-stream from debrid without waiting for a full library ingest
- Download pipeline with WebSocket progress on the Requests page

### Audiobook pipeline (LibraForge)
- Staging under `/audiobooks/.unorganized/` → Metadata → M4B → Chapter Forge (ASIN) → Folder Forge → ABS
- M4B: Library Site runs a **global encode queue (concurrency 1)** shared by auto-forge, Quick Review, and continue-forge — waiters show request badge **Queued for M4B**, active encode shows **Converting M4B**
- Per-run ffmpeg/m4b-tool workers: `LIBRAFORGE_M4B_JOBS=1` (default; keep at 1 on a Pi)
- Admin Quick Review: Files → Metadata → M4B → Chapters → Continue; Re-run; Open LibraForge
- Reject deletes staging; staging file browser on quarantined requests  
  Details: [docs/libraforge.md](docs/libraforge.md)

### Ebook pipeline
- Staging under `/ebooks/unorganized/` → identify → Author/Series/Title → Kavita  
  Details: [docs/ebooks.md](docs/ebooks.md)

### My Library
- Tabs: Audiobooks / eBooks / My Collection / Downloads; Store nav; Continue shelves on My Library
- ABS/local metadata is source of truth for series, author, genre, and sequence (Hardcover genres fill empty only)
- Broad library search across metadata fields; shelf cache v6
- Filter UI: By Genre / Series / Author above full-width dropdowns
- Offline unlock (PIN/biometric), Save offline, Downloaded tab, offline My Library

### Libraries
- Audiobookshelf: browse, play (proxied stream), progress sync; scan = clean orphans (no metadata rewrite); ASIN protect
- Kavita: ebook collections, covers, PDF/EPUB reader endpoints
- Shared on-disk media across library groups; debrid credentials are group-scoped

### Listening & reading
- Full audiobook player with mini-player, scrubbing, and media session controls
- In-app ebook reader (PDF + EPUB) with reading progress
- Offline audio/ebook cache (web + Android WebView)
- Continue listening / continue reading on My Library and home

### Accounts & library groups
- JWT auth; first account becomes admin (fresh install only)
- **Invite-only signup** — friends open `/join/CODE`, pick username/password, and join immediately (no admin approval)
- Offline PIN onboarding for new accounts
- Library groups with invite links (code + server URL for Android deep link)
- Admins: user cards + search, disable / reset password
- Per-user preferred debrid provider and private mode

### Admin
- Left-nav console grouped into **Operations**, **Library**, and **System** (legacy `?tab=` aliases still remap)
- First-run setup wizard at `/admin/setup` (libraries, pipelines, debrid, staging checklist, catalog, APK, scraper)
- **Operations**: Overview (health probes, Scan ABS & clean orphans, Open LibraForge), Requests, Users
- **Library**: Discovery (scraper enable/run, RSS vs deep-crawl, debrid refresh, catalog relink), Catalog (API keys + Open Library build/update/schedule)
- **System**: Pipelines (LibraForge / ebook toggles and scores), Integrations (debrid, VPN, OpenRouter, secrets), Settings (core, libraries, indexers, storage paths)
- All Requests: Requested by, cover → store, staging browser, Quick Review / Re-run / reject; M4B badge **Queued for M4B** vs **Converting M4B**
- Push notifications (Disable → Enable to refresh a stuck browser subscription)
- RSS-only scraper defaults (Pi-friendly); optional high-usage FlareSolverr crawls

### Notifications
- Web Push (VAPID) for download completion and admin events
- Availability alerts: watch a book that isn't in cache yet; get notified when it appears
- Real-time WebSocket updates for active downloads
- Native notifications on the Android app

### Android app
- Capacitor APK with bundled UI — users enter their Library URL on sign-in (editable in Settings)
- One prebuilt APK works with any self-hosted instance (GitHub Releases)
- Lock-screen / notification media controls (±15s seek, idle resume)
- Android Auto: Continue Listening and A-Z library browse  
  See [docs/android-app.md](docs/android-app.md)

### Optional networking
- Mullvad WireGuard via gluetun - HTTP proxy used **only** for AudioBook Bay egress
- Jackett, Knaben, and the rest of the stack stay on the LAN
- Register WireGuard from Admin (or `scripts/mullvad_register_wg.py`)
- Example nginx and Nginx Proxy Manager configs in `nginx/`
- Optional Tailscale Funnel exposure: [docs/TAILSCALE_FUNNEL.md](docs/TAILSCALE_FUNNEL.md)

---

## Architecture

```mermaid
flowchart TB
  client[Browser / Android]
  proxy[Reverse proxy<br/>nginx / NPM]
  app[app<br/>FastAPI + React SPA]
  prowlarr[Prowlarr]
  jackett[Jackett]
  flare[FlareSolverr]
  gluetun[gluetun<br/>Mullvad HTTP proxy]
  indexers[Torrent indexers]
  data[(data/<br/>SQLite + OL catalog)]
  audio[/audiobooks/]
  ebooks[/ebooks/]
  abs[Audiobookshelf]
  kavita[Kavita]

  client --> proxy --> app
  app --> prowlarr --> indexers
  app --> jackett --> indexers
  app --> flare
  flare -->|"ABB egress only"| gluetun
  app --> data
  app --> audio --> abs
  app --> ebooks --> kavita
```

### Request paths

| Flow | What happens |
|------|----------------|
| **First admin** | Empty DB → create admin on `/login` → `/onboarding` create library (invite code) → optional `/admin/setup` |
| **Invite signup** | Open `/join/CODE` → username/password → account + library membership (no approval) |
| **Catalog browse** | SPA -> `/api/books/*` -> Google Books / Open Library DB / Hardcover / NYT / ISBNdb |
| **Release search** | SPA -> `/api/search` -> local indexer cache first; optional live Prowlarr / Jackett ABB / AA |
| **Download (audio)** | SPA -> `/api/requests` -> RD/TorBox -> `/audiobooks/.unorganized` -> LibraForge (Metadata/M4B/Chapters/Folder) -> ABS scan -> WebSocket + push |
| **Download (ebook)** | SPA -> `/api/requests` -> debrid/AA -> `/ebooks/unorganized` -> identify/organize -> Kavita scan |
| **Scraper** | Background job (ABB/Knaben RSS by default) -> indexer cache -> catalog match -> debrid preload badges |
| **Listen** | SPA -> `/api/stream/*` -> ABS proxy or debrid smart-stream; progress sync; offline cache |
| **Read** | SPA -> `/api/library/reader/*` -> Kavita chapters / PDF; in-app `PdfViewer` / EPUB reader |
| **My Library** | SPA -> `/api/library/*` -> ABS/Kavita local metadata (series/author/genre/sequence); broad search + shelf filters |

### Stack

| Layer | Tech |
|-------|------|
| Backend | Python 3.11, FastAPI, SQLAlchemy (async), SQLite, Alembic |
| Frontend | React, Vite, Tailwind CSS, TanStack Query, pdf.js |
| Mobile | Capacitor Android (+ Android Auto MediaBrowserService) |
| Infra | Docker Compose, optional nginx / Tailscale |
| Integrations | Prowlarr, Jackett, FlareSolverr, Real-Debrid, TorBox, Audiobookshelf, Kavita, Mullvad/gluetun, optional OpenRouter LLM assist |

### Docker services

| Service | Role |
|---------|------|
| **app** | API + SPA; scraper; download pipeline |
| **prowlarr** | Indexer manager |
| **jackett** | Indexer bridge (including AudioBook Bay) |
| **flaresolverr** | Cloudflare challenge solver for ABB / AA paths |
| **gluetun** | Mullvad WireGuard HTTP proxy for ABB egress only |

---

## Prerequisites

- **Docker** with Compose v2
  - Linux / Raspberry Pi: Docker Engine + Compose plugin
  - Windows: [Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/) (WSL2 backend recommended), running before install
- Git (for clone)
- Free host ports: **8085** (app), **9696** (Prowlarr), **8191** (FlareSolverr), **9117** (Jackett)
- A Real-Debrid and/or TorBox account (can add after first boot)
- Audiobookshelf and/or Kavita reachable from the host (optional at install time)
- A public HTTPS hostname (or Tailscale Funnel) if you want off-LAN access and push/Android

**Mullvad / gluetun is optional.** Fresh installs start without the VPN sidecar so the stack is healthy before you add WireGuard keys. Enable later by adding `vpn` to `COMPOSE_PROFILES` (e.g. `bundled-media,vpn`) and `ABB_PROXY_URL=http://gluetun:8888` (see `.env.example`).

**Bundled media is the default for new installs.** Profile `bundled-media` starts Audiobookshelf, Kavita, and LibraForge on the same Docker network, wires internal URLs into `.env`, and bootstraps API keys (same idea as Jackett/Prowlarr sync). Existing Pi hosts that already use external ABS/Kavita/LF keep those URLs — leave the profile off. Expect ~1–2 GB extra RAM for the media sidecars.

---

## Quick start

### Windows (Docker Desktop)

```powershell
git clone https://github.com/brutaliccus/Library.git library
cd library
powershell -ExecutionPolicy Bypass -File .\scripts\install_library.ps1
```

Unattended (CI / test box) — includes bundled media:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_library.ps1 -Target "C:\dev\Library" -NonInteractive
```

Opt out of bundled ABS/Kavita/LibraForge:

```powershell
.\scripts\install_library.ps1 -NonInteractive -SkipBundledMedia
```

### Linux / Raspberry Pi

```bash
git clone https://github.com/brutaliccus/Library.git library
cd library
chmod +x scripts/install_library.sh
./scripts/install_library.sh /opt/library
```

Or run the installer against a fresh target; it clones from this repo by default:

```bash
curl -fsSL https://raw.githubusercontent.com/brutaliccus/Library/main/scripts/install_library.sh | bash
```

The installers write `.env`, create media/staging directories, ensure `seed/indexer_cache.db.gz` is present (repo / Git LFS / `data-seed` release download), apply **RSS-only** scraper defaults (unless you opt into deep crawls), set `COMPOSE_PROFILES=bundled-media` for new installs (clone LibraForge into gitignored `./libraforge`), build the stack, wait for `/api/health`, then sync Jackett/Prowlarr/ABS/Kavita/LibraForge into `.env`. **Mullvad is optional** (especially on Windows). After create-admin → create-library → offline PIN, open **`/admin/setup`** — the Stack step should show **Using bundled stack** with green probes (Continue without pasting API keys). Soft warnings only if you opt out of the bundled profile. On Linux they can also install nightly DB backup / OL catalog cron; on Windows use Task Scheduler or **Admin → Catalog** schedule instead.

### Indexer cache seed

| | |
|---|---|
| **File** | `seed/indexer_cache.db.gz` |
| **Size** | ~36 MB compressed → ~150 MB when imported into `data/app.db` |
| **Contents** | Sanitized torrent/indexer cache + catalog match tables (no users or API keys) |
| **Import** | Automatic on first boot when `indexer_torrents` is empty (`app/services/indexer_seed.py`) |
| **Distribution** | Shipped in git + Docker image; installers also try Git LFS, then the GitHub Release tag [`data-seed`](https://github.com/brutaliccus/Library/releases/tag/data-seed) (`indexer_cache.db.gz` / `seed-cache`) |
| **Optional** | If the download fails, install still succeeds — the cache starts empty and fills via scrapers/indexers |

Rebuild: `python scripts/export_indexer_seed.py /path/to/app.db ./seed` (see `seed/README.md`).

### Manual (any OS with Docker)

```bash
git clone https://github.com/brutaliccus/Library.git library
cd library
cp .env.example .env
# Edit APP_URL, SECRET_KEY, media paths, and any API keys you already have
mkdir -p media/audiobooks media/ebooks media/openlibrary data prowlarr-config jackett-config
# Windows PowerShell: New-Item -ItemType Directory -Force media/audiobooks, media/ebooks, media/openlibrary, data, prowlarr-config, jackett-config
docker compose up -d --build
```

The image **builds the frontend in Docker** — you do not need Node on the host for production.

App listens on **`http://127.0.0.1:8085`**.

| Service | Port | Notes |
|---------|------|--------|
| Library app | `8085` → container `8080` | Always |
| Prowlarr | `9696` | Always |
| FlareSolverr | `8191` | Always |
| Jackett | `9117` | Always |
| Audiobookshelf | `13378` → container `80` | `bundled-media` profile |
| Kavita | `5000` | `bundled-media` profile |
| LibraForge | `5056` | `bundled-media` profile |

Internal URLs used by the app container (written by installers): `http://audiobookshelf:80`, `http://kavita:5000`, `http://libraforge:5056`.

### First-run wizard

1. Open **`/login`** (or the site root — with zero users you are redirected there) → create the **admin** account  
   - Only when `GET /api/auth/setup-required` reports `setup_required: true` (user count is 0).  
   - Reusing an existing `data/app.db` with users skips this step; delete `data/app.db` (+ `-wal`/`-shm`) for a clean first-run.  
2. **Onboarding** → create your library (name + debrid keys). That generates the invite link.  
3. **`/admin/setup`** — with bundled-media, Stack shows **Using bundled stack** (keys already synced); otherwise enter external ABS/Kavita/LF. Then indexers, debrid (TorBox optional), staging checklist, catalog APIs, Android APK repo, scraper mode  
4. Confirm folder conventions: ABS ignores `.unorganized`; Kavita excludes `unorganized` (bundled bootstrap sets Kavita exclude patterns)  
5. Share the **invite link** from Settings (`/join/CODE`). Friends open it (Android app if installed, otherwise the site), set username/password + offline PIN, and join — no approval step.  
6. Anytime later use the Admin left-nav: **Overview** / **Requests** / **Users**, **Discovery** / **Catalog** (Open Library build + schedule), **Pipelines**, **Integrations** (optional OpenRouter LLM assist — off by default: Metadata Forge / ebook identify retry, multi-book split, file prune, ASIN recovery; shows per-key credit usage), and **Settings**

### Media mounts

Compose reads host paths from `.env`:

| Variable | Default | Container path |
|----------|---------|----------------|
| `AUDIOBOOK_HOST_DIR` | `./media/audiobooks` | `/audiobooks` |
| `EBOOK_HOST_DIR` | `./media/ebooks` | `/ebooks` |
| `OPENLIBRARY_HOST_DIR` | `./media/openlibrary` | `/openlibrary` |

Point these at existing library folders if you already have them. In-container paths used by the app are `AUDIOBOOK_DIR=/audiobooks` and `EBOOK_DIR=/ebooks`.

**Staging conventions**

| Pipeline | Staging path | Library ignore |
|----------|--------------|----------------|
| Audiobooks (LibraForge) | `/audiobooks/.unorganized/req_{id}_…` | ABS skips dot folders (plus `.ignore`) |
| Ebooks | `/ebooks/unorganized/req_{id}_…` | Kavita must exclude `unorganized` |

Set `PUID`/`PGID` to `1000` when sharing the audiobook mount with LibraForge (avoids Permission denied on forge writes).

### Reverse proxy

Example configs:

- `nginx/library.example.com.conf` - full site proxy (long timeouts for search streams + WebSockets)
- `nginx/npm-server_proxy.conf` - Nginx Proxy Manager custom locations

Proxy to `http://127.0.0.1:8085`.

---

## Configuration

See [`.env.example`](.env.example) for the full list.

Most integration keys can also be set in **Admin → Settings** / **Integrations** / **Catalog** / **Pipelines** (stored in the DB, env as fallback). `SECRET_KEY` and the VAPID private key stay env-only. Library/staging paths are editable under **Admin → Settings → Storage / Paths** (env fallback; host bind mounts remain compose-only via `AUDIOBOOK_HOST_DIR` / `EBOOK_HOST_DIR`).

| Area | Variables |
|------|-----------|
| Core | `SECRET_KEY`, `DATABASE_URL`, `APP_URL` |
| Indexers | `PROWLARR_URL`, `PROWLARR_API_KEY`, `JACKETT_*`, `FLARESOLVERR_URL` |
| Scraper | `ABB_RSS_ONLY`, `ABB_AUTHOR_CRAWL_ENABLED`, `ABB_LIVE_SEARCH_ENABLED`, Knaben crawl knobs |
| Debrid | `REAL_DEBRID_API_TOKEN`, `TORBOX_API_TOKEN` (optional) |
| Libraries | `ABS_URL`, `ABS_API_KEY`, `ABS_LIBRARY_ID`, `KAVITA_*` |
| LibraForge | `LIBRAFORGE_URL`, `LIBRAFORGE_INTERNAL_URL`, `LIBRAFORGE_PIPELINE_ENABLED`, `LIBRAFORGE_MIN_SCORE`, `LIBRAFORGE_NAMING_TEMPLATE`, `LIBRAFORGE_M4B_JOBS` (per-run workers; app also serializes cross-request M4B to 1) |
| Ebooks | `EBOOK_PIPELINE_ENABLED`, `EBOOK_MIN_SCORE` |
| Catalog | `HARDCOVER_API_KEY`, `NYT_API_KEY`, `ISBNDB_API_KEY`, `GOOGLE_BOOKS_API_KEY`, `AA_ACCOUNT_ID`, `OPENROUTER_*` (optional LLM assist) |
| Mobile | `ANDROID_APK_GITHUB_REPO`, `GITHUB_TOKEN` (optional rate-limit) |
| VPN | `WIREGUARD_PRIVATE_KEY`, `WIREGUARD_ADDRESSES`, `MULLVAD_*`, `ABB_PROXY_URL` |
| Push | `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY` (`python scripts/generate_vapid.py`) |
| Host mounts | `AUDIOBOOK_HOST_DIR`, `EBOOK_HOST_DIR`, `OPENLIBRARY_HOST_DIR`, `PUID`/`PGID` (prefer `1000`), `TZ` |

More detail: [docs/libraforge.md](docs/libraforge.md), [docs/ebooks.md](docs/ebooks.md), [docs/android-app.md](docs/android-app.md).

### Scraper modes

| Mode | Behavior |
|------|----------|
| **RSS-only (default)** | ABB + Knaben RSS ingest; live Jackett ABB search still works; low CPU |
| **Deep crawl (optional)** | ABB author/A-Z Flare crawls, ABB live Flare deep search, Knaben full category crawl - high usage on a Pi |

Prefer RSS-only unless you know you need the deeper coverage.

### Open Library catalog (optional)

A local SQLite catalog avoids hammering live Open Library APIs during scrape/match. It is **not** required for a working install (the indexer cache seed is enough to search releases).

**Admin → Catalog** shows catalog readiness, dump presence, and whether newer remote dumps are available (admins get a notification when dumps change). Use **Generate catalog** / **Update catalog** for an immediate multi-GB download + rebuild, or **schedule** dump download + rebuild for a chosen local time (persists across restarts; cancel/reschedule anytime). That runs the same import as:

```bash
python scripts/ol_import_dumps.py --help
# Optional host cron (in addition to in-app schedule):
bash scripts/install_ol_catalog_cron.sh
```

Expect multi-GB downloads and a multi-GB finished DB (much larger if you include editions). On a Pi this often takes many hours—prefer scheduling off-peak. Mount dump/working dirs via `OPENLIBRARY_HOST_DIR`.

---

## Operations

### Database & backups

- SQLite DB: `data/app.db` (users, indexer cache, progress, settings, alerts)
- Alembic migrations run automatically on startup
- Nightly backup cron: `bash scripts/install_backup_cron.sh` -> `data/backups/`

### Useful scripts

| Script | Purpose |
|--------|---------|
| `scripts/install_library.sh` | Full host bootstrap on Linux/Pi (pipelines, staging dirs, PUID 1000, APK repo) |
| `scripts/install_library.ps1` | Same bootstrap for Windows + Docker Desktop (`-NonInteractive` supported) |
| `scripts/install_libraforge.sh` | Sibling LibraForge stack (shared `/audiobooks` mount) |
| `scripts/generate_vapid.py` | Web Push keypair |
| `scripts/backup_db.sh` / `install_backup_cron.sh` | DB backups (Linux cron) |
| `scripts/sync_jackett_env.sh` / `.ps1` | Copy Jackett API key into `.env` (repo-relative) |
| `scripts/sync_prowlarr_abb_indexer.sh` | Wire Prowlarr -> Jackett ABB |
| `scripts/mullvad_register_wg.py` | Register Mullvad WireGuard keys |
| `scripts/ol_import_dumps.py` / `refresh_ol_catalog.sh` | Open Library catalog |
| `scripts/check.ps1` | Pre-deploy tests + typecheck (dev) |

### Health

**Admin → Overview** probes Real-Debrid, TorBox, Audiobookshelf, Kavita, LibraForge, Prowlarr, Jackett, FlareSolverr, Mullvad, Knaben, Open Library catalog, NYT, and disk space. **Scan ABS & clean orphans** runs scan + orphan cleanup only (no Quick Match / title rewrite). **Open LibraForge** uses `LIBRAFORGE_URL`.

---

## Development

### Backend

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8080
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Vite proxies `/api` and `/ws` to `localhost:8080`.

### Production frontend build (local)

```bash
cd frontend
npm run build   # -> backend/static/
```

Docker production builds use a multi-stage image and do not require this step on the host.

### Checks

```powershell
.\scripts\check.ps1 -SkipAndroid
```

### Android

```bash
cd frontend
npm run android:sync
npm run android:open
```

Users enter their Library HTTPS URL in the app (not at build time). Details: [docs/android-app.md](docs/android-app.md).

---

## Project layout

```
app/                 FastAPI application (routers, services, models)
frontend/            React SPA + Capacitor Android project
browser-extension/   Magnet → request queue (Chrome/Brave MV3)
docs/                LibraForge, ebooks, Android, Tailscale notes
migrations/          Alembic schema versions
seed/                Warm indexer-cache DB (gzipped; auto-imported on first boot)
nginx/               Reverse-proxy examples
scripts/             Install, backup, catalog, indexer helpers
tests/               Pytest suite
docker-compose.yml   App + Prowlarr + Jackett + FlareSolverr + gluetun
Dockerfile           Multi-stage: Node frontend build -> Python app image
.env.example         Documented environment template
```

---

## License

Use and modify for your own self-hosted deployment. Respect the terms of third-party services you connect (debrid providers, indexers, catalog APIs, library apps).
