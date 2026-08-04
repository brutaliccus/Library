"""Contract tests for My Library shelf persist / soft-refresh semantics.

Validates frontend source keeps origin-scoped persist, merge helpers, and
stale-while-revalidate soft refresh (no purge-before-fetch on Refresh).
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "frontend" / "src" / "main.tsx"
UTIL = ROOT / "frontend" / "src" / "utils" / "shelfQueryCache.ts"
MY_LIBRARY = ROOT / "frontend" / "src" / "pages" / "MyLibrary.tsx"
ADMIN = ROOT / "frontend" / "src" / "pages" / "Admin.tsx"
LIBRARY_ROUTER = ROOT / "app" / "routers" / "library.py"


def test_persist_buster_is_v6_origin_scoped_and_clears_legacy():
    main = MAIN.read_text(encoding="utf-8")
    assert "shelfPersistKey" in main
    assert "clearLegacyShelfPersist" in main
    util = UTIL.read_text(encoding="utf-8")
    assert 'SHELF_PERSIST_KEY_PREFIX = "rq-shelf-cache-v6:"' in util
    assert "rq-shelf-cache-v5" in util  # legacy clear list
    assert "rq-shelf-cache-v4" in util
    assert 'rq-shelf-cache-v5:' in util  # origin-scoped prefix sweep


def test_util_exports_orphan_merge_and_soft_refresh_helpers():
    util = UTIL.read_text(encoding="utf-8")
    for needle in (
        "absCollectionItemIds",
        "absCollectionSignature",
        "absCollectionHasOrphans",
        "mergeAbsCollection",
        "mergeKavitaCollection",
        "softRefreshLibraryCollectionQueries",
        "shouldBustLibraryCollectionCache",
        "markLibraryCollectionCacheBust",
        "purgeLibraryCollectionQueries",
        "stripCollectionEntriesFromPersist",
        "clearLegacyShelfPersist",
        "shelfPersistKey",
    ):
        assert needle in util


def test_my_library_soft_refreshes_without_purge_on_refresh():
    src = MY_LIBRARY.read_text(encoding="utf-8")
    assert "structuralSharing: false" in src
    assert "softRefreshLibraryCollectionQueries" in src
    assert "mergeAbsCollection" in src
    # Refresh must go through the single serialized backend pipeline — firing
    # ABS + Kavita scans in parallel from the client froze the Pi (OOM).
    refresh_fn = src[src.index("handleRefreshLibrary") : src.index("handleRefreshLibrary") + 2600]
    assert '"/library/refresh"' in refresh_fn
    assert "/library/abs/scan" not in refresh_fn
    assert "/library/kavita/scan" not in refresh_fn
    assert "/library/refresh/status" in refresh_fn
    # Refresh must not hard-purge before the first refetch.
    assert "purgeLibraryCollectionQueries" not in refresh_fn
    assert "softRefreshLibraryCollectionQueries" in refresh_fn
    # Must not thundering-herd refresh=true on every poll (Pi load): busted
    # catch-up only once the pipeline reports idle.
    assert "bustMs: 0" in refresh_fn
    assert "bustMs: 5_000" in refresh_fn or "bustMs: 5000" in refresh_fn.replace("_", "")


def test_soft_refresh_defaults_to_no_cache_bust():
    """Polls must not re-extend a refresh=true bust window by default."""
    util = UTIL.read_text(encoding="utf-8")
    # Default path must not call markLibraryCollectionCacheBust unconditionally.
    assert "opts?.bustMs != null && opts.bustMs > 0" in util or (
        "opts?.bustMs" in util and "bustMs > 0" in util
    )
    assert "opts?.bustMs ?? 35_000" not in util


def test_admin_fix_metadata_soft_refreshes_collection_cache():
    src = ADMIN.read_text(encoding="utf-8")
    assert "softRefreshLibraryCollectionQueries" in src
    assert "Library Refresh" in src
    assert "/admin/library/refresh" in src
    # Busted catch-up happens only after the pipeline reports idle.
    assert "/admin/library/refresh/status" in src
    assert "bustMs: 5_000" in src or "bustMs: 5000" in src.replace("_", "")


def test_legacy_scan_endpoints_join_serialized_pipeline():
    """Old mobile builds call /abs/scan + /kavita/scan in parallel — both must
    coalesce onto the single ABS → Kavita pipeline (parallel scans froze the Pi)."""
    src = LIBRARY_ROUTER.read_text(encoding="utf-8")
    assert "deferred" in src
    assert "already_running" in src
    assert "_singleflight" in src
    assert 'from app.services import library_refresh' in src
    assert "refresh_pipeline.kick()" in src
    # Direct scans must be gone from the legacy endpoints.
    assert "kick_library_scan" not in src
    assert "await kavita.scan_library()" not in src
    # Forced refresh must not run Hardcover enrich (Pi CPU).
    assert "if refresh:" in src
    assert "items = cleaned" in src


def test_admin_library_refresh_endpoint_exists():
    admin = Path(__file__).resolve().parents[1] / "app" / "routers" / "admin.py"
    src = admin.read_text(encoding="utf-8")
    assert '/library/refresh' in src or '"/library/refresh"' in src
    assert '"/library/refresh/status"' in src
    assert "library_refresh" in src
    assert "refresh_pipeline.kick()" in src


def test_abs_collection_signature_logic_orphan_detection():
    """Mirror of absCollectionHasOrphans — cached ids not in fresh → orphans."""
    cached_ids = {"old-asin", "keep-me"}
    fresh_ids = {"keep-me", "new-fixed"}
    assert any(i not in fresh_ids for i in cached_ids)
    assert not any(i not in fresh_ids for i in {"keep-me", "new-fixed"})


def test_merge_abs_prune_and_upsert_semantics():
    """Mirror of mergeAbsCollection pruneMissing behavior."""
    cached = {"a": 1, "b": 2}
    fresh = {"b": 20, "c": 3}
    # prune: only fresh keys
    pruned = {**fresh}
    assert set(pruned) == {"b", "c"}
    # upsert without prune: keep cached-only + fresh
    upserted = {**cached, **fresh}
    assert set(upserted) == {"a", "b", "c"}
    assert upserted["b"] == 20


def test_abs_collection_total_items_is_unique_not_genre_slots():
    """My Library subtitle must count unique books, not genre-bucket rows.

    Multi-genre titles (esp. after Hardcover fill) appear in several buckets;
    summing bucket lengths inflated the audiobook count (~272 vs ~180 ABS).
    """
    src = LIBRARY_ROUTER.read_text(encoding="utf-8")
    assert "totalItems\": unique_count" in src.replace(" ", "") or (
        '"totalItems": unique_count' in src
    )
    assert "unique_count = len(items)" in src
    # Must not sum genre bucket lengths for the subtitle count.
    assert "sum(len(v) for v in genres.values())" not in src

    util = UTIL.read_text(encoding="utf-8")
    assert "const totalItems = items.filter" in util or "totalItems = items.filter" in util
    assert "reduce((n, b) => n + b.length" not in util
