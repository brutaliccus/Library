"""Request cover/title/author refresh after metadata match."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.forge_pipeline import (
    refresh_request_display_metadata,
    title_author_from_staging,
)


def test_title_author_from_staging_metadata_json(tmp_path: Path):
    meta = tmp_path / "metadata.json"
    meta.write_text(
        json.dumps(
            {
                "title": "Dune",
                "authors": ["Frank Herbert"],
                "cover_url": "https://images.example/dune.jpg",
            }
        ),
        encoding="utf-8",
    )
    title, author = title_author_from_staging(tmp_path)
    assert title == "Dune"
    assert author == "Frank Herbert"


def test_refresh_request_display_overwrites_stale_cover(tmp_path: Path):
    meta = tmp_path / "metadata.json"
    meta.write_text(
        json.dumps(
            {
                "title": "Project Hail Mary",
                "author": "Andy Weir",
                "cover_url": "https://images.example/phm.jpg",
            }
        ),
        encoding="utf-8",
    )

    req = MagicMock()
    req.id = 42
    req.user_id = 7
    req.status = "metadata_forge"
    req.status_detail = "Matching…"
    req.title = "Andy Weir"
    req.author = "Project Hail Mary"
    req.cover_url = "https://placeholder.example/old.jpg"

    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    result = MagicMock()
    result.scalar_one_or_none.return_value = req
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()

    with (
        patch("app.services.forge_pipeline.async_session", return_value=session),
        patch("app.utils.websocket.ws_manager.send_to_user", new_callable=AsyncMock) as send_ws,
    ):
        updated = asyncio.run(refresh_request_display_metadata(42, tmp_path))

    assert updated["title"] == "Project Hail Mary"
    assert updated["author"] == "Andy Weir"
    assert updated["cover_url"] == "https://images.example/phm.jpg"
    assert req.title == "Project Hail Mary"
    assert req.author == "Andy Weir"
    assert req.cover_url == "https://images.example/phm.jpg"
    session.commit.assert_awaited()
    send_ws.assert_awaited()
