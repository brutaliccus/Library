# Library Site

Self-hosted audiobook + ebook request library: search indexers, download via debrid, and sync into Audiobookshelf / Kavita.

## Features

- Search audiobooks and ebooks across torrent indexers (Prowlarr / Jackett)
- One-click requests through Real-Debrid and/or TorBox
- Automatic library integration (Audiobookshelf + Kavita)
- Account request / approval flow
- Web + Capacitor Android client (optional Android Auto support)
- Push notifications and live WebSocket status
- Admin panel with first-run setup wizard and runtime Config

## Prerequisites

- Linux host (or any Docker-capable machine) with Docker Compose
- A debrid account (Real-Debrid and/or TorBox)
- Audiobookshelf and/or Kavita already running (or reachable on your LAN)
- A public hostname (or Tailscale Funnel) if you want HTTPS off-LAN

## Quick start

### Install script

```bash
chmod +x scripts/install_library.sh
./scripts/install_library.sh /opt/library-site
```

The script clones/updates the repo, writes `.env`, creates media mount dirs, and runs `docker compose up -d --build`.

### Manual

```bash
git clone <this-repo> library-site
cd library-site
cp .env.example .env
# Edit APP_URL, SECRET_KEY, media host paths, and any API keys
mkdir -p media/audiobooks media/ebooks media/openlibrary data
docker compose up -d --build
```

This builds the frontend inside Docker and starts:

- **app** on `127.0.0.1:8085`
- **prowlarr**, **jackett**, **flaresolverr**, **gluetun** (optional Mullvad HTTP proxy for AudioBook Bay)

### First-run wizard

1. Open the site → create the **admin** account
2. Go to **`/admin/setup`** — libraries, Prowlarr, debrid defaults, catalog APIs, scraper mode (RSS-only by default)
3. Create or join a **library group** at `/onboarding`
4. Later: **Admin → Config** for every runtime setting / API key

### Media mounts

Compose reads host paths from `.env`:

| Variable | Default | Mounted as |
|----------|---------|------------|
| `AUDIOBOOK_HOST_DIR` | `./media/audiobooks` | `/audiobooks` |
| `EBOOK_HOST_DIR` | `./media/ebooks` | `/ebooks` |
| `OPENLIBRARY_HOST_DIR` | `./media/openlibrary` | `/openlibrary` |

Point these at your existing library folders if you already have them.

### nginx + TLS (optional)

Example configs live in `nginx/`. Point your reverse proxy at `127.0.0.1:8085`.

### Tailscale Funnel (optional)

See [docs/TAILSCALE_FUNNEL.md](docs/TAILSCALE_FUNNEL.md).

## Environment variables

See [`.env.example`](.env.example). Most integration keys can also be set at runtime in **Admin → Config** (DB override with env fallback).

| Variable | Description | Required |
|----------|-------------|----------|
| `SECRET_KEY` | JWT signing secret | Yes |
| `DATABASE_URL` | SQLite URL | Has default |
| `APP_URL` | Public URL of the app | Yes |
| `PROWLARR_URL` / `PROWLARR_API_KEY` | Indexer manager | Yes (or Admin → Config) |
| `REAL_DEBRID_API_TOKEN` / `TORBOX_API_TOKEN` | Server debrid defaults | Recommended |
| `ABS_*` / `KAVITA_*` | Library connections | At least one |
| `HARDCOVER_API_KEY` / `NYT_API_KEY` / `ISBNDB_API_KEY` | Catalog APIs | Optional |
| `VAPID_*` | Web Push (`python scripts/generate_vapid.py`) | Optional |
| `AUDIOBOOK_DIR` / `EBOOK_DIR` | In-container paths | Defaults match mounts |
| `ABB_RSS_ONLY` | Seed RSS-only scraper mode | Recommended `true` |

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
npm run build   # → backend/static/ (used only for local/dev; Docker builds this in-image)
```

### Checks

```powershell
.\scripts\check.ps1 -SkipAndroid
```

## Database backups

`data/app.db` holds users, cache, progress, and settings. Install a nightly cron:

```bash
bash scripts/install_backup_cron.sh
```

## Download pipeline

1. User searches (Prowlarr / cache)
2. User requests a release
3. Backend sends the magnet to Real-Debrid / TorBox
4. Files download into `{AUDIOBOOK_DIR|EBOOK_DIR}/{Author}/{Title}/`
5. Audiobookshelf / Kavita scan is triggered
6. WebSocket + optional push notify the user

## Account request flow

1. Visitor requests an account on the login page
2. Admin approves or denies in the Admin panel
3. Approved users get a temporary password and can join a library group

## Tech stack

- **Backend**: Python 3.11, FastAPI, SQLAlchemy (async), SQLite
- **Frontend**: React, Vite, Tailwind CSS, TanStack Query, pdf.js
- **Infrastructure**: Docker Compose, optional nginx / Tailscale
- **Integrations**: Prowlarr, Jackett, FlareSolverr, Real-Debrid, TorBox, Audiobookshelf, Kavita

## Android app

See [docs/android-app.md](docs/android-app.md). Build from `frontend/` with Capacitor after pointing `capacitor.config.ts` at your `APP_URL`.
