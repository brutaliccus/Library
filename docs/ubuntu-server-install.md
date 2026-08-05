# Ubuntu Server 24.04 - Library Site install

Guided path for a **fresh** host (e.g. spare laptop). Does **not** migrate or stop the production Pi.

## Prerequisites

| Tool | Why |
|------|-----|
| Ubuntu Server 24.04 LTS | Recommended OS (OpenSSH enabled at install) |
| Docker Engine + Compose plugin | Runs the stack |
| Git | Clone / update the repo |
| curl, openssl | Installer + secrets |
| Free ports | `8085`, `9696`, `8191`, `9117` (+ `13378`, `5000`, `5056` if bundled-media) |

Optional later: Tailscale Funnel or reverse proxy for HTTPS; Mullvad WireGuard for ABB egress.

### One-time host prep

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git openssl

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
4. **Indexers** - Prowlarr / Jackett / FlareSolverr URLs (compose sidecars)
5. **Debrid** - Real-Debrid / TorBox (optional)
6. **Pipelines + Sweep** - LibraForge / ebook organizer + scan cadence defaults
7. **Catalog / LLM** - Hardcover, OpenRouter, AA, NYT, ISBNdb, Google Books (optional)
8. **Android APK repo** + scraper RSS-only defaults
9. **VPN** - Mullvad/gluetun (optional profile)
10. **Start stack** → sync keys → optional VAPID → health report

Re-runs are safe: existing `.env` secrets are kept when you press Enter on a prompt.

### Unattended smoke test

```bash
LIBRARY_NONINTERACTIVE=1 LIBRARY_APP_URL="http://127.0.0.1:8085" \
  ./scripts/install_library.sh /opt/library --non-interactive
```

## First launch (web)

1. Open `http://<laptop-ip>:8085/login` → create **admin**
2. Create library + offline PIN
3. **`/admin/setup`** - bundled stack should already show green probes; finish Audible / debrid / catalog as needed
4. Share invite link from Settings

## Do not touch the Pi

Production remains on the Pi (`pihole@192.168.68.76` / existing mounts). This laptop install uses its own `/opt/library` and local or separately mounted media unless you deliberately point host paths at shared storage.

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

See also: [README Quick start](../README.md#quick-start), [`.env.example`](../.env.example).