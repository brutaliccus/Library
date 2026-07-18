"""ABB cache hash resolution tests."""

from app.services.audiobookbay import _abb_details_url


def test_abb_details_url_prefers_guid_over_jackett_dl():
    row = {
        "guid": "http://audiobookbay.is/abss/some-book/",
        "downloadUrl": "http://audiobook-jackett:9117/dl/audiobookbay/?jackett_apikey=secret",
    }
    assert _abb_details_url(row) == "http://audiobookbay.is/abss/some-book/"


def test_abb_details_url_uses_direct_download_when_no_guid():
    row = {"downloadUrl": "http://audiobookbay.is/abss/direct/"}
    assert _abb_details_url(row) == "http://audiobookbay.is/abss/direct/"
