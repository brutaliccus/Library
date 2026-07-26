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
