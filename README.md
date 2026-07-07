# Audiobook Request System

A self-hosted web app for searching and requesting audiobooks. Searches torrent indexers via Prowlarr, downloads through Real-Debrid, and automatically adds books to your Audiobookshelf library.

Live at: `https://library.freiverse.com`

## Features

- **Search** audiobooks across multiple torrent indexers via Prowlarr
- **One-click requests** that route through Real-Debrid for fast, seedbox-free downloads
- **Automatic library integration** -- files are organized and an Audiobookshelf scan is triggered
- **Account request system** -- new users request access; admin approves via the web UI
- **Push notifications** -- get notified when a requested book finishes (users) or when admin events occur (admins: new account requests, download status, errors)
- **Real-time status** -- WebSocket-powered live updates as your book moves through the pipeline
- **Admin panel** -- manage users, review account requests, monitor system health

## Architecture

```
User Browser
    |
    v (HTTPS)
nginx (library.freiverse.com)
    |
    v (proxy_pass :8080)
FastAPI Backend ──> Prowlarr ──> Torrent Indexers
    |                               
    v                               
Real-Debrid API                     
    |                               
    v (download files)              
Pi Storage (/audiobooks)            
    |                               
    v (library scan)                
Audiobookshelf                      
```

## Prerequisites

- Raspberry Pi (or any Linux server) with Docker and Docker Compose
- A Real-Debrid premium account ([get API token](https://real-debrid.com/apitoken))
- Audiobookshelf already running
- DNS A record for `library.freiverse.com` pointing to your server

## Quick Start

### 1. Clone and configure

```bash
git clone <this-repo> audiobook-request
cd audiobook-request
cp .env.example .env
# Edit .env with your API keys and paths
nano .env
```

### 2. Start the services

```bash
docker compose up -d
```

This starts:
- **audiobook-request** on `127.0.0.1:8080` (the web app)
- **prowlarr** on `127.0.0.1:9696` (indexer manager)

### 3. Configure Prowlarr

1. Open `http://your-pi-ip:9696` in your browser
2. Set up authentication when prompted
3. Go to **Indexers** and add your preferred torrent indexers
4. Go to **Settings > General** and copy the API Key
5. Paste the API key into your `.env` file as `PROWLARR_API_KEY`
6. Restart: `docker compose restart app`

### 4. Set up nginx + SSL

Copy the nginx config to your sites:

```bash
sudo cp nginx/library.freiverse.com.conf /etc/nginx/sites-available/
sudo ln -s /etc/nginx/sites-available/library.freiverse.com.conf /etc/nginx/sites-enabled/
```

Get an SSL certificate:

```bash
sudo certbot --nginx -d library.freiverse.com
```

Reload nginx:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

### 5. (Optional) Tailscale Funnel for blocked networks

If your main domain is blocked (e.g. at work) but Tailscale URLs work, use [Tailscale Funnel](docs/TAILSCALE_FUNNEL.md) to expose the site via `https://pihole.your-tailnet.ts.net`:

```bash
./tailscale-funnel-setup.sh
```

### 6. First-run setup

1. Visit your site (e.g. `https://library.freiverse.com` or your Tailscale funnel URL)
2. You'll be prompted to create an admin account (first user = admin)
3. Log in and start searching!

## Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `SECRET_KEY` | Random string for JWT signing | Yes |
| `DATABASE_URL` | SQLite connection string | Yes (has default) |
| `APP_URL` | Public URL of the app | Yes |
| `PROWLARR_URL` | Prowlarr base URL | Yes |
| `PROWLARR_API_KEY` | Prowlarr API key | Yes |
| `REAL_DEBRID_API_TOKEN` | Real-Debrid API token | Yes |
| `ABS_URL` | Audiobookshelf base URL | Yes |
| `ABS_API_KEY` | Audiobookshelf API key | Yes |
| `ABS_LIBRARY_ID` | Audiobookshelf library ID | Yes |
| `NYT_API_KEY` | NYT Books API key (for real bestsellers in Trending) | No (free at developer.nytimes.com) |
| `VAPID_PRIVATE_KEY` | Web Push VAPID private key (PEM) | No (for push notifications) |
| `VAPID_PUBLIC_KEY` | Web Push VAPID public key (base64url) | No (for push) |
| `AUDIOBOOK_DIR` | Path to audiobook storage | Yes |

## Development

### Backend

```bash
cd backend
python -m venv venv
source venv/bin/activate  # or venv\Scripts\activate on Windows
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8080
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

The Vite dev server proxies `/api` requests to the backend at `localhost:8080`.

### Build frontend for production

```bash
cd frontend
npm run build
# Output goes to ../backend/static/
```

## Database Backups

`data/app.db` holds the torrent cache, users, listening progress, and admin settings. Back it up nightly on the Pi with the included script (uses SQLite's online backup, safe while the app is running):

```bash
chmod +x "/opt/stacks/Library Site/scripts/backup_db.sh"
crontab -e
# add:
15 3 * * * "/opt/stacks/Library Site/scripts/backup_db.sh" >> "/opt/stacks/Library Site/data/backups/backup.log" 2>&1
```

Backups land in `data/backups/` as gzipped snapshots and are pruned after 14 days (override with `RETENTION_DAYS`).

## CI Checks

Before deploying, run the pre-deploy check suite (backend tests, frontend type-check, Android compile):

```powershell
.\scripts\check.ps1            # all checks
.\scripts\check.ps1 -SkipAndroid  # skip the slow gradle step
```

## Download Pipeline

1. User searches for an audiobook (search proxied to Prowlarr)
2. User clicks "Request" on a result
3. Backend sends the magnet link to Real-Debrid
4. Background task polls Real-Debrid until the torrent is downloaded
5. Files are unrestricted and downloaded to `{AUDIOBOOK_DIR}/{Author}/{Title}/`
6. Audiobookshelf library scan is triggered
7. User gets a real-time WebSocket notification that their book is ready
8. If push notifications are enabled, user gets a device notification when the app is closed

## Push Notifications

Users can enable push notifications on the **My Requests** page to get alerted when a requested book finishes downloading. Admins can enable push on the **Admin** page to receive notifications for new account requests, download status, and errors.

**Setup:** Generate VAPID keys and add to `.env`:

```bash
pip install py-vapid
python scripts/generate_vapid.py
# Copy the output into your .env file
```

## Account Request Flow

1. Visitor clicks "Request an Account" on the login page
2. Fills out username and optional reason
3. Admin gets a Discord notification with a link to the admin panel
4. Admin approves or denies from the Approvals tab
5. If approved, a temporary password is generated
6. Visitor checks their status using the token they received

## Tech Stack

- **Backend**: Python 3.11, FastAPI, SQLAlchemy (async), SQLite
- **Frontend**: React 18, Vite, TailwindCSS, TanStack Query
- **Infrastructure**: Docker Compose, nginx, Let's Encrypt
- **Integrations**: Prowlarr, Real-Debrid, Audiobookshelf, Discord Webhooks
