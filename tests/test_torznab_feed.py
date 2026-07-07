"""Tests for the Torznab recent-releases feed parser (RSS ingestion)."""

from app.services.prowlarr import _parse_torznab_feed

_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:torznab="http://torznab.com/schemas/2015/feed">
  <channel>
    <title>Knaben</title>
    <item>
      <title>Brandon Sanderson - Mistborn [M4B Audiobook]</title>
      <guid>https://example.org/t/1</guid>
      <link>https://example.org/dl/1.torrent</link>
      <comments>https://example.org/t/1</comments>
      <pubDate>Mon, 06 Jul 2026 12:00:00 +0000</pubDate>
      <size>512000000</size>
      <torznab:attr name="category" value="3030" />
      <torznab:attr name="seeders" value="42" />
      <torznab:attr name="peers" value="50" />
      <torznab:attr name="infohash" value="ABCDEF0123456789ABCDEF0123456789ABCDEF01" />
    </item>
    <item>
      <title>Some Movie 2026 1080p BluRay x264</title>
      <guid>https://example.org/t/2</guid>
      <link>https://example.org/dl/2.torrent</link>
      <size>9000000000</size>
      <torznab:attr name="category" value="2040" />
      <torznab:attr name="seeders" value="500" />
    </item>
    <item>
      <title>Jane Author - Novel Title (EPUB)</title>
      <guid>magnet:?xt=urn:btih:1111111111111111111111111111111111111111&amp;dn=novel</guid>
      <enclosure url="magnet:?xt=urn:btih:1111111111111111111111111111111111111111&amp;dn=novel" length="2000000" type="application/x-bittorrent" />
      <torznab:attr name="category" value="7020" />
      <torznab:attr name="seeders" value="7" />
      <torznab:attr name="peers" value="9" />
    </item>
  </channel>
</rss>
"""


def test_parses_book_items_and_drops_video():
    results = _parse_torznab_feed(_FEED, "Knaben")
    titles = [r["title"] for r in results]
    assert "Brandon Sanderson - Mistborn [M4B Audiobook]" in titles
    assert "Jane Author - Novel Title (EPUB)" in titles
    assert all("BluRay" not in t for t in titles)


def test_extracts_hash_seeders_and_size():
    results = _parse_torznab_feed(_FEED, "Knaben")
    audiobook = next(r for r in results if "Mistborn" in r["title"])
    assert audiobook["infoHash"] == "abcdef0123456789abcdef0123456789abcdef01"
    assert audiobook["seeders"] == 42
    assert audiobook["leechers"] == 8
    assert audiobook["size"] == 512_000_000
    assert audiobook["mediaType"] == "audiobook"
    assert audiobook["indexer"] == "Knaben"


def test_magnet_guid_yields_hash_and_magnet():
    results = _parse_torznab_feed(_FEED, "Knaben")
    ebook = next(r for r in results if "EPUB" in r["title"])
    assert ebook["infoHash"] == "1111111111111111111111111111111111111111"
    assert (ebook["magnetUrl"] or "").startswith("magnet:?xt=urn:btih:1111")
    assert ebook["mediaType"] == "ebook"


def test_bad_xml_returns_empty():
    assert _parse_torznab_feed("<not-xml", "Knaben") == []
    assert _parse_torznab_feed("<rss></rss>", "Knaben") == []
