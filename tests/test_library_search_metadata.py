"""Library search matches broadly across local metadata fields."""

from __future__ import annotations

from app.routers import library as library_router


def test_search_tokens_split_and_casefold():
    assert library_router._search_tokens("  Vampire Chronicles ") == ["vampire", "chronicles"]
    assert library_router._search_tokens("") == []


def test_tokens_match_series_and_description():
    item = {
        "title": "Blood Canticle",
        "author": "Anne Rice",
        "seriesName": "The Vampire Chronicles",
        "narrator": "Simon Vance",
        "genres": ["Fantasy"],
        "asin": "B000XXXX",
        "subtitle": "",
        "description": "A vampire tale in New Orleans.",
    }
    assert library_router._tokens_match_metadata(item, ["vampire"])
    assert library_router._tokens_match_metadata(item, ["anne", "rice"])
    assert library_router._tokens_match_metadata(item, ["simon"])
    assert library_router._tokens_match_metadata(item, ["b000xxxx"])
    assert not library_router._tokens_match_metadata(item, ["mayfair"])


def test_tokens_require_all_tokens():
    item = {"title": "Blood Canticle", "author": "Anne Rice", "seriesName": "Vampire"}
    assert library_router._tokens_match_metadata(item, ["blood", "rice"])
    assert not library_router._tokens_match_metadata(item, ["blood", "hobbit"])
