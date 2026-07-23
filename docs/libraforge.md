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

## Automated download pipeline (Library → LibraForge)

When `LIBRAFORGE_PIPELINE_ENABLED=true` (default), audiobook requests:

1. Land in `/audiobooks/_unorganized/req_{id}_{slug}/` (host: `/mnt/Audiobooks/_unorganized/…`).
2. **Metadata Forge** (`POST /api/runs`, `apply=true`, `min_score` from `LIBRAFORGE_MIN_SCORE`, covers on).
3. **M4B** on Pi (`POST /api/m4b/runs`) if not already a single `.m4b`.
4. **Folder Forge** (`POST /api/organizer/runs`) with template  
   `{author}/{series} [{edition}]/{title}/{filename}` → `/audiobooks`.
5. ABS scan / finalize.

If metadata score is below auto-apply threshold (or LibraForge is down), the request becomes **`quarantined`**: files stay in `_unorganized`, admins are notified, and Admin → Requests offers **Manual Review** (LibraForge), **Continue pipeline**, or **Reject / delete**.

### ABS exclusion for `_unorganized` (required)

`_unorganized` lives **inside** the shared `/mnt/Audiobooks` mount (LibraForge convention). Audiobookshelf does **not** treat `_` as hidden — it will index staging folders unless excluded.

**Decision:** keep `_unorganized` under `/mnt/Audiobooks` for LibraForge defaults, and configure ABS **Folders to Ignore** to include `_unorganized` (library settings). Quarantined books can sit there indefinitely; without this ignore, ABS will show ghost/incomplete items.

Fallback if ABS cannot ignore folders: move staging to a sibling host path outside the ABS library root (e.g. `/mnt/m4b-source-quarantine`) and mount it into Library + LibraForge only — not into ABS.

### Manual / legacy workflow

1. **Messy / legacy books** — drop into `/mnt/Audiobooks/_unorganized/`.
2. **LibraForge Metadata Forge** — Admin → Health → **Open LibraForge**; dry-run, then apply.
3. **Folder Forge** (`/organizer`) — dry-run then apply into `/audiobooks`.
4. **Audiobookshelf** — **Scan ABS & fix metadata** from Library Admin → Health.
5. **M4B Tool** — optional; heavy converts may use Windows LibraForge `:5057` manually.

Always **dry-run first** for manual batch work. Back up media before the first write pass.

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

## abs-agg (specialty metadata)

Optional companion stack: [abs-agg](https://github.com/Vito0912/abs-agg) on the Pi at `/opt/stacks/abs-agg`.

| Item | Value |
|------|-------|
| Host ports | `127.0.0.1:3010` and `192.168.68.76:3010` (container listens on 3000) |
| Docker network | Joined to `libraforge_default` as hostname `abs-agg` |
| Pi LibraForge URL | `http://abs-agg:3000` (`/opt/stacks/libraforge/config/abs-agg.json`) |
| Windows LibraForge URL | `http://192.168.68.76:3010` (`C:\dev\LibraForge\config\abs-agg.json`) |
| Secrets | `HARDCOVER_TOKEN` in `/opt/stacks/abs-agg/.env` (copied from Library Site `integrations.hardcover_api_key`). `GOODREADS_API_KEY` not set (Goodreads provider disabled until you add a LazyLibrarian-style key). Do not put these keys in LibraForge. |

**UI:** LibraForge → **Settings → GraphicAudio / SoundBooth Theater (abs-agg)** → set URL → **Save and verify**. On Manual Review / Metadata Forge, choose metadata provider **abs-agg** and pick a source (Hardcover, GraphicAudio, LibriVox, etc.). Batch runs also use this URL as an automatic specialty fallback when publishers match.

Start/stop: `cd /opt/stacks/abs-agg && docker compose up -d` (does not affect M4B or other stacks).

