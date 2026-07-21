# LibraForge (sibling stack)

[LibraForge](https://github.com/brutaliccus/LibraForge) (fork of [coconautilus17/LibraForge](https://github.com/coconautilus17/LibraForge)) runs **beside** the Library app — not inside it. It shares the same on-disk audiobook library that Audiobookshelf and Library use.

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

Your Pi uses **Nginx Proxy Manager** on ports 80/443. Add a proxy host (manual in NPM UI if not already present):

| Field | Value |
|-------|-------|
| Domain | `forge.library.freiverse.com` |
| Forward hostname | `127.0.0.1` |
| Forward port | `5056` |
| Websockets | On |
| Access | **Restrict** — VPN, home IP, or NPM access list |

Request SSL in NPM as usual. Do **not** expose LibraForge to the public internet without restrictions — it can rewrite tags and move files. Library deploy does **not** create this proxy host.

## Audible authentication

Metadata Forge needs Audible API credentials. Use a **dedicated** Audible account (not your main one).

1. On any machine with Python, install the Audible CLI and authenticate:

   ```bash
   pip install audible
   audible quickstart
   ```

2. Copy the generated auth file to the Pi:

   ```bash
   scp ~/.audible/audible-metadata.json pihole@192.168.68.76:/opt/stacks/libraforge/audible-auth/
   ```

3. Restart LibraForge:

   ```bash
   ssh pihole@192.168.68.76 "cd /opt/stacks/libraforge && docker compose restart"
   ```

The auth directory is mounted read-only inside the container at `/auth`.

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
