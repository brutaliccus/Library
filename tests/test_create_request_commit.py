"""Create-request must commit before scheduling download background tasks."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.routers import requests as requests_router
from app.routers.requests import CreateDownloadRequest


def _admin_user() -> SimpleNamespace:
    return SimpleNamespace(id=7, private_mode=False)


@pytest.mark.parametrize(
    "body,expected_task",
    [
        (
            CreateDownloadRequest(
                title="Test Book",
                author="Author",
                magnet_link="magnet:?xt=urn:btih:abcd",
                media_type="audiobook",
            ),
            "process_download",
        ),
        (
            CreateDownloadRequest(
                title="AA Book",
                author="Author",
                source="annas_archive",
                aa_md5="deadbeefcafebabe",
                media_type="ebook",
                aa_file_extension="epub",
            ),
            "process_aa_download",
        ),
    ],
)
def test_create_request_commits_before_scheduling(body, expected_task):
    """Regression: flush-only left background workers racing an uncommitted row."""

    async def _run():
        call_order: list[str] = []

        dl = SimpleNamespace(
            id=0,
            user_id=7,
            title=body.title,
            author=body.author,
            magnet_link="",
            indexer=None,
            size_bytes=None,
            media_type=body.media_type,
            status="pending",
            status_detail=None,
            progress_percent=None,
            progress_bytes=None,
            progress_total_bytes=None,
            progress_speed_bps=None,
            rd_torrent_id=None,
            aa_file_extension=None,
            is_private=False,
            google_volume_id=None,
            cover_url=None,
            created_at=None,
            completed_at=None,
            debrid_provider=None,
            libraforge_run_id=None,
            error_message=None,
        )

        db = AsyncMock()
        db.add = MagicMock()

        async def _commit():
            call_order.append("commit")
            dl.id = 99

        async def _refresh(obj):
            call_order.append("refresh")
            assert obj is dl
            assert dl.id == 99

        db.commit = AsyncMock(side_effect=_commit)
        db.refresh = AsyncMock(side_effect=_refresh)
        db.flush = AsyncMock()

        def _create_task(coro):
            call_order.append("create_task")
            # Close the coroutine so pytest does not warn about unawaited coro.
            if asyncio.iscoroutine(coro):
                coro.close()
            return MagicMock()

        with (
            patch.object(requests_router.google_books, "lookup_cover_url", new=AsyncMock(return_value="")),
            patch.object(requests_router.asyncio, "create_task", side_effect=_create_task),
            patch.object(requests_router, "process_download", new=AsyncMock()) as pd,
            patch.object(requests_router, "process_aa_download", new=AsyncMock()) as paa,
            patch.object(requests_router, "DownloadRequest", return_value=dl),
            patch.object(
                requests_router,
                "_to_response",
                side_effect=lambda r: SimpleNamespace(id=r.id, title=r.title),
            ),
        ):
            result = await requests_router.create_request(body, user=_admin_user(), db=db)

        assert result.id == 99
        db.flush.assert_not_awaited()
        db.commit.assert_awaited_once()
        db.refresh.assert_awaited_once()
        assert call_order == ["commit", "refresh", "create_task"]
        if expected_task == "process_download":
            pd.assert_called_once_with(99)
            paa.assert_not_called()
        else:
            paa.assert_called_once_with(99)
            pd.assert_not_called()

    asyncio.run(_run())


def test_normalize_app_url_collapses_duplicate_schemes():
    from app.utils.app_url import normalize_app_url

    assert normalize_app_url("  https://https://library.example.com/  ") == "https://library.example.com"
    assert normalize_app_url("http://http://192.168.1.10:8085") == "http://192.168.1.10:8085"
    assert normalize_app_url("library.example.com") == "https://library.example.com"
    assert normalize_app_url("https://library.example.com/", strip_trailing_slash=False) == (
        "https://library.example.com/"
    )