"""ABB search waterfall and scrape gating."""
from __future__ import annotations

import asyncio

from app.services import audiobookbay, prowlarr


def test_scrape_enabled_for_live_always():
    assert audiobookbay._scrape_enabled(for_live=True) is True


def test_flare_allowed_requires_opt_in(monkeypatch):
    monkeypatch.setattr(audiobookbay.settings, "flaresolverr_url", "http://flare:8191")
    monkeypatch.setattr(audiobookbay.settings, "abb_proxy_url", "")
    monkeypatch.setattr(audiobookbay.settings, "abb_live_search_enabled", False)
    monkeypatch.setattr(audiobookbay.settings, "abb_deep_search_enabled", False)
    assert audiobookbay._flare_allowed() is False
    monkeypatch.setattr(audiobookbay.settings, "abb_proxy_url", "http://gluetun:8888")
    assert audiobookbay._flare_allowed() is True


def test_search_multi_falls_through_to_direct_scrape(monkeypatch):
    async def empty_jackett(*_a, **_k):
        return []

    async def no_indexer():
        return None

    async def fake_scrape(query, *, max_pages, remaining_fn, label):
        assert label == "direct scrape"
        return [
            {
                "title": "Fake Book",
                "indexer": "AudioBookBay",
                "downloadUrl": "http://audiobookbay.is/abss/fake/",
                "infoHash": "",
                "magnetUrl": None,
            }
        ]

    async def passthrough(rows, **_k):
        return rows

    monkeypatch.setattr(prowlarr, "search_jackett_audiobookbay", empty_jackett)
    monkeypatch.setattr(prowlarr, "get_audiobookbay_indexer_id", no_indexer)
    monkeypatch.setattr(prowlarr, "_abb_live_scrape", fake_scrape)
    monkeypatch.setattr(prowlarr, "enrich_audiobookbay_for_cache", passthrough)
    monkeypatch.setattr(prowlarr.settings, "abb_proxy_url", "")

    rows = asyncio.run(
        prowlarr.search_audiobookbay_multi(
            ["some title"],
            allow_flare_fallback=True,
            overall_timeout=40.0,
        )
    )
    assert len(rows) == 1
    assert rows[0]["title"] == "Fake Book"


def test_recent_scraper_releases_enriches_abb_when_proxy_set(monkeypatch):
    """Regression: proxy mode used to skip hash enrich, so ABB RSS never upserted."""

    async def no_indexers():
        return []

    async def fake_listings(*, max_pages=2):
        return [
            {
                "title": "Brand New ABB Title [M4B]",
                "indexer": "AudioBookBay",
                "downloadUrl": "http://audiobookbay.lu/abss/brand-new/",
                "guid": "http://audiobookbay.lu/abss/brand-new/",
                "infoHash": "",
                "magnetUrl": None,
                "size": 200_000_000,
                "mediaType": "audiobook",
                "categories": [{"name": "Audiobooks"}],
            }
        ]

    enrich_calls: list[dict] = []

    async def fake_enrich(rows, **kwargs):
        enrich_calls.append({"n": len(rows), **kwargs})
        out = []
        for r in rows:
            c = dict(r)
            c["infoHash"] = "a" * 40
            c["magnetUrl"] = f"magnet:?xt=urn:btih:{'a'*40}"
            out.append(c)
        return out

    monkeypatch.setattr(prowlarr, "get_trusted_indexer_info", no_indexers)
    monkeypatch.setattr(prowlarr.settings, "abb_proxy_url", "http://gluetun:8888")
    monkeypatch.setattr(prowlarr.settings, "flaresolverr_url", "http://flare:8191")
    monkeypatch.setattr(prowlarr, "enrich_audiobookbay_for_cache", fake_enrich)

    import app.services.audiobookbay as abb_mod

    monkeypatch.setattr(abb_mod, "fetch_recent_listings", fake_listings)

    results, counts = asyncio.run(
        prowlarr.fetch_recent_scraper_releases(
            limit_per_indexer=50,
            timeout=30,
            include_abb_flare=True,
        )
    )
    assert enrich_calls, "ABB rows must be hash-enriched when proxy is configured"
    assert enrich_calls[0].get("limit") == 12
    assert counts.get("AudioBookBay(VPN)") == 1
    assert len(results) == 1
    assert results[0]["infoHash"] == "a" * 40