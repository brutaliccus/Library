"""Unit tests for Anna's Archive URL extraction / file-URL heuristics."""

from __future__ import annotations

from unittest.mock import patch

from app.services import annas_archive as aa


DETAIL_HTML = """
<html><body>
  <a class="js-download-link" href="/fast_download/abc123/0/0">Fast Partner Server #1</a>
  <a class="js-download-link" href="/fast_download/abc123/0/1">Fast Partner Server #2</a>
  <a href="/slow_download/abc123/0/0" class="js-download-link">Slow Partner Server #1</a>
  <a href="/slow_download/abc123/0/1" class="js-download-link">Slow Partner Server #2</a>
  <a href="/slow_download/abc123/0/2" class="js-download-link">Slow Partner Server #3</a>
  <ul class="js-show-external">
    <li><a class="js-download-link" href="https://z-library.sk/md5/abc123">Z-Library</a></li>
    <li><a class="js-download-link" href="https://libgen.li/ads.php?md5=abc123">Libgen.li</a></li>
  </ul>
</body></html>
"""

ZLIB_ONLY_HTML = """
<html><body>
  <a class="js-download-link" href="/fast_download/deadbeef/0/0">Fast #1</a>
  <a href="/slow_download/deadbeef/0/0" class="js-download-link">Slow #1</a>
  <a href="/slow_download/deadbeef/0/1" class="js-download-link">Slow #2</a>
  <a class="js-download-link" href="https://z-library.sk/md5/deadbeef">Z-Library</a>
</body></html>
"""

SLOW_PAGE_HTML = """
<html><body>
  <p>Download from partner website</p>
  <span class="bg-gray-200 break-all">https://wbsg8v.xyz/d3/y/s/1785882941/500/g6/zlib3_files/20260706/file.epub</span>
  <a href="/account/downloaded">Downloaded files</a>
</body></html>
"""

NOT_MEMBER_HTML = """
<html><body>
  <a href="/account/downloaded">Downloaded files</a>
  <a href="/donate">Become a member</a>
</body></html>
"""


def test_is_likely_file_url_accepts_aa_slow_cdn():
    url = "https://wbsg8v.xyz/d3/y/s/1785882941/500/g6/zlib3_files/20260706/aacid__x"
    assert aa._is_aa_slow_cdn_url(url)
    assert aa._is_likely_file_url(url)


def test_is_likely_file_url_rejects_account_pages():
    assert not aa._is_likely_file_url("https://annas-archive.gl/account/downloaded")
    assert not aa._is_likely_file_url("https://annas-archive.gl/fast_download_not_member")
    assert aa._is_aa_account_or_member_page("/account/downloaded")


def test_extract_direct_url_from_slow_page():
    url = aa._extract_direct_url_from_aa_html(SLOW_PAGE_HTML)
    assert url is not None
    assert url.startswith("https://wbsg8v.xyz/d3/")


def test_extract_direct_url_ignores_account_links():
    assert aa._extract_direct_url_from_aa_html(NOT_MEMBER_HTML) is None


def test_extract_download_urls_prefers_libgen_and_multiple_slow():
    with patch.object(aa.settings, "aa_account_id", "test-cookie"):
        urls = aa._extract_download_urls(DETAIL_HTML, "https://annas-archive.gl")
    assert urls[0].startswith("https://libgen.li/ads.php")
    assert any("/fast_download/" in u for u in urls)
    slow = [u for u in urls if "/slow_download/" in u]
    assert len(slow) >= 2
    zlib_idxs = [i for i, u in enumerate(urls) if "z-library" in u]
    assert not zlib_idxs


def test_extract_download_urls_zlib_only_uses_slow_fallback():
    with patch.object(aa.settings, "aa_account_id", "test-cookie"):
        urls = aa._extract_download_urls(ZLIB_ONLY_HTML, "https://annas-archive.gl")
    assert any("/fast_download/" in u for u in urls)
    slow = [u for u in urls if "/slow_download/" in u]
    assert len(slow) >= 2
    assert not any("z-library" in u for u in urls)


def test_cloudflare_detector_includes_ddos_guard():
    assert aa._is_cloudflare_challenge('<div class="ddg-l10n-description">Please wait</div>')