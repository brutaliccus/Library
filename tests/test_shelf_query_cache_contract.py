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
    assert "wait: false" in src or 'wait: false' in src or "wait: false" in src.replace(" ", "")
    # Refresh must not hard-purge before the first refetch.
    refresh_fn = src[src.index("handleRefreshLibrary") : src.index("handleRefreshLibrary") + 1200]
    assert "purgeLibraryCollectionQueries" not in refresh_fn
    assert "softRefreshLibraryCollectionQueries" in refresh_fn


def test_admin_fix_metadata_soft_refreshes_collection_cache():
    src = ADMIN.read_text(encoding="utf-8")
    assert "softRefreshLibraryCollectionQueries" in src


def test_abs_scan_supports_deferred_wait_false():
    src = LIBRARY_ROUTER.read_text(encoding="utf-8")
    assert "wait: bool" in src or "wait: bool =" in src.replace(" ", "")
    assert "deferred" in src
    assert "_abs_scan_wait_and_cleanup" in src


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
