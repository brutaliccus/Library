# Ubuntu Server 24.04 - Library Site install

Guided path for a **fresh** host (e.g. spare laptop). Does **not** migrate or stop the production Pi.

## Prerequisites

| Tool | Why |
|------|-----|
| Ubuntu Server 24.04 LTS | Recommended OS (OpenSSH enabled at install) |
| Docker Engine + Compose plugin | Runs the stack |
| Git | Clone / update the repo |
| curl, openssl, python3 | Installer + secrets + NPM API JSON |
| Free ports | `8085`, `9696`, `8191`, `9117` (+ `13378`, `5000`, `5056` if bundled-media; **`80`/`443`/`81` if NPM**) |

Optional later: Mullvad WireGuard for ABB egress. Remote HTTPS is covered by the bundled Nginx Proxy Manager profile (or your own reverse proxy / Tailscale Funnel).

### One-time host prep

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git openssl python3

# Docker Engine + Compose (official apt repo)
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker "$USER"
# Re-login (or: newgrp docker) then:
docker compose version
```

Find this machine's LAN IP (share with the installer operator):

```bash
hostname -I | awk '{print $1}'
whoami
```

## Install (guided)

From the laptop (or via SSH):

```bash
# Option A - installer clones into /opt/library
curl -fsSL https://raw.githubusercontent.com/brutaliccus/Library/main/scripts/install_library.sh | bash

# Option B - clone first, then run locally (good for a specific branch)
git clone https://github.com/brutaliccus/Library.git /opt/library
cd /opt/library
chmod +x scripts/install_library.sh
./scripts/install_library.sh /opt/library
```

The script walks you through:

1. **Core** - `APP_URL`, `SECRET_KEY`, `TZ`, `PUID`/`PGID`, `DOCKER_GID`
2. **Media mounts** - audiobooks / ebooks / Open Library dumps (+ staging folders)
3. **Bundled media** - ABS + Kavita + LibraForge (default for new servers)
4. **Jackett** — bundled + AudioBookBay/Flare preconfigure (or connect existing URL + API key)
5. **Prowlarr** — Knaben + ABB Torznab → Jackett (or connect existing)
6. **Open Library catalog** — Skip by default (advanced multi-GB option; indexers cover day-one search)
7. **Debrid** — Real-Debrid / TorBox (optional)
8. **Pipelines + Sweep** — LibraForge / ebook organizer + scan cadence defaults
9. **Catalog / LLM** — Hardcover, OpenRouter, AA, NYT, ISBNdb, Google Books (optional)
10. **Android APK repo** + scraper RSS-only defaults
11. **Nginx Proxy Manager** — default Yes; skip if you already reverse-proxy (ports 80/443)
12. **VPN** — Mullvad/gluetun (optional profile)
13. **Start stack** → configure Jackett/Prowlarr → sync ABS/Kavita/LF → NPM API → optional VAPID → health report

Re-runs are safe: existing `.env` secrets are kept when you press Enter on a prompt. NPM proxy hosts are upserted by domain (no duplicates).
If NPM is enabled, the installer writes `npm` into `COMPOSE_PROFILES`, starts `library-npm` with `--profile npm`, and **fails the install** if `:81` never listens (so a silent skip cannot happen).

### Nginx Proxy Manager prompts

| Prompt | Notes |
|--------|--------|
| Enable Nginx Proxy Manager (publishes 80/443 + admin :81)? | Default **Yes**. Answer **n** only if something else already owns 80/443. |
| Library public domain | e.g. `library.example.com`. Blank = LAN-only; use `:8085` until DNS is ready. |
| ABS / Kavita domains | Optional when bundled-media is on |
| Let's Encrypt email | Blank = HTTP-only proxy hosts; add later and re-run `bash scripts/configure_npm.sh` |
| NPM admin email / password | Bootstraps admin via `INITIAL_ADMIN_*` + API (no UI click-path) |

**With a domain (remote HTTPS):**

1. Point DNS **A/AAAA** for the domain at this laptop's public IP (router port-forward 80/443 if needed).
2. Enter domain + LE email in the installer.
3. `APP_URL` becomes `https://your.domain`.
4. If LE fails (DNS not ready), fix DNS and re-run `bash scripts/configure_npm.sh`.

**LAN-only:**

1. Leave domain blank (or skip NPM if you only need `:8085`).
2. Open `http://<lan-ip>:8085/login`.
3. NPM admin stays at `http://<lan-ip>:81` when the profile is on.

Container name is `library-npm` so it does not clash with a host-level NPM stack on the Pi. Production Pi installs that already reverse-proxy should answer **Skip NPM**.

### Unattended smoke test

```bash
LIBRARY_NONINTERACTIVE=1 LIBRARY_SKIP_NPM=1 LIBRARY_APP_URL="http://127.0.0.1:8085" \
  ./scripts/install_library.sh /opt/library --non-interactive
```

Full stack with NPM + Let's Encrypt (DNS must already point here):

```bash
LIBRARY_NONINTERACTIVE=1 \
  LIBRARY_NPM_DOMAIN=library.example.com \
  LIBRARY_NPM_LE_EMAIL=you@example.com \
  LIBRARY_NPM_ADMIN_EMAIL=you@example.com \
  LIBRARY_NPM_ADMIN_PASSWORD='choose-a-strong-password' \
  ./scripts/install_library.sh /opt/library --non-interactive
```

## First launch (web)

