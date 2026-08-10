"""ABB HTML URLs must resolve to magnets before TorBox/RD - not be uploaded as torrents."""

from __future__ import annotations

import asyncio

import pytest

from app.services import audiobookbay, debrid


def test_is_abb_page_url():
    assert audiobookbay.is_abb_page_url(
        "https://audiobookbay.lu/abss/some-book/"
    )
    assert audiobookbay.is_abb_page_url("https://example.com/abss/foo")
    assert not audiobookbay.is_abb_page_url("magnet:?xt=urn:btih:abc")
    assert not audiobookbay.is_abb_page_url("https://jackett.local/dl/abc")
    assert not audiobookbay.is_abb_page_url(None)


def test_reject_non_torrent_body_html():
    with pytest.raises(RuntimeError, match="HTML instead of a .torrent"):
        debrid.reject_non_torrent_body(
            b"<!DOCTYPE html><html><body>cf</body></html>",
            "text/html; charset=utf-8",
        )
    with pytest.raises(RuntimeError, match="HTML instead of a .torrent"):
        debrid.reject_non_torrent_body(b"<html><head></head></html>", "")
    with pytest.raises(RuntimeError, match="empty body"):
        debrid.reject_non_torrent_body(b"", "application/x-bittorrent")


def test_reject_non_torrent_body_allows_bencode():
    debrid.reject_non_torrent_body(b"d8:announce", "application/x-bittorrent")


def test_abb_resolve_none_leaves_non_magnet_link(monkeypatch):
    async def _boom(*_a, **_k):
        return None, None, []

    monkeypatch.setattr(audiobookbay, "resolve_details_from_page", _boom)
    link = "https://audiobookbay.lu/abss/some-title/"
    assert audiobookbay.is_abb_page_url(link)
    m, _h, _files = asyncio.run(
        audiobookbay.resolve_details_from_page(link, title="t")
    )
    assert m is None
    assert not str(link).startswith("magnet:")


def test_missing_token_message_is_actionable():
    msg = RuntimeError("Torbox API token is not configured")
    assert "not configured" in str(msg)


def test_html_rejection_is_hard_failure():
    from app.services.pipeline import _is_hard_debrid_failure

    assert _is_hard_debrid_failure(
        RuntimeError(
            "Download URL returned HTML instead of a .torrent/magnet "
            "(indexer may require Cloudflare). Resolve the magnet before debrid."
        )
    )
    assert _is_hard_debrid_failure(
        RuntimeError("Torbox API token is not configured")
    )
