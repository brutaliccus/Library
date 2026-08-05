# Shipped warm data for fresh installs

`indexer_cache.db.gz` — sanitized SQLite snapshot (~36 MB compressed / ~150 MB
decompressed) of the indexer torrent cache and catalog match tables. No users,
API keys, or settings.

## How installs get the seed

1. **In-repo copy** — tracked under `seed/` and baked into the Docker image
   (`COPY seed/ seed/`). First boot imports when `indexer_torrents` is empty.
2. **Git LFS** — if the file is LFS-tracked, installers run
   `git lfs pull --include seed/indexer_cache.db.gz`.
3. **GitHub Release asset** (fallback) — tag `data-seed` assets:
   - `indexer_cache.db.gz` (preferred)
   - `seed-cache` (alias)
   Install scripts (`install_library.sh` / `install_library.ps1`) download when
   the local file is missing. **Optional:** if download fails, install continues
   with an empty cache; search still works once scrapers/indexers populate it.

## Rebuild

From a live instance:

```bash
python scripts/export_indexer_seed.py /path/to/app.db ./seed
```

Then refresh the `data-seed` release asset if you publish outside git.

## Open Library catalog (optional, large)

A local `ol_catalog.db` (multi‑GB) is **not** shipped in git. Fresh installs can:

1. **Download prebuilt** from the same [`data-seed`](https://github.com/brutaliccus/Library/releases/tag/data-seed) release when assets exist:
   - `ol_catalog.db.gz` (single file), or
   - `ol_catalog.manifest.json` + `ol_catalog.db.gz.partNN` (split for GitHub’s ~2 GiB limit)
2. **Build locally** via `scripts/ol_import_dumps.py` (hours + disk)
3. **Skip** — indexer seed + live Google Books still work

### Publish a prebuilt catalog (maintainers)

On a host that already has a built DB (e.g. Pi `/opt/stacks/Library Site/data/ol_catalog.db`):

```bash
python scripts/export_ol_catalog_seed.py /path/to/ol_catalog.db ./seed/ol
# Upload every file under ./seed/ol to the data-seed GitHub Release
```

Installers call `scripts/fetch_ol_catalog.py` which reassembles parts and writes `data/ol_catalog.db`.
