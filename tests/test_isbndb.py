"""ISBNdb normalization helpers."""

from app.services.isbndb import _normalize_book


def test_normalize_book_isbn13():
    book = _normalize_book(
        {
            "title": "Dungeon Crawler Carl",
            "authors": ["Matt Dinniman"],
            "isbn13": "9780593820247",
            "isbn": "0593820249",
            "image": "http://images.isbndb.com/covers/02/47/9780593820247.jpg",
            "publisher": "Ace",
            "date_published": "2024",
            "subjects": ["Fiction", "Science Fiction"],
            "synopsis": "A litRPG adventure.",
            "pages": 464,
        }
    )
    assert book is not None
    assert book["volumeId"] == "ISBN:9780593820247"
    assert book["title"] == "Dungeon Crawler Carl"
    assert book["authors"] == ["Matt Dinniman"]
    assert book["coverUrl"].startswith("https://")
    assert book["isbn13"] == "9780593820247"


def test_normalize_book_requires_title():
    assert _normalize_book({"isbn13": "9780593820247"}) is None
