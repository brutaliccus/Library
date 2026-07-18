"""Unit tests for Hardcover helpers (no live API)."""

from app.services import hardcover


def test_hc_book_to_summary_basic():
    raw = {
        "id": 42,
        "title": "The Name of the Wind",
        "slug": "the-name-of-the-wind",
        "rating": 4.5,
        "ratings_count": 1000,
        "reviews_count": 200,
        "contributions": [{"author": {"name": "Patrick Rothfuss"}}],
        "isbns": ["9780756404741"],
        "image": {"url": "https://example.com/cover.jpg"},
        "featured_series": {"name": "The Kingkiller Chronicle", "position": 1},
    }
    out = hardcover._hc_book_to_summary(raw)
    assert out is not None
    assert out["title"] == "The Name of the Wind"
    assert out["authors"] == ["Patrick Rothfuss"]
    assert out["isbn13"] == "9780756404741"
    assert out["averageRating"] == 4.5
    assert out["seriesName"] == "The Kingkiller Chronicle"
    assert out["id"].startswith("HC:")


def test_extract_search_docs_typesense():
    results = {
        "found": 1,
        "hits": [{"document": {"id": 1, "title": "Dungeon Crawler Carl", "series_names": ["Dungeon Crawler Carl"]}}],
    }
    docs = hardcover._extract_search_docs(results)
    assert len(docs) == 1
    assert docs[0]["title"] == "Dungeon Crawler Carl"


def test_score_list_prefers_liked_genre_list():
    weak = {"id": 1, "name": "Best Fantasy", "likes_count": 0, "followers_count": 0, "books_count": 45}
    strong = {
        "id": 108,
        "name": "The 31 Best Fantasy Books Everyone Should Read",
        "likes_count": 109,
        "followers_count": 40,
        "books_count": 30,
    }
    junk = {
        "id": 471,
        "name": "The 50 Best Manga You Must Read Right Now",
        "likes_count": 1,
        "followers_count": 0,
        "books_count": 49,
    }
    off_topic = {
        "id": 83400,
        "name": "The Esquire 75 Best Sci-Fi Books of All Time",
        "likes_count": 191,
        "followers_count": 0,
        "books_count": 75,
    }
    fantasy_terms = ["fantasy"]
    horror_terms = ["horror"]
    assert hardcover._score_list_doc(strong, require_terms=fantasy_terms) > hardcover._score_list_doc(
        weak, require_terms=fantasy_terms
    )
    assert hardcover._score_list_doc(strong, require_terms=fantasy_terms) > hardcover._score_list_doc(
        junk, require_terms=fantasy_terms
    )
    assert hardcover._score_list_doc(off_topic, require_terms=horror_terms) < 0


def test_titles_compatible_rejects_loose_substring():
    assert hardcover._titles_compatible("The Fire Rose", "The Fire Rose")
    assert not hardcover._titles_compatible("It", "Little Women")


def test_curated_shelf_slugs_include_home_and_genre():
    slugs = hardcover.curated_shelf_slugs()
    assert "best-fantasy" in slugs
    assert "fantasy" in slugs
