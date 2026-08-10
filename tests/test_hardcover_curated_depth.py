"""Curated Hardcover shelves must stay within GraphQL depth=3."""
from __future__ import annotations

import asyncio

import pytest

from app.services import hardcover


@pytest.fixture(autouse=True)
def _clear_hc_cache(monkeypatch: pytest.MonkeyPatch):
    hardcover._cache.clear()

    async def _key() -> str:
        return "Bearer test"

    monkeypatch.setattr(hardcover, "get_api_key", _key)


def test_get_curated_shelf_hydrates_books_without_deep_list_nesting(monkeypatch):
    calls: list[str] = []

    async def fake_graphql(query: str, variables: dict | None = None):
        q = " ".join(query.split())
        if "SearchLists" in q or 'query_type: "List"' in q:
            calls.append("search")
            return {
                "search": {
                    "results": {
                        "hits": [
                            {
                                "document": {
                                    "id": 42,
                                    "name": "Best Fantasy Books Everyone Should Read",
                                    "likes_count": 500,
                                    "followers_count": 100,
                                    "books_count": 31,
                                }
                            }
                        ]
                    }
                }
            }
        if "ListBooks" in q:
            calls.append("list")
            assert "image {" not in q
            assert "contributions" not in q
            assert "editions" not in q
            return {
                "lists": [
                    {
                        "id": 42,
                        "name": "Best Fantasy Books Everyone Should Read",
                        "list_books": [
                            {
                                "position": 1,
                                "book": {
                                    "id": 101,
                                    "title": "The Name of the Wind",
                                    "subtitle": "",
                                    "slug": "the-name-of-the-wind",
                                    "rating": 4.5,
                                    "ratings_count": 1000,
                                    "reviews_count": 50,
                                    "pages": 662,
                                    "release_year": 2007,
                                },
                            },
                            {
                                "position": 2,
                                "book": {
                                    "id": 102,
                                    "title": "The Hobbit",
                                    "slug": "the-hobbit",
                                    "rating": 4.3,
                                    "ratings_count": 2000,
                                    "reviews_count": 80,
                                    "pages": 310,
                                    "release_year": 1937,
                                },
                            },
                        ],
                    }
                ]
            }
        if "BooksByIds" in q:
            calls.append("hydrate")
            return {
                "books": [
                    {
                        "id": 101,
                        "title": "The Name of the Wind",
                        "slug": "the-name-of-the-wind",
                        "rating": 4.5,
                        "ratings_count": 1000,
                        "reviews_count": 50,
                        "pages": 662,
                        "release_year": 2007,
                        "image": {"url": "https://example.com/notw.jpg"},
                        "contributions": [{"author": {"name": "Patrick Rothfuss"}}],
                        "editions": [{"isbn_13": "9780756404741"}],
                    },
                    {
                        "id": 102,
                        "title": "The Hobbit",
                        "slug": "the-hobbit",
                        "rating": 4.3,
                        "ratings_count": 2000,
                        "reviews_count": 80,
                        "pages": 310,
                        "release_year": 1937,
                        "image": {"url": "https://example.com/hobbit.jpg"},
                        "contributions": [{"author": {"name": "J.R.R. Tolkien"}}],
                        "editions": [{"isbn_13": "9780547928227"}],
                    },
                ]
            }
        calls.append("other")
        return {}

    monkeypatch.setattr(hardcover, "_graphql", fake_graphql)

    shelf = asyncio.run(hardcover.get_curated_shelf("best-fantasy", limit=12))
    assert shelf["source"] == "hardcover"
    assert shelf["listId"] == 42
    assert len(shelf["books"]) == 2
    assert shelf["books"][0]["title"] == "The Name of the Wind"
    assert shelf["books"][0]["authors"] == ["Patrick Rothfuss"]
    assert "notw.jpg" in (shelf["books"][0].get("coverUrl") or "")
    assert "search" in calls and "list" in calls and "hydrate" in calls


def test_get_curated_shelf_falls_back_to_search_when_list_rows_missing(monkeypatch):
    async def fake_graphql(query: str, variables: dict | None = None):
        q = " ".join(query.split())
        if 'query_type: "List"' in q:
            return {
                "search": {
                    "results": {
                        "hits": [
                            {
                                "document": {
                                    "id": 7,
                                    "name": "Best Horror Books of All Time",
                                    "likes_count": 200,
                                    "followers_count": 40,
                                    "books_count": 25,
                                }
                            }
                        ]
                    }
                }
            }
        if "ListBooks" in q:
            return {}
        return {}

    async def fake_search(query: str, *, limit: int = 10, page: int = 1):
        return [
            {
                "id": "HC:1",
                "volumeId": "HC:1",
                "title": "The Shining",
                "authors": ["Stephen King"],
                "coverUrl": "",
            }
        ]

    monkeypatch.setattr(hardcover, "_graphql", fake_graphql)
    monkeypatch.setattr(hardcover, "search_books", fake_search)

    shelf = asyncio.run(hardcover.get_curated_shelf("best-horror", limit=12))
    assert shelf["books"]
    assert shelf["books"][0]["title"] == "The Shining"
    assert shelf["source"] == "hardcover_search"