1. Open `APP_URL/login` (or `http://<laptop-ip>:8085/login`) → create **admin**
2. Create library + offline PIN
3. **`/admin/setup`** - bundled stack should already show green probes; finish Audible / debrid / catalog as needed
4. Share invite link from Settings



## Jackett / Prowlarr / Open Library

| Choice | Default | Skip / connect existing |
|--------|---------|-------------------------|
| Bundled Jackett | Yes | Answer **n**, then paste Jackett URL + API key (`LIBRARY_SKIP_JACKETT=1` / `LIBRARY_JACKETT_URL`) |
| Bundled Prowlarr | Yes | Answer **n**, then paste Prowlarr URL + API key (`LIBRARY_SKIP_PROWLARR=1` / `LIBRARY_PROWLARR_URL`) |
| Open Library cache | Skip | LIBRARY_OL_MODE=skip|build|download (default skip; multi-GB optional) |build|skip` |

After start, `scripts/configure_jackett.sh` and `scripts/configure_prowlarr.sh` idempotently wire FlareSolverr + AudioBookBay and Knaben (same shape as production on the Pi), then `scripts/apply_indexer_keys.sh` force-recreates the app and seeds `app_settings` so Admin Overview shows Jackett/Prowlarr as **configured**.

If Admin still says **Not configured**, the API keys never reached the running app container (`.env` write alone is not enough). Repair:

```bash
cd /opt/library   # or your install dir
bash scripts/configure_jackett.sh --force-bundled
bash scripts/configure_prowlarr.sh --force-bundled
bash scripts/apply_indexer_keys.sh
```

Bundled LibraForge sets `LIBRAFORGE_URL=http://<lan-ip>:5056` (not `127.0.0.1`) so **Open LibraForge** works from other devices. With NPM + a domain, `configure_npm.sh` creates access list `home-or-vpn` (LAN / docker / Tailscale) and a `forge.<domain>` proxy host.

Prebuilt OL catalog assets live on the GitHub Release tag [`data-seed`](https://github.com/brutaliccus/Library/releases/tag/data-seed). Maintainers package with `scripts/export_ol_catalog_seed.py`.

## Updating

Admins should update from the **host** install root (not inside the app container). The durable path is:

`ash
cd /opt/library   # or your install dir
bash scripts/update_library.sh
`

What it does:

1. git fetch of origin/main (shallow-clone friendly)
2. git reset --hard origin/main when the tree is clean (refuses dirty tracked files unless --force)
3. docker compose build app && docker compose up -d (honors COMPOSE_PROFILES from .env)
4. Re-runs scripts/apply_indexer_keys.sh when present (idempotent)

Preserved: .env secrets, media mounts, data/, NPM / reverse-proxy config. Does **not** run cutover or change proxy hosts.

Useful flags: --force (discard local tracked changes), --skip-build, --skip-keys, --branch NAME.

Admin UI (**Admin → Health**) includes a **Server stack update** card (version compare, **Check for updates**, **Update**) that mirrors the Android APK updater. Check uses the GitHub API against your configured repo; **Update** runs `scripts/update_library.sh --force` on the host via a one-shot Docker sidecar (requires `docker.sock` and the compose project directory — discovered from compose labels, or set `LIBRARY_HOST_ROOT=/opt/library` in `.env`). SSH one-liner still works when the Admin bridge is unavailable:

```bash
cd /opt/library && bash scripts/update_library.sh
```

### Optional: weekly systemd timer

`ash
sudo tee /etc/systemd/system/library-update.service >/dev/null <<'EOF'
[Unit]
Description=Update Library from origin/main
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=joey
WorkingDirectory=/opt/library
ExecStart=/bin/bash /opt/library/scripts/update_library.sh
EOF

sudo tee /etc/systemd/system/library-update.timer >/dev/null <<'EOF'
[Unit]
Description=Weekly Library git update

[Timer]
OnCalendar=Sun *-*-* 04:30:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now library-update.timer
`

Adjust User= / WorkingDirectory= to match the install. Prefer manual updates if you want to review changelogs first.

## Do not touch the Pi

Production remains on the Pi (`pihole@192.168.68.76` / existing mounts). This laptop install uses its own `/opt/library` and local or separately mounted media unless you deliberately point host paths at shared storage. Do **not** enable the `npm` profile on the Pi if it already runs a reverse proxy on 80/443.

## SSH from a Windows admin PC

```powershell
# Replace USER and IP
ssh USER@LAPTOP_IP
```

## Troubleshooting

| Symptom | Check |
|---------|--------|
| `docker: permission denied` | `newgrp docker` or re-login after `usermod -aG docker` |
| Admin Health Start/Stop fails | `DOCKER_GID` = host docker group id, recreate app |
| ABS/Kavita yellow in setup | Wait for first boot; `docker compose logs audiobookshelf kavita` |
| Flare thrashing CPU | Keep RSS-only; compose already caps FlareSolverr |
| NPM ports busy | Skip NPM (`LIBRARY_SKIP_NPM=1`) or free 80/443; container is `library-npm` |
| Let's Encrypt failed | DNS A/AAAA + port-forward 80/443, then `bash scripts/configure_npm.sh` |
| NPM login failed | Check `NPM_ADMIN_EMAIL` / `NPM_ADMIN_PASSWORD` in `.env`; admin UI on `:81` |

See also: [README Quick start](../README.md#quick-start), [`.env.example`](../.env.example).
