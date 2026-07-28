"""TorBox status mapping and debrid fallback eligibility."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services import torbox
from app.services.pipeline import _is_hard_debrid_failure


@pytest.mark.parametrize(
    "download_state,finished,expected",
    [
        ("downloading", False, "downloading"),
        ("queued", False, "queued"),
        ("stalled (no seeds)", False, "downloading"),
        ("metaDL", False, "downloading"),
        ("checkingResumeData", False, "downloading"),
        ("paused", False, "downloading"),
        ("uploading", False, "downloading"),
        ("completed", False, "downloading"),  # TorBox: not download_finished
        ("cached", False, "downloading"),
        ("", False, "downloading"),
        ("someUnknownQbtState", False, "downloading"),
        ("error", False, "error"),
        ("failed", False, "error"),
        ("missingFiles", False, "error"),
        ("dead", False, "error"),
        ("expired", False, "error"),
        ("downloading", True, "downloaded"),
        ("error", True, "downloaded"),  # finished flags win
    ],
)
def test_map_download_state(download_state, finished, expected):
    assert torbox.map_download_state(download_state, finished=finished) == expected


def test_normalize_info_preserves_raw_download_state():
    info = torbox._normalize_info(
        {
            "id": 42,
            "name": "Book",
            "hash": "ABC",
            "download_finished": False,
            "download_present": False,
            "download_state": "stalled (no seeds)",
            "progress": 0.25,
            "download_speed": 1024,
            "files": [],
        }
    )
    assert info["status"] == "downloading"
    assert info["download_state"] == "stalled (no seeds)"
    assert info["progress"] == 25
    assert info["id"] == "42"


def test_downloading_is_not_hard_failure():
    assert not _is_hard_debrid_failure(
        RuntimeError("Torbox torrent did not complete within 100s")
    )
    assert not _is_hard_debrid_failure(TimeoutError("still going"))
    assert not _is_hard_debrid_failure(
        TypeError("poll_until_ready() got an unexpected keyword argument 'on_progress'")
    )
    assert not _is_hard_debrid_failure(
        httpx.TimeoutException("request timed out")
    )


def test_hard_failures_allow_fallback():
    assert _is_hard_debrid_failure(
        RuntimeError("Torbox torrent failed with status: error (download_state='error')")
    )
    assert _is_hard_debrid_failure(
        RuntimeError("Real-Debrid torrent failed with status: dead")
    )
    assert _is_hard_debrid_failure(RuntimeError("Torbox createtorrent failed: rejected"))
    assert _is_hard_debrid_failure(RuntimeError("unauthorized / invalid token"))


def test_http_429_is_not_hard_failure():
    req = httpx.Request("GET", "https://api.torbox.app/v1/api/torrents/mylist")
    resp = httpx.Response(429, request=req)
    assert not _is_hard_debrid_failure(httpx.HTTPStatusError("rate limited", request=req, response=resp))


def test_http_500_is_not_hard_but_pre_create_fallback_uses_any_error():
    """5xx is soft for post-create; pre-create path falls back on any error."""
    req = httpx.Request("POST", "https://api.torbox.app/v1/api/torrents/createtorrent")
    resp = httpx.Response(500, request=req)
    assert not _is_hard_debrid_failure(
        httpx.HTTPStatusError("server error", request=req, response=resp)
    )


def test_download_provider_order_excludes_failed_provider():
    from app.services import debrid

    with patch.object(
        debrid,
        "available_providers",
        return_value=[debrid.RD, debrid.TORBOX],
    ):
        assert debrid.download_provider_order(
            debrid.TORBOX, debrid.TORBOX, exclude=[debrid.TORBOX]
        ) == [debrid.RD]
        assert debrid.pick_provider(
            "abcdef0123456789abcdef0123456789abcdef01",
            {debrid.RD: set(), debrid.TORBOX: {"abcdef0123456789abcdef0123456789abcdef01"}},
            debrid.RD,
            exclude=[debrid.TORBOX],
        ) == debrid.RD


def test_poll_until_ready_accepts_on_progress():
    """Regression: pipeline always passes on_progress; TorBox must accept it."""
    calls: list[dict] = []

    async def _progress(info: dict) -> None:
        calls.append(info)

    infos = [
        {
            "id": "1",
            "status": "downloading",
            "download_state": "downloading",
            "progress": 10,
            "speed": 0,
            "files": [],
            "links": [],
        },
        {
            "id": "1",
            "status": "downloaded",
            "download_state": "cached",
            "progress": 100,
            "speed": 0,
            "files": [],
            "links": ["torbox://1/0/a.m4b"],
        },
    ]

    async def _run():
        with patch.object(torbox, "get_torrent_info", AsyncMock(side_effect=infos)):
            with patch.object(torbox.asyncio, "sleep", AsyncMock()):
                return await torbox.poll_until_ready("1", interval=0.01, on_progress=_progress)

    result = asyncio.run(_run())
    assert result["status"] == "downloaded"
    assert len(calls) == 2
    assert calls[0]["status"] == "downloading"
