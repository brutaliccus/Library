"""Admin Health ebook counts must match My Library shelf expansion."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from app.routers import library as library_router


def test_kavita_ebook_inventory_counts_shelf_cards_not_series():
    """Two ebook series with 2+1 volumes -> shelf 3, series 2 (not series-only)."""

    all_series = [
        {"id": 1, "name": "Series A", "format": 3, "created": 0},
        {"id": 2, "name": "Standalone", "format": 4, "created": 0},
        {"id": 3, "name": "Comic", "format": 1, "created": 0},  # not ebook
    ]
    volumes = {
        1: [
            {
                "id": 10,
                "number": 1,
                "chapters": [{"id": 100, "files": [{"filePath": "/a/Book1.epub"}]}],
            },
            {
                "id": 11,
                "number": 2,
                "chapters": [{"id": 101, "files": [{"filePath": "/a/Book2.epub"}]}],
            },
        ],
        2: [
            {
                "id": 20,
                "number": 1,
                "chapters": [{"id": 200, "files": [{"filePath": "/b/Solo.epub"}]}],
            },
        ],
    }

    async def fake_volumes(sid: int):
        return volumes.get(sid, [])

    async def _run():
        with (
            patch.object(
                library_router.kavita,
                "get_all_series",
                new=AsyncMock(return_value=all_series),
            ),
            patch.object(
                library_router.kavita,
                "get_series_volumes",
                new=AsyncMock(side_effect=fake_volumes),
            ),
        ):
            return await library_router.kavita_ebook_inventory(force_refresh=True)

    inv = asyncio.run(_run())
    assert inv["series_count"] == 3
    assert inv["ebook_series_count"] == 2
    assert inv["ebook_count"] == 3  # shelf cards, matching My Library totalItems