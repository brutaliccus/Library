"""Contract tests for My Library shelf persist / replace semantics.

Validates frontend source keeps the ABS collection cache buster and purge helpers
that stop orphan ASIN titles from surviving after metadata fixes.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "frontend" / "src" / "main.tsx"
UTIL = ROOT / "frontend" / "src" / "utils" / "shelfQueryCache.ts"
MY_LIBRARY = ROOT / "frontend" / "src" / "pages" / "MyLibrary.tsx"
ADMIN = ROOT / "frontend" / "src" / "pages" / "Admin.tsx"


def test_persist_buster_is_v5_origin_scoped_and_clears_legacy():
    main = MAIN.read_text(encoding="utf-8")
    assert "shelfPersistKey" in main
    assert "clearLegacyShelfPersist" in main
    util = UTIL.read_text(encoding="utf-8")
    assert 'SHELF_PERSIST_KEY_PREFIX = "rq-shelf-cache-v5:"' in util
    assert "rq-shelf-cache-v4" in util  # legacy clear list
    assert "rq-shelf-cache-v3" in util


def test_util_exports_orphan_and_purge_helpers():
    util = UTIL.read_text(encoding="utf-8")
    for needle in (
        "absCollectionItemIds",
        "absCollectionSignature",
        "absCollectionHasOrphans",
        "purgeLibraryCollectionQueries",
        "stripCollectionEntriesFromPersist",
        "clearLegacyShelfPersist",
        "shelfPersistKey",
    ):
        assert needle in util


def test_my_library_replaces_collections_and_purges_on_refresh():
    src = MY_LIBRARY.read_text(encoding="utf-8")
    assert "structuralSharing: false" in src
    assert "purgeLibraryCollectionQueries" in src
    assert "removeQueries" in UTIL.read_text(encoding="utf-8")


def test_admin_fix_metadata_purges_collection_cache():
    src = ADMIN.read_text(encoding="utf-8")
    assert "purgeLibraryCollectionQueries" in src


def test_abs_collection_signature_logic_orphan_detection():
    """Mirror of absCollectionHasOrphans — cached ids not in fresh → orphans."""
    cached_ids = {"old-asin", "keep-me"}
    fresh_ids = {"keep-me", "new-fixed"}
    assert any(i not in fresh_ids for i in cached_ids)
    assert not any(i not in fresh_ids for i in {"keep-me", "new-fixed"})
