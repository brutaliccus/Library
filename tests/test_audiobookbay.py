"""Unit tests for AudioBook Bay listing HTML parse."""

from app.services.audiobookbay import _parse_listing_page, _normalize_query


SAMPLE = """
<html><body>
<div class="post">
  <div class="postTitle"><h2><a href="/abooks/dungeon-crawler-carl-1/">Dungeon Crawler Carl Book 1</a></h2></div>
  <div class="postContent">Format: M4B / Bitrate: 64 kbps File Size: 450.2MB Posted: 12 Mar 2022</div>
  <div class="postInfo">Category: Sci-Fi</div>
</div>
<div class="post">
  <div class="postTitle"><h2><a href="/abooks/dungeon-crawler-carl-7/">Dungeon Crawler Carl Book 7</a></h2></div>
  <div class="postContent">Format: MP3 / Bitrate: 128 kbps File Size: 1.2GB Posted: 01 Jan 2025</div>
</div>
</body></html>
"""


def test_normalize_query():
    assert _normalize_query("Dungeon Crawler Carl!!!") == "dungeon crawler carl"


def test_parse_listing_page():
    rows = _parse_listing_page(SAMPLE, "https://audiobookbay.lu/")
    assert len(rows) == 2
    assert "Dungeon Crawler Carl Book 1" in rows[0]["title"]
    assert "[M4B]" in rows[0]["title"]
    assert rows[0]["downloadUrl"].endswith("/abooks/dungeon-crawler-carl-1/")
    assert rows[0]["size"] > 400 * 1024 * 1024
    assert rows[0]["indexer"] == "AudioBookBay"
    assert rows[0]["mediaType"] == "audiobook"
    assert rows[1]["size"] > 1024 * 1024 * 1024


def test_scrape_enabled_live_vs_scraper(monkeypatch):
    from app.services import audiobookbay as abb
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("ABB_LIVE_SEARCH_ENABLED", "true")
    monkeypatch.setenv("ABB_DEEP_SEARCH_ENABLED", "false")
    get_settings.cache_clear()
    abb.settings = get_settings()
    assert abb._scrape_enabled(for_live=True) is True
    assert abb._scrape_enabled(for_live=False) is False


def test_needs_flare_only():
    from app.services.audiobookbay import _needs_flare_only

    assert _needs_flare_only("https://audiobookbay.lu/") is True
    assert _needs_flare_only("https://example.com/") is False


def test_max_pages_hard_ceiling(monkeypatch):
    from app.services import audiobookbay as abb
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("ABB_DEEP_SEARCH_PAGES", "99")
    get_settings.cache_clear()
    abb.settings = get_settings()
    assert abb._max_pages(None) == 12
    assert abb._max_pages(3) == 3
    get_settings.cache_clear()


def test_flare_max_timeout_not_capped_at_120s(monkeypatch):
    from app.services import audiobookbay as abb
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("ABB_FLARESOLVERR_TIMEOUT", "180")
    get_settings.cache_clear()
    abb.settings = get_settings()
    assert abb._flare_max_timeout_ms(warmup=True) == 180_000
    assert abb._flare_max_timeout_ms(warmup=False) == 180_000
    get_settings.cache_clear()
