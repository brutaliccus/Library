"""Unit tests for Anna's Archive libgen.li download resolution."""

from app.services import annas_archive as aa


LIBGEN_ADS_HTML = """
<html><body>
<a href="#">DOWNLOAD</a>
<a href="get.php?md5=c24fb619df6a96ba72271622a936eaf8&amp;key=ABC123XYZ">GET</a>
<a href="https://wiki.mhut.org/software:libgen_desktop">Libgen librarian</a>
</body></html>
"""


def test_extract_libgen_get_links_resolves_relative():
    page = "https://libgen.li/ads.php?md5=c24fb619df6a96ba72271622a936eaf8"
    links = aa._extract_libgen_get_links(LIBGEN_ADS_HTML, page)
    assert links
    assert links[0].startswith("https://libgen.li/get.php?")
    assert "md5=c24fb619df6a96ba72271622a936eaf8" in links[0]
    assert "key=ABC123XYZ" in links[0]


def test_is_libgen_direct_get():
    assert aa._is_libgen_direct_get(
        "https://libgen.li/get.php?md5=abc&key=1"
    )
    assert aa._is_libgen_direct_get(
        "https://cdn5.booksdl.lc/get.php?md5=abc&key=1"
    )
    assert not aa._is_libgen_direct_get(
        "https://annas-archive.gl/slow_download/abc/0/0"
    )


def test_timer_page_is_aa_slow_only():
    assert aa._is_timer_page_url(
        "https://annas-archive.gl/slow_download/abc/0/0"
    )
    assert not aa._is_timer_page_url(
        "https://libgen.li/get.php?md5=abc&key=1"
    )


def test_host_allows_flaresolverr_blocks_libgen_li():
    assert not aa._host_allows_flaresolverr("https://libgen.li/ads.php?md5=x")
    assert aa._host_allows_flaresolverr(
        "https://annas-archive.gl/slow_download/x/0/0"
    )


def test_partner_rank_prefers_libgen_li_ads():
    html = """
    <html><body>
      <a class="js-download-link" href="https://z-lib.gd/md5/abc">z</a>
      <a class="js-download-link" href="https://libgen.li/ads.php?md5=abc">lg</a>
      <a class="js-download-link" href="https://archive.org/details/foo">ia</a>
      <a href="/slow_download/abc/0/0">slow</a>
    </body></html>
    """
    urls = aa._extract_download_urls(html, "https://annas-archive.gl")
    assert urls[0].startswith("https://libgen.li/ads.php")
    assert urls[-1].endswith("/slow_download/abc/0/0")


def test_build_session_skips_aa_cookie_for_partners(monkeypatch):
    monkeypatch.setattr(aa.settings, "aa_account_id", "secret-token")
    partner = aa._build_session(for_annas_archive=False)
    aa_client = aa._build_session(for_annas_archive=True)
    assert partner.cookies.get("aa_account_id2") is None
    assert aa_client.cookies.get("aa_account_id2") == "secret-token"
