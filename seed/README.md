# Shipped warm data for fresh installs

`indexer_cache.db.gz` — sanitized SQLite snapshot (~36 MB compressed / ~150 MB
decompressed) of the **indexer torrent cache** and catalog match tables (AudioBook
Bay / Knaben / related search hits). No users, API keys, or settings.

This is what the guided install seeds for day-one release search — **not** the
multi‑GB Open Library SQLite catalog.

## How installs get the seed

1. **In-repo copy** — tracked under `seed/` and baked into the Docker image
   (`COPY seed/ seed/`). First boot imports when `indexer_torrents` is empty.
2. **Git LFS** — if the file is LFS-tracked, installers run
   `git lfs pull --include seed/indexer_cache.db.gz`.
3. **GitHub Release asset** (fallback) — tag [`data-seed`](https://github.com/brutaliccus/Library/releases/tag/data-seed):
   - `indexer_cache.db.gz` (preferred)
   - `seed-cache` (alias)
   Install scripts (`install_library.sh` / `install_library.ps1`) download when
   the local file is missing. **Optional:** if download fails, install continues
   with an empty cache; Jackett/Prowlarr live search still works once configured.

## Rebuild

From a live instance:

```bash
python scripts/export_indexer_seed.py /path/to/app.db ./seed
```

Then refresh the `data-seed` release asset if you publish outside git.

## Open Library catalog (advanced, optional, large)

A local `ol_catalog.db` (multi‑GB) is **not** shipped in git and is **not** on
the `data-seed` release. Fresh installs **skip** it by default.

Day-one search does **not** need it: Jackett (AudioBookBay + FlareSolverr),
Prowlarr (Knaben + ABB Torznab), and this indexer cache seed cover release
search. Store browse still works via live Google Books / Open Library APIs.

Later options (Admin → Catalog, or installer advanced choice):

1. **Skip** (default / recommended)
2. **Build locally** via `scripts/ol_import_dumps.py` (hours + disk)
3. **Download prebuilt** only if a maintainer explicitly published
   `ol_catalog.manifest.json` / `ol_catalog.db.gz` assets (rare; multi‑GB)

```bash
bash scripts/fetch_ol_catalog.sh          # only if prebuilt assets exist
python scripts/export_ol_catalog_seed.py  # maintainers who choose to publish OL
python scripts/ol_import_dumps.py --help  # local build
```
