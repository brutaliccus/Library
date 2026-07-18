"""Tests for Knaben full category crawl, sharding, and RSS parsing."""

import asyncio
from unittest.mock import AsyncMock, patch

from app.services.knaben import (
    KnabenFullCrawlState,
    KnabenSearchOptions,
    advance_crawl_state,
    build_knaben_crawl_queue,
    crawl_state_from_json,
    crawl_state_to_json,
    default_category_shards,
    expand_crawl_shard,
    search_with_options,
    _hit_to_result,
    _parse_knaben_rss_xml,
    _total_is_capped,
)


def test_build_knaben_crawl_queue_audiobook_only():
    specs = build_knaben_crawl_queue()
    cats = {s.categories[0] for s in specs}
    assert 1_003_000 in cats
    assert 9_001_000 not in cats


def test_default_category_shards_starts_with_browse():
    shards = default_category_shards()
    assert shards[0] == ""
    assert "a" in shards
    assert "9" in shards


def test_total_needs_shard_expansion():
    from app.services.knaben import _total_needs_shard_expansion

    assert _total_needs_shard_expansion({"relation": "gte", "value": 10000})
    assert _total_needs_shard_expansion({"relation": "eq", "value": 35796})
    assert not _total_needs_shard_expansion({"relation": "eq", "value": 3635})


def test_total_is_capped():
    assert _total_is_capped({"relation": "gte", "value": 10000})
    assert not _total_is_capped({"relation": "eq", "value": 3635})


def test_expand_crawl_shard_inserts_children():
    shards = ["", "a", "b"]
    out = expand_crawl_shard(shards, 1, "a")
    assert out[0] == ""
    assert out[1] == "a"
    assert "aa" in out
    assert "az" in out


def test_advance_crawl_state_moves_offset_then_shard():
    state = KnabenFullCrawlState(categories=[1_003_000], shards=["", "a"], shard_idx=0, offset=100)
    advance_crawl_state(state, next_offset=500, shard_exhausted=False)
    assert state.offset == 500
    assert state.shard_idx == 0

    advance_crawl_state(state, next_offset=1000, shard_exhausted=True)
    assert state.shard_idx == 1
    assert state.offset == 0


def test_crawl_state_roundtrip_json():
    state = KnabenFullCrawlState(
        categories=[1_003_000, 9_001_000],
        category_idx=1,
        shards=["", "z"],
        shard_idx=1,
        offset=200,
        expanded_shards=["a"],
        phase="full",
    )
    data = crawl_state_to_json(state)
    restored = crawl_state_from_json(data)
    assert restored.categories == [1_003_000]
    assert restored.category_idx == 0
    assert restored.shards == ["", "z"]
    assert restored.expanded_shards == ["a"]


def test_hit_to_result_rejects_missing_book_category():
    hit = {
        "title": "Some Audiobook m4b unabridged",
        "bytes": 500_000_000,
        "seeders": 5,
        "peers": 5,
        "categoryId": None,
        "magnetUrl": "magnet:?xt=urn:btih:" + "d" * 40,
        "hash": "d" * 40,
    }
    assert _hit_to_result(hit) is None


def test_hit_to_result_accepts_native_knaben_book_category():
    hit = {
        "title": "Some Novel",
        "bytes": 2_000_000,
        "seeders": 0,
        "peers": 0,
        "categoryId": 9_001_000,
        "category": "EBook",
        "magnetUrl": "magnet:?xt=urn:btih:" + "a" * 40,
        "hash": "a" * 40,
    }
    row = _hit_to_result(hit)
    assert row is not None


def test_parse_knaben_rss_xml_extracts_magnet_and_hash():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <rss><channel>
      <item>
        <title><![CDATA[Test Audiobook]]></title>
        <guid>abcdef0123456789abcdef0123456789abcdef01</guid>
        <description><![CDATA[Size: 123.45 MB\nmagnet:?xt=urn:btih:ABCDEF0123456789ABCDEF0123456789ABCDEF01]]></description>
      </item>
    </channel></rss>"""
    rows = _parse_knaben_rss_xml(xml, category_id=1_003_000)
    assert len(rows) == 1
    assert rows[0]["title"] == "Test Audiobook"
    assert rows[0]["infoHash"] == "abcdef0123456789abcdef0123456789abcdef01"
    assert rows[0]["magnetUrl"].startswith("magnet:")


def test_search_with_options_paginates(monkeypatch):
    pages = [
        [{"title": f"Book {i}", "bytes": 5_000_000, "seeders": 1, "peers": 1,
          "categoryId": 1_003_000, "category": "Audiobook",
          "magnetUrl": f"magnet:?xt=urn:btih:{i:040x}", "hash": f"{i:040x}"}
         for i in range(100)],
        [{"title": f"Book {i}", "bytes": 5_000_000, "seeders": 1, "peers": 1,
          "categoryId": 1_003_000, "category": "Audiobook",
          "magnetUrl": f"magnet:?xt=urn:btih:{i+100:040x}", "hash": f"{i+100:040x}"}
         for i in range(40)],
    ]
    call_count = {"n": 0}

    async def fake_page_raw(opts, *, offset, timeout, page_size=100):
        idx = call_count["n"]
        call_count["n"] += 1
        if idx >= len(pages):
            return {"hits": [], "total": {"relation": "eq", "value": 140}}
        return {"hits": pages[idx], "total": {"relation": "eq", "value": 140}}

    monkeypatch.setattr("app.services.knaben._fetch_page_raw", fake_page_raw)

    opts = KnabenSearchOptions(query="test", categories=(1_003_000,))
    results = asyncio.run(search_with_options(opts, limit=150, timeout=30))
    assert call_count["n"] == 2
    assert len(results) == 140


def test_crawl_full_category_batch_updates_state(monkeypatch):
    from app.services.knaben import crawl_full_category_batch, new_full_crawl_state

    async def fake_page_raw(opts, *, offset, timeout, page_size=100):
        if offset > 0:
            return {"hits": [], "total": {"relation": "eq", "value": 1}}
        return {
            "hits": [{
                "title": "Crawled Book",
                "bytes": 1_000_000,
                "seeders": 0,
                "peers": 0,
                "categoryId": 1_003_000,
                "category": "Audiobook",
                "magnetUrl": "magnet:?xt=urn:btih:" + "f" * 40,
                "hash": "f" * 40,
            }],
            "total": {"relation": "eq", "value": 1},
        }

    monkeypatch.setattr("app.services.knaben._fetch_page_raw", fake_page_raw)
    monkeypatch.setattr("app.services.knaben.maybe_expand_crawl_shard", AsyncMock())

    state = new_full_crawl_state()
    results = asyncio.run(crawl_full_category_batch(state, max_pages=1, timeout=10))
    assert len(results) == 1
    assert state.shard_idx == 1
