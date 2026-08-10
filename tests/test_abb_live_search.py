"""Live ABB search must return real listing rows (network required)."""
from __future__ import annotations

import asyncio
import os

import pytest

# Keep this test opt-in for CI; always run locally / when explicitly requested.
pytestmark = pytest.mark.skipif(
    os.environ.get("ABB_LIVE_TEST", "1") == "0",
    reason="Set ABB_LIVE_TEST=0 to skip live AudioBookBay network test",
)


@pytest.fixture(autouse=True)
def _reset_abb_state(monkeypatch: pytest.MonkeyPatch):
    from app.services import audiobookbay

    audiobookbay._BASE_URL = None
    audiobookbay._CACHE.clear()
    audiobookbay._flare_session = None
    audiobookbay._abb_cookies = None
    # Ensure live scrape is not blocked by empty deep/live flags.
    monkeypatch.setattr(audiobookbay.settings, "abb_live_search_enabled", False)
    monkeypatch.setattr(audiobookbay.settings, "abb_deep_search_enabled", False)
    monkeypatch.setattr(audiobookbay.settings, "abb_proxy_url", "")
    monkeypatch.setattr(audiobookbay.settings, "flaresolverr_url", "")
    monkeypatch.setattr(audiobookbay.settings, "abb_mirror_max_tries", 3)


def test_live_abb_direct_scrape_returns_results():
    from app.services import audiobookbay

    async def _run():
        rows: list = []
        async for page, pages, fresh in audiobookbay.iter_search_pages(
            "dune herbert", max_pages=1, for_live=True,
        ):
            rows.extend(fresh)
            assert page == 1
            assert pages >= 1
        return rows

    rows = asyncio.run(_run())
    assert len(rows) >= 3, f"expected ABB listing hits, got {len(rows)}"
    assert any("dune" in (r.get("title") or "").lower() for r in rows)
    assert all((r.get("downloadUrl") or r.get("guid")) for r in rows)
    assert all((r.get("indexer") or "").lower().startswith("audiobookbay") for r in rows)


def test_live_abb_search_audiobookbay_multi_returns_results(monkeypatch: pytest.MonkeyPatch):
    from app.services import audiobookbay, prowlarr

    async def _no_jackett(*_a, **_k):
        return []

    async def _no_indexer_id():
        return None

    monkeypatch.setattr(prowlarr, "search_jackett_audiobookbay", _no_jackett)
    monkeypatch.setattr(prowlarr, "get_audiobookbay_indexer_id", _no_indexer_id)
    monkeypatch.setattr(prowlarr.settings, "abb_proxy_url", "")
    monkeypatch.setattr(audiobookbay.settings, "abb_proxy_url", "")

    rows = asyncio.run(
        prowlarr.search_audiobookbay_multi(
            ["project hail mary"],
            allow_flare_fallback=True,
            overall_timeout=45.0,
        )
    )
    assert len(rows) >= 1, f"search_audiobookbay_multi returned no ABB hits ({len(rows)})"
    assert any(
        "hail" in (r.get("title") or "").lower() or "weir" in (r.get("title") or "").lower()
        for r in rows
    )