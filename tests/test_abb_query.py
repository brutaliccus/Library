"""ABB query resolution for Jackett search."""

from app.services.download_discovery import (
    build_audiobookbay_queries,
    build_knaben_queries,
    resolve_abb_search_query,
    resolve_book_search_context,
)


def test_build_knaben_queries_includes_author():
    ctx = resolve_book_search_context(
        title="Dungeon Crawler Carl",
        author="Matt Dinniman",
        series_index="1",
    )
    queries = build_knaben_queries(ctx)
    # Never author-only — that floods unrelated titles from the same author
    assert "Matt Dinniman" not in queries
    assert "Dungeon Crawler Carl" in queries
    assert any("Dungeon Crawler Carl" in q and "Matt Dinniman" in q for q in queries)


def test_resolve_abb_search_query_manual_override():
    ctx = resolve_book_search_context(
        title="Dungeon Crawler Carl",
        author="Matt Dinniman",
        series_index="7",
    )
    assert resolve_abb_search_query(ctx, "carl book 7 m4b") == "carl book 7 m4b"


def test_resolve_abb_search_query_falls_back_to_built():
    ctx = resolve_book_search_context(
        title="Dungeon Crawler Carl",
        author="Matt Dinniman",
        series_index="7",
    )
    built = build_audiobookbay_queries(ctx)
    assert resolve_abb_search_query(ctx, None) == built[0]
    assert resolve_abb_search_query(ctx, "   ") == built[0]
