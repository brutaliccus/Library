"""ABS sidecar → API metadata sync (post–Folder Forge finalize)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.services import audiobookshelf as abs_svc


def test_metadata_payload_from_libraforge_sidecar(tmp_path: Path):
    (tmp_path / "libraforge.json").write_text(
        json.dumps(
            {
                "marker": {
                    "audible": {
                        "title": "Illidan: World of Warcraft",
                        "subtitle": "A Novel",
                        "author": "William King",
                        "narrator": "Graeme Malcolm",
                        "series": "World of Warcraft",
                        "asin": "B01DYF5XCW",
                        "publisher": "Random House Audio",
                        "year": "2016",
                        "genre": "Epic, Action & Adventure",
                        "cover_url": "https://example.com/cover.jpg",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    payload, cover = abs_svc._metadata_payload_from_book_dir(tmp_path)
    assert payload["title"] == "Illidan: World of Warcraft"
    assert payload["asin"] == "B01DYF5XCW"
    assert payload["authors"] == [{"name": "William King"}]
    assert payload["narrators"] == ["Graeme Malcolm"]
    assert payload["series"][0]["name"] == "World of Warcraft"
    assert "Epic" in payload["genres"][0]
    assert cover == "https://example.com/cover.jpg"


def test_metadata_payload_uses_summary_and_skips_title_series(tmp_path: Path):
    (tmp_path / "libraforge.json").write_text(
        json.dumps(
            {
                "marker": {
                    "audible": {
                        "title": "Timeline",
                        "author": "Michael Crichton",
                        "narrator": "John Bedford Lloyd",
                        "series": "Timeline",
                        "asin": "B002VA96S4",
                        "summary": "A long description from Audible.",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    payload, _cover = abs_svc._metadata_payload_from_book_dir(tmp_path)
    assert payload["description"] == "A long description from Audible."
    assert "series" not in payload


def test_sync_book_dir_pushes_metadata_without_match(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(abs_svc.settings, "audiobook_dir", str(tmp_path))
    book = tmp_path / "Author" / "Book"
    book.mkdir(parents=True)
    (book / "metadata.json").write_text(
        json.dumps({"title": "Book Title", "authors": ["Author"], "asin": "B012345678"}),
        encoding="utf-8",
    )

    async def _run():
        with (
            patch.object(
                abs_svc,
                "find_item_by_rel_path",
                new=AsyncMock(return_value={"id": "item-1", "relPath": "Author/Book"}),
            ),
            patch.object(abs_svc, "update_item_metadata", new=AsyncMock(return_value=True)) as upd,
            patch.object(abs_svc, "set_item_cover_from_url", new=AsyncMock(return_value=False)),
            patch.object(abs_svc, "update_item_chapters", new=AsyncMock(return_value=False)),
            patch.object(abs_svc, "invalidate_cache"),
            patch.object(abs_svc, "match_item", new=AsyncMock()) as match,
        ):
            out = await abs_svc.sync_book_dir_metadata_to_abs(book)

        assert out["updated"] is True
        assert out["item_id"] == "item-1"
        upd.assert_awaited_once()
        meta = upd.await_args.kwargs["metadata"]
        assert meta["title"] == "Book Title"
        assert meta["asin"] == "B012345678"
        match.assert_not_awaited()

    asyncio.run(_run())


def test_match_item_skips_when_asin_present():
    async def _run():
        with (
            patch.object(
                abs_svc,
                "get_library_item",
                new=AsyncMock(
                    return_value={"media": {"metadata": {"asin": "B002VA96S4", "title": "Timeline"}}}
                ),
            ),
            patch.object(abs_svc.httpx, "AsyncClient") as client_cls,
        ):
            out = await abs_svc.match_item("item-1", override_defaults=False)

        assert out == {
            "skipped": True,
            "reason": "asin_present",
            "asin": "B002VA96S4",
            "updated": False,
        }
        client_cls.assert_not_called()

    asyncio.run(_run())
