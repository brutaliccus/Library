"""NYT Bestsellers API integration for real trending/bestseller data.

Requires NYT_API_KEY in config. Free tier: 1,000 requests/day.
Fetches current bestseller lists and enriches with Google Books data for covers/metadata.
"""

import logging
from typing import Any

import httpx

from app.config import get_settings
from app.services import google_books

logger = logging.getLogger(__name__)
settings = get_settings()

NYT_BASE = "https://api.nytimes.com/svc/books/v3/lists"
# Combined print+ebook fiction is a good "trending" list; hardcover-fiction is classic
TRENDING_LISTS = [
    "combined-print-and-e-book-fiction",
    "hardcover-fiction",
    "combined-print-and-e-book-nonfiction",
]


async def fetch_bestsellers(list_name: str, max_books: int = 20) -> list[dict[str, Any]]:
    """Fetch a NYT bestseller list and enrich with Google Books metadata."""
    api_key = getattr(settings, "nyt_api_key", "") or getattr(settings, "nyt_books_api_key", "")
    if not api_key:
        return []

    url = f"{NYT_BASE}/current/{list_name}.json"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params={"api-key": api_key})
            if resp.status_code != 200:
                logger.warning("NYT API error %s: %s", resp.status_code, resp.text[:200])
                return []
            data = resp.json()
    except Exception as e:
        logger.warning("NYT API fetch failed: %s", e)
        return []

    results = data.get("results", {})
    books_raw = results.get("books", [])
    if not books_raw:
        return []

    out: list[dict[str, Any]] = []
    for b in books_raw[:max_books]:
        # NYT current list: books have title, author, primary_isbn13, book_image at top level
        isbn13 = b.get("primary_isbn13", "")
        if not isbn13 and b.get("isbns"):
            isbns = b.get("isbns", [])
            isbn13 = next((i.get("isbn13", "") for i in isbns if isinstance(i, dict) and len(i.get("isbn13", "")) == 13), "")
        title = b.get("title", "")
        author = b.get("author", "")

        # Look up in Google Books for cover and full metadata
        if isbn13:
            gb_result = await google_books.search_volumes(
                f"isbn:{isbn13}", max_results=1, order_by="relevance"
            )
            gb_books = gb_result.get("books", [])
            if gb_books:
                out.append(gb_books[0])
                continue

        # Fallback: use title+author search
        if title and author:
            gb_result = await google_books.search_volumes(
                f"intitle:{title} inauthor:{author}", max_results=1, order_by="relevance"
            )
            gb_books = gb_result.get("books", [])
            if gb_books:
                out.append(gb_books[0])
                continue

        # Last resort: minimal book from NYT data (has book_image, etc. at top level)
        out.append({
            "id": f"nyt-{isbn13 or title}",
            "title": title,
            "authors": [author] if author else [],
            "coverUrl": b.get("book_image", ""),
            "isbn13": isbn13,
            "isbn10": b.get("primary_isbn10", ""),
            "description": b.get("description", ""),
            "publisher": b.get("publisher", ""),
            "publishedDate": b.get("published_date", ""),
            "averageRating": 0,
            "ratingsCount": 0,
        })
    return out


async def get_trending_from_nyt(max_results: int = 20) -> list[dict[str, Any]]:
    """Fetch real bestsellers from NYT. Returns empty if no API key or on error."""
    seen_ids: set[str] = set()
    out: list[dict[str, Any]] = []
    for list_name in TRENDING_LISTS:
        if len(out) >= max_results:
            break
        books = await fetch_bestsellers(list_name, max_books=max_results - len(out))
        for b in books:
            bid = b.get("id", "") or b.get("title", "")
            if bid and bid not in seen_ids:
                seen_ids.add(bid)
                out.append(b)
                if len(out) >= max_results:
                    break
    return out
