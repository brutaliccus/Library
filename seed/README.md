"""Shipped warm data for fresh installs.

indexer_cache.db.gz — sanitized SQLite snapshot (~36 MB compressed / ~150 MB
decompressed) of the indexer torrent cache and catalog match tables. No users,
API keys, or settings.

On first boot (empty indexer_torrents), the app decompresses and imports this
automatically. Rebuild from a live instance with:

    python scripts/export_indexer_seed.py /path/to/app.db ./seed
"""
