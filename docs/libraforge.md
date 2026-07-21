# LibraForge (sibling stack)

[LibraForge](https://github.com/coconautilus17/LibraForge) runs **beside** the Library app — not inside it. It shares the same on-disk audiobook library that Audiobookshelf and Library use.

| Service | Host path | In-container path |
|---------|-----------|-------------------|
| Library app | `/mnt/Audiobooks` | `/audiobooks` |
| Audiobookshelf | `/mnt/Audiobooks` | `/audiobooks` |
| LibraForge | `/mnt/Audiobooks` | `/audiobooks` |

Stack location on the Pi: `/opt/stacks/libraforge`

**License**: treat as **AGPL** (Audible/mutagen lineage). Do **not** vendor or import LibraForge code into Library Site — open it in a new tab only.

## Admin UI entry point

In Library → **Admin → Health**, the **LibraForge** card shows connected/disconnected (probe from the Library container to `LIBRAFORGE_INTERNAL_URL`) and an **Open LibraForge** button (`LIBRAFORGE_URL`, default `https://forge.library.freiverse.com`).

After Metadata/Folder Forge apply, use **Scan ABS & fix metadata** on the same Health tab.

Env (Library Site `.env` on the Pi):

```bash
LIBRAFORGE_URL=https://forge.library.freiverse.com
LIBRAFORGE_INTERNAL_URL=http://172.17.0.1:5056
```

Audible credentials stay only under `/opt/stacks/libraforge/audible-auth/` — never in Library `.env`.

## Install / update

On the Pi:

```bash
cd "/opt/stacks/Library Site"
bash scripts/install_libraforge.sh
```

Or from your dev machine after pulling this repo:

```powershell
scp scripts/install_libraforge.sh pihole@192.168.68.76:/tmp/
ssh pihole@192.168.68.76 "bash /tmp/install_libraforge.sh"
```

UI (localhost + Docker bridge): `http://127.0.0.1:5056` (also bound on `172.17.0.1:5056` so the Library container can health-probe it). The install script writes `docker-compose.override.yml` for that second bind.

The install script creates `/mnt/Audiobooks/_unorganized` for messy imports.

## Expose safely (Nginx Proxy Manager)

NPM runs on the Pi as Docker container `nginx-proxy-manager` (`/opt/stacks/nginxproxymanager`), UI on **http://192.168.68.76:81** (ports 80/443 for public traffic). Library deploy only reloads custom nginx snippets - it does **not** create proxy hosts, and this repo has **no** NPM API password/token.

LibraForge listens on `127.0.0.1:5056` and `172.17.0.1:5056` only (not the LAN IP). From the NPM container, **`172.17.0.1:5056` works**; `127.0.0.1` and `192.168.68.76:5056` do not.

### Configured on Pi (proxy host + access list)

These already exist in NPM (other proxy hosts on this box are typically public because those apps have their own login; LibraForge does **not**, so it is restricted):

| Item | Value |
|------|-------|
| Proxy host | `forge.library.freiverse.com` -> `http://172.17.0.1:5056` (websockets on, block exploits on) |
| Access list | `home-or-vpn` (Satisfy Any): allow `192.168.68.0/22`, `192.168.0.0/16`, `100.64.0.0/10` (Tailscale), `10.0.0.0/8`, `172.16.0.0/12`, `127.0.0.1/32`; NPM adds `deny all` |
| SSL | Let's Encrypt cert id **39**, Force SSL on (expires ~2026-10-19). DNS A -> `74.135.86.77` (DNS-only / grey-cloud; not Cloudflare-proxied) |

Local check (from Pi): `curl -sS http://172.17.0.1:5056/health` and `curl -sS --resolve forge.library.freiverse.com:443:127.0.0.1 https://forge.library.freiverse.com/`.

### DNS + SSL (done)

Cloudflare A record for `forge.library.freiverse.com` -> `74.135.86.77` (same as `library.freiverse.com`). Keep the record **DNS only** (grey cloud) so HTTP-01 renewals work; orange-cloud proxy can break Let's Encrypt HTTP-01.

From an allowed LAN/VPN IP, `https://forge.library.freiverse.com` should load; from the public Internet expect **403** (`home-or-vpn`).

Do **not** leave the forge host on Public / without an access list. It can rewrite tags and move files.
## Audible authentication

Metadata Forge needs an **unencrypted** Audible auth JSON at `/auth/audible-metadata.json` (host: `/opt/stacks/libraforge/audible-auth/`). Use a **dedicated** Audible account (not your main one).

### Does this LibraForge have a browser login GUI?

**Yes** — the installed stack is upstream [coconautilus17/LibraForge](https://github.com/coconautilus17/LibraForge). Prefer **Settings → Accounts** for guided browser OAuth when available.

### Practical path (auth file → Pi)

If you already have an unencrypted Audible auth JSON (e.g. from `audible-cli`):

1. On Windows (Python), install the CLI and run quickstart — choose **external browser** login when asked:

   ```powershell
   pip install audible-cli
   audible quickstart
   ```

   Or with the library alone: use `Authenticator.from_login_external` (prints a URL; paste the final Amazon redirect URL back). See [Audible authorization docs](https://audible.readthedocs.io/en/latest/auth/authorization.html).

2. Copy the generated auth file to the Pi (rename to the expected name if needed):

   ```powershell
   scp $env:USERPROFILE\.audible\*.json pihole@192.168.68.76:/opt/stacks/libraforge/audible-auth/audible-metadata.json
   ```

   Exact filename from quickstart may vary; LibraForge expects **`audible-metadata.json`** in that folder.

3. Restart LibraForge:

   ```powershell
   ssh pihole@192.168.68.76 "cd /opt/stacks/libraforge && docker compose restart"
   ```

The auth directory is mounted into the container at `/auth`. Encrypted auth files are **not** supported by this UI.

## Recommended workflow with Library

1. **New downloads** — Library pipeline organizes into `/mnt/Audiobooks/{Author}/{Title}/` as today.
2. **Messy / legacy books** — move or download into `/mnt/Audiobooks/_unorganized/`.
3. **LibraForge Metadata Forge** — open from Admin → Health → **Open LibraForge**; dry-run on `_unorganized`, review report, enable backup on first apply, then apply.
4. **Folder Forge** (`/organizer`) — dry-run moves from `_unorganized` → `/audiobooks` (`Author/Series/Book` layout).
5. **Audiobookshelf** — run **Scan ABS & fix metadata** from Library Admin → Health (or let ABS scan on its schedule).
6. **M4B Tool** — optional: merge multi-file books before or after metadata apply.

Always **dry-run first**. Back up media before the first write pass.

## Permissions note

Some folders under `/mnt/Audiobooks` may be owned by `root` from older imports. LibraForge runs as UID `1000` (`pihole`). If apply fails with permission errors:

```bash
sudo chown -R pihole:pihole "/mnt/Audiobooks/Some Author"
```

## Uninstall

```bash
cd /opt/stacks/libraforge
docker compose down
# rm -rf /opt/stacks/libraforge   # optional — removes clone, reports, and local .env
```

Your audiobook files are not deleted by uninstalling the container.
