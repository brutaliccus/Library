"""Hardcover.app GraphQL API — ratings, series, curated lists (public catalog data only).

No user-library sync. Token is used only as a backend API key for public book metadata.
Docs: https://docs.hardcover.app/
Endpoint: https://api.hardcover.app/v1/graphql
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

API_URL = "https://api.hardcover.app/v1/graphql"
API_KEY_SETTING = "integrations.hardcover_api_key"
USER_AGENT = "LibrarySite/1.0 (+https://library.example.com)"

# Genre slug → Hardcover list search (used on genre hub pages, not the main store).
GENRE_LIST_QUERIES: dict[str, list[str]] = {
    "fantasy": [
        "The 31 Best Fantasy Books Everyone Should Read",
        "best fantasy books",
        "epic fantasy",
    ],
    "science-fiction": [
        "The Esquire 75 Best Sci-Fi Books of All Time",
        "best sci-fi books",
        "best science fiction",
    ],
    "mystery": [
        "best mystery novels",
        "best detective fiction",
        "mystery must read",
    ],
    "thriller": [
        "best psychological thrillers",
        "best thriller novels",
        "thriller must read",
    ],
    "romance": [
        "best romance novels of all time",
        "best romance books",
    ],
    "horror": [
        "best horror novels",
        "best horror books of all time",
        "Bram Stoker Award for Best Horror Novel",
    ],
    "young-adult": [
        "best young adult books",
        "best YA fantasy",
        "best young adult novels",
    ],
    "literary-fiction": [
        "best literary fiction",
        "best novels of all time",
        "modern classics",
    ],
    "historical-fiction": [
        "best historical fiction",
        "best historical novels",
    ],
    "nonfiction": [
        "best nonfiction books",
        "best non-fiction of all time",
    ],
}

# Main store recommendation shelves — curated list titles, not plain genre browse.
# Ordered for home infinite-scroll; each entry resolves one Hardcover community list.
HOME_CURATED_SHELVES: list[dict[str, Any]] = [
    {
        "slug": "best-fantasy",
        "queries": ["The 31 Best Fantasy Books Everyone Should Read", "best fantasy books"],
        "require_terms": ["fantasy"],
        "genre": "fantasy",
    },
    {
        "slug": "best-scifi",
        "queries": ["The Esquire 75 Best Sci-Fi Books of All Time", "best sci-fi books of all time"],
        "require_terms": ["sci fi", "sci-fi", "science fiction", "sf"],
        "genre": "science-fiction",
    },
    {
        "slug": "best-mystery",
        "queries": ["best mystery novels", "best detective fiction"],
        "require_terms": ["mystery", "detective", "crime"],
        "genre": "mystery",
    },
    {
        "slug": "best-thriller",
        "queries": ["best psychological thrillers", "best thriller novels"],
        "require_terms": ["thriller"],
        "genre": "thriller",
    },
    {
        "slug": "best-horror",
        "queries": ["best horror books of all time", "best horror novels"],
        "require_terms": ["horror"],
        "genre": "horror",
    },
    {
        "slug": "best-romance",
        "queries": ["best romance novels of all time", "best romance books"],
        "require_terms": ["romance"],
        "genre": "romance",
    },
    {
        "slug": "best-ya",
        "queries": ["best young adult books", "best YA novels"],
        "require_terms": ["young adult", "ya "],
        "genre": "young-adult",
    },
    {
        "slug": "best-literary",
        "queries": ["best literary fiction", "100 best novels"],
        "require_terms": ["literary", "novel", "classic"],
        "genre": "literary-fiction",
    },
    {
        "slug": "best-historical",
        "queries": ["best historical fiction", "best historical novels"],
        "require_terms": ["historical"],
        "genre": "historical-fiction",
    },
    {
        "slug": "best-nonfiction",
        "queries": ["best nonfiction books", "best non-fiction books of all time"],
        "require_terms": ["nonfiction", "non-fiction", "non fiction"],
        "genre": "nonfiction",
    },
    {
        "slug": "hugo-nebula",
        "queries": ["Hugo Award winners", "Nebula Award winners"],
        "require_terms": ["hugo", "nebula", "award"],
        "genre": "science-fiction",
    },
    {
        "slug": "booker-pulitzer",
        "queries": ["Booker Prize winners", "Pulitzer Prize for Fiction"],
        "require_terms": ["booker", "pulitzer", "prize", "award"],
        "genre": "literary-fiction",
    },
    {
        "slug": "best-fantasy-series",
        "queries": ["best fantasy series", "must read fantasy series"],
        "require_terms": ["fantasy"],
        "genre": "fantasy",
    },
    {
        "slug": "best-scifi-classics",
        "queries": ["classic science fiction", "best classic sci-fi"],
        "require_terms": ["sci", "science fiction", "classic"],
        "genre": "science-fiction",
    },
    {
        "slug": "best-cozy-mystery",
        "queries": ["best cozy mysteries", "cozy mystery must read"],
        "require_terms": ["cozy", "mystery"],
        "genre": "mystery",
    },
    {
        "slug": "best-dark-academia",
        "queries": ["best dark academia books", "dark academia must read"],
        "require_terms": ["dark academia", "academia"],
        "genre": "literary-fiction",
    },
    {
        "slug": "best-memoir",
        "queries": ["best memoirs of all time", "best memoir books"],
        "require_terms": ["memoir"],
        "genre": "nonfiction",
    },
    {
        "slug": "best-graphic-novels",
        "queries": ["best graphic novels", "best comics and graphic novels"],
        "require_terms": ["graphic", "comic"],
        "genre": "comics",
    },
]


def curated_shelf_slugs() -> set[str]:
    """Slugs that should load via /books/curated rather than plain genre browse."""
    return {s["slug"] for s in HOME_CURATED_SHELVES} | set(GENRE_LIST_QUERIES.keys())


def _home_shelf_by_slug(slug: str) -> dict[str, Any] | None:
    for entry in HOME_CURATED_SHELVES:
        if entry["slug"] == slug:
            return entry
    return None

_GENRE_NAME_TERMS: dict[str, list[str]] = {
    "fantasy": ["fantasy"],
    "science-fiction": ["science fiction", "sci fi", "sci-fi", " sf"],
    "mystery": ["mystery", "detective", "crime"],
    "thriller": ["thriller"],
    "romance": ["romance"],
    "horror": ["horror"],
    "young-adult": ["young adult", "ya "],
    "literary-fiction": ["literary", "fiction"],
    "historical-fiction": ["historical"],
    "nonfiction": ["nonfiction", "non-fiction", "non fiction"],
    "comics": ["graphic", "comic"],
}

# Optional allow-lists for compound shelves (e.g. fantasy may include "sci-fi and fantasy").
_GENRE_ALLOW_EXTRA: dict[str, list[str]] = {
    "fantasy": ["science-fiction and fantasy", "sci-fi and fantasy", "science fiction and fantasy"],
    "science-fiction": ["science-fiction and fantasy", "sci-fi and fantasy", "science fiction and fantasy"],
}

_LIST_NAME_PENALTY = (
    "manga", "splatterpunk", "magazine", "brother", "cliche", "cliché",
    "ya only", "fanfiction", "fan fiction", "partially read", "ever scanned",
    "recommends", "lgbtq",
)

_cache: dict[str, tuple[float, Any]] = {}
_CACHE_TTL = 3600.0


async def get_api_key() -> str:
    from app.services import app_settings

    env_key = (getattr(settings, "hardcover_api_key", "") or "").strip()
    raw = (await app_settings.get_setting(API_KEY_SETTING, default=env_key)).strip()
    if not raw:
        return ""
    # Accept "Bearer xxx" or bare token from the Hardcover settings page.
    if raw.lower().startswith("bearer "):
        return raw
    return f"Bearer {raw}"


def _cache_get(key: str) -> Any | None:
    hit = _cache.get(key)
    if not hit:
        return None
    ts, val = hit
    if time.time() - ts > _CACHE_TTL:
        _cache.pop(key, None)
        return None
    return val


def _cache_set(key: str, val: Any) -> None:
    _cache[key] = (time.time(), val)
    if len(_cache) > 400:
        oldest = min(_cache, key=lambda k: _cache[k][0])
        _cache.pop(oldest, None)


async def _graphql(query: str, variables: dict | None = None) -> dict:
    token = await get_api_key()
    if not token:
        return {}
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                API_URL,
                headers={
                    "Authorization": token,
                    "Content-Type": "application/json",
                    "User-Agent": USER_AGENT,
                },
                json={"query": query, "variables": variables or {}},
            )
            if resp.status_code == 401:
                logger.warning("Hardcover auth failed — check API token")
                return {}
            if resp.status_code == 429:
                logger.warning("Hardcover rate limited")
                return {}
            if resp.status_code >= 400:
                logger.debug("Hardcover HTTP %s: %s", resp.status_code, resp.text[:200])
                return {}
            payload = resp.json()
            if payload.get("errors"):
                logger.debug("Hardcover GraphQL errors: %s", payload["errors"][:2])
            return payload.get("data") or {}
    except Exception as e:
        logger.debug("Hardcover request failed: %s", e)
        return {}


def _norm_title(s: str) -> str:
    s = (s or "").lower().replace("\u2019", "'").replace("\u2018", "'")
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _extract_search_docs(results: Any) -> list[dict]:
    """Normalize Hardcover Typesense ``search.results`` into document dicts.

    Live API returns ``{hits: [{document: {...}}], found: N, ...}``, not a bare list.
    """
    if not results:
        return []
    if isinstance(results, dict):
        hits = results.get("hits") or results.get("items") or []
        return _extract_search_docs(hits)
    if not isinstance(results, list):
        return []
    docs: list[dict] = []
    for raw in results:
        if not isinstance(raw, dict):
            continue
        doc = raw.get("document") if "document" in raw else raw
        if isinstance(doc, dict):
            docs.append(doc)
    return docs


def _series_fields_from_raw(raw: dict) -> tuple[str, str]:
    """Pull (series_name, position) from a Hardcover book document."""
    series_name = ""
    series_pos = ""
    featured = raw.get("featured_series") or {}
    if isinstance(featured, dict) and featured:
        nested = featured.get("series") if isinstance(featured.get("series"), dict) else {}
        series_name = (
            (nested.get("name") if nested else "")
            or featured.get("name")
            or featured.get("series_name")
            or ""
        ).strip()
        pos = featured.get("position")
        if pos is not None:
            series_pos = str(pos)
    if not series_name:
        names = raw.get("series_names") or []
        if isinstance(names, list) and names:
            series_name = str(names[0]).strip()
    if not series_name and raw.get("seriesName"):
        series_name = str(raw.get("seriesName") or "").strip()
        series_pos = str(raw.get("seriesBookNumber") or series_pos)
    return series_name, series_pos


def _hc_book_to_summary(raw: dict) -> dict | None:
    """Map a Hardcover search/book hit into store BookSummary-ish fields."""
    if not isinstance(raw, dict):
        return None
    title = (raw.get("title") or "").strip()
    if not title:
        return None
    authors = []
    for a in raw.get("author_names") or []:
        if isinstance(a, str) and a.strip():
            authors.append(a.strip())
    for c in raw.get("contributions") or []:
        if isinstance(c, dict):
            name = ((c.get("author") or {}).get("name") or "").strip()
            if name and name not in authors:
                authors.append(name)

    isbns = raw.get("isbns") or []
    isbn13 = ""
    isbn10 = ""
    for isbn in isbns:
        digits = "".join(ch for ch in str(isbn) if ch.isdigit() or ch.upper() == "X")
        if len(digits) == 13 and not isbn13:
            isbn13 = digits
        elif len(digits) == 10 and not isbn10:
            isbn10 = digits

    image = raw.get("image") or {}
    cover = ""
    if isinstance(image, dict):
        cover = (image.get("url") or "").strip()
    elif isinstance(image, str):
        cover = image.strip()

    slug = (raw.get("slug") or "").strip()
    hc_id = raw.get("id")
    volume_id = f"HC:{hc_id}" if hc_id is not None else (f"HC:{slug}" if slug else f"HC:title:{_norm_title(title)[:60]}")

    series_name, series_pos = _series_fields_from_raw(raw)
    # Normalize 1.0 → 1
    try:
        if series_pos and float(series_pos) == int(float(series_pos)):
            series_pos = str(int(float(series_pos)))
    except (TypeError, ValueError):
        pass

    rating = float(raw.get("rating") or 0) or 0.0
    ratings_count = int(raw.get("ratings_count") or 0)

    return {
        "id": volume_id,
        "volumeId": volume_id,
        "title": title,
        "subtitle": (raw.get("subtitle") or "").strip(),
        "authors": authors,
        "publisher": "",
        "publishedDate": str(raw.get("release_year") or raw.get("release_date") or ""),
        "description": (raw.get("description") or "").strip(),
        "pageCount": int(raw.get("pages") or 0) or 0,
        "categories": list(raw.get("genres") or raw.get("tags") or [])[:5],
        "mainCategory": "",
        "averageRating": rating,
        "ratingsCount": ratings_count,
        "language": "en",
        "coverUrl": cover,
        "coverUrlLarge": cover,
        "isbn10": isbn10,
        "isbn13": isbn13,
        "previewLink": f"https://hardcover.app/books/{slug}" if slug else "",
        "infoLink": f"https://hardcover.app/books/{slug}" if slug else "",
        "seriesName": series_name,
        "seriesBookNumber": series_pos,
        "source": "hardcover",
        "hardcoverId": hc_id,
        "hardcoverSlug": slug,
        "hardcoverReviewsCount": int(raw.get("reviews_count") or 0),
    }


async def search_books(query: str, *, limit: int = 10, page: int = 1) -> list[dict]:
    q = (query or "").strip()
    if len(q) < 2 or not await get_api_key():
        return []
    cache_key = f"hc_search:{q}:{limit}:{page}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    data = await _graphql(
        """
        query SearchBooks($q: String!, $perPage: Int!, $page: Int!) {
          search(query: $q, query_type: "Book", per_page: $perPage, page: $page) {
            results
          }
        }
        """,
        {"q": q[:200], "perPage": min(max(1, limit), 25), "page": max(1, page)},
    )
    docs = _extract_search_docs((data.get("search") or {}).get("results"))
    books: list[dict] = []
    for doc in docs:
        norm = _hc_book_to_summary(doc)
        if norm:
            books.append(norm)
    _cache_set(cache_key, books)
    return books


async def get_rating(
    *,
    isbn13: str = "",
    isbn10: str = "",
    title: str = "",
    author: str = "",
) -> dict:
    """Return rating fields compatible with the existing detail UI keys."""
    empty = {
        "goodreadsRating": 0.0,
        "goodreadsCount": 0,
        "goodreadsReviewCount": 0,
        "source": "hardcover",
    }
    if not await get_api_key():
        return empty

    query = ""
    if isbn13:
        query = isbn13
    elif isbn10:
        query = isbn10
    elif title:
        query = f"{title} {author}".strip()
    if not query:
        return empty

    cache_key = f"hc_rating:{_norm_title(query)}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    hits = await search_books(query, limit=5)
    pick = None
    title_n = _norm_title(title)
    for h in hits:
        if isbn13 and h.get("isbn13") == isbn13:
            pick = h
            break
        if isbn10 and h.get("isbn10") == isbn10:
            pick = h
            break
    if not pick and title_n:
        for h in hits:
            if _norm_title(h.get("title") or "") == title_n:
                pick = h
                break
        if not pick:
            for h in hits:
                ht = _norm_title(h.get("title") or "")
                if title_n in ht or ht in title_n:
                    pick = h
                    break
    if not pick and hits:
        pick = hits[0]

    if not pick or not pick.get("averageRating"):
        _cache_set(cache_key, empty)
        return empty

    out = {
        "goodreadsRating": float(pick["averageRating"]),
        "goodreadsCount": int(pick.get("ratingsCount") or 0),
        "goodreadsReviewCount": int(pick.get("hardcoverReviewsCount") or 0),
        "source": "hardcover",
    }
    _cache_set(cache_key, out)
    return out


_SERIES_JUNK_TITLE = (
    "collection set",
    "box set",
    "boxed set",
    "books collection",
    "omnibus",
    "complete series",
    "complete collection",
    "anthology",
    "bundle",
    "books 1-",
    "book 1-",
    "vol 1-",
    "volumes 1-",
)


def _titles_compatible(a: str, b: str) -> bool:
    """True when titles are equal or one is a clear expansion of the other."""
    na, nb = _norm_title(a), _norm_title(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    # Avoid tiny-substring traps ("it" in "little").
    if len(na) < 5 or len(nb) < 5:
        return False
    shorter, longer = (na, nb) if len(na) <= len(nb) else (nb, na)
    if not longer.startswith(shorter) and shorter not in longer:
        return False
    # Require the shorter title to be a substantial fraction of the longer.
    return len(shorter) >= max(5, int(len(longer) * 0.55))


def _authors_overlap(a: str, authors: list[str]) -> bool:
    an = _norm_title(a)
    if not an:
        return True
    for name in authors:
        bn = _norm_title(name)
        if not bn:
            continue
        if an == bn or an in bn or bn in an:
            return True
        # Last-name match for "Mercedes Lackey" vs "Lackey, Mercedes"
        a_parts = an.split()
        b_parts = bn.split()
        if a_parts and b_parts and a_parts[-1] == b_parts[-1] and len(a_parts[-1]) >= 4:
            return True
    return False


def _series_name_compatible(series_name: str, candidate: str, book_title: str = "") -> bool:
    sn, cn = _norm_title(series_name), _norm_title(candidate)
    if not sn or not cn:
        return False
    if sn == cn:
        return True
    if sn in cn or cn in sn:
        # Reject when the only overlap is a generic word shared with the book title.
        return True
    # Never accept "book title ≈ series name" unless nearly equal.
    if book_title and _titles_compatible(book_title, candidate) and sn != cn:
        return False
    return False


async def get_series_for_book(
    *,
    title: str,
    author: str = "",
    series_hint: str = "",
) -> dict:
    """Resolve a series and ordered books via Hardcover.

    Returns {seriesName, books:[{id,title,subtitle,coverUrl,authors,sequence,publishedDate,isbn13}], currentBookIndex}.
    Book ids are HC:* until the caller remaps them onto local OL volumes.
    """
    empty = {"seriesName": None, "books": [], "currentBookIndex": -1}
    if not await get_api_key():
        return empty

    cache_key = f"hc_series:v2:{_norm_title(series_hint or title)}:{_norm_title(author)}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    series_name = (series_hint or "").strip()
    seed_authors = [author] if author else []
    if not series_name:
        # Discover series from the best matching book hit — require a real title match.
        hits = await search_books(f"{title} {author}".strip(), limit=8)
        title_n = _norm_title(title)
        best = None
        for h in hits:
            ht = _norm_title(h.get("title") or "")
            if not ht or not _titles_compatible(title, h.get("title") or ""):
                continue
            h_authors = h.get("authors") or []
            if author and h_authors and not _authors_overlap(author, h_authors):
                continue
            best = h
            if h.get("seriesName"):
                break
        if best and best.get("seriesName"):
            series_name = best["seriesName"]
            if best.get("authors"):
                seed_authors = list(best["authors"])

    if not series_name:
        # Do not fall back to searching Series with the raw book title — that
        # attaches unrelated series far too often.
        _cache_set(cache_key, empty)
        return empty

    # Search series index, then load books with distinct positions.
    series_queries = []
    if series_hint:
        series_queries.append(series_hint[:120])
    if series_name and series_name not in series_queries:
        series_queries.append(series_name[:120])
    if author and series_name:
        series_queries.append(f"{series_name} {author}".strip()[:120])

    series_id = None
    canonical_name = series_name
    author_n = _norm_title(author)
    for sq in series_queries:
        if not sq:
            continue
        data = await _graphql(
            """
            query FindSeries($q: String!) {
              search(query: $q, query_type: "Series", per_page: 8, page: 1) {
                results
              }
            }
            """,
            {"q": sq},
        )
        docs = _extract_search_docs((data.get("search") or {}).get("results"))
        ranked: list[tuple[float, dict]] = []
        for doc in docs:
            name = (doc.get("name") or "").strip()
            if not name or not _series_name_compatible(series_name, name, title):
                continue
            if int(doc.get("books_count") or 0) == 0:
                continue
            score = 10.0
            nn = _norm_title(name)
            sn = _norm_title(series_name)
            if nn == sn:
                score += 50
            elif sn in nn or nn in sn:
                score += 25
            doc_author = ""
            a = doc.get("author")
            if isinstance(a, dict):
                doc_author = _norm_title(a.get("name") or "")
            if author_n and doc_author:
                if author_n in doc_author or doc_author in author_n:
                    score += 40
                else:
                    # Wrong-author series with a similar name — skip.
                    continue
            elif author_n and not doc_author:
                score -= 5
            score += min(int(doc.get("books_count") or 0), 40) * 0.25
            ranked.append((score, doc))
        ranked.sort(key=lambda x: x[0], reverse=True)
        if ranked and ranked[0][0] >= 20:
            best_doc = ranked[0][1]
            series_id = best_doc.get("id")
            canonical_name = (best_doc.get("name") or series_name).strip()
            break

    if series_id is None:
        _cache_set(cache_key, empty)
        return empty

    try:
        series_id_int = int(series_id)
    except (TypeError, ValueError):
        _cache_set(cache_key, empty)
        return empty

    detail = await _graphql(
        """
        query SeriesBooks($id: Int!) {
          series(where: {id: {_eq: $id}}) {
            id
            name
            book_series(
              distinct_on: position
              order_by: [{position: asc}, {book: {users_count: desc}}]
              where: {
                compilation: {_eq: false}
                book: {canonical_id: {_is_null: true}, is_partial_book: {_eq: false}}
              }
            ) {
              position
              book {
                id
                title
                subtitle
                slug
                rating
                ratings_count
                pages
                release_year
                image { url }
                contributions { author { name } }
                editions(limit: 3, order_by: {users_count: desc}) {
                  isbn_13
                  isbn_10
                  image { url }
                }
              }
            }
          }
        }
        """,
        {"id": series_id_int},
    )
    rows = detail.get("series") or []
    if not rows:
        _cache_set(cache_key, empty)
        return empty
    series = rows[0]
    books_out: list[dict] = []
    title_n = _norm_title(title)
    current_idx = -1
    for entry in series.get("book_series") or []:
        book = entry.get("book") or {}
        if not book.get("title"):
            continue
        bt = (book.get("title") or "").lower()
        if any(junk in bt for junk in _SERIES_JUNK_TITLE):
            continue
        authors = []
        for c in book.get("contributions") or []:
            name = ((c.get("author") or {}).get("name") or "").strip()
            if name:
                authors.append(name)
        # Drop spin-offs / shared-world books by other authors when we know the seed author.
        if seed_authors and authors:
            if not any(_authors_overlap(sa, authors) for sa in seed_authors):
                continue
        cover = ""
        img = book.get("image") or {}
        if isinstance(img, dict):
            cover = (img.get("url") or "").strip()
        isbn13 = ""
        isbn10 = ""
        for ed in book.get("editions") or []:
            if not isbn13 and ed.get("isbn_13"):
                isbn13 = str(ed["isbn_13"])
            if not isbn10 and ed.get("isbn_10"):
                isbn10 = str(ed["isbn_10"])
            if not cover:
                eimg = ed.get("image") or {}
                if isinstance(eimg, dict):
                    cover = (eimg.get("url") or "").strip()
        pos = entry.get("position")
        seq = "" if pos is None else str(pos)
        try:
            if seq and float(seq) == int(float(seq)):
                seq = str(int(float(seq)))
        except (TypeError, ValueError):
            pass
        # Skip unnumbered extras (side stories often have null position).
        if not seq:
            continue
        try:
            if float(seq) <= 0:
                continue
        except (TypeError, ValueError):
            pass
        bid = f"HC:{book['id']}"
        item = {
            "id": bid,
            "title": book["title"],
            "subtitle": (book.get("subtitle") or "").strip(),
            "coverUrl": cover,
            "authors": authors,
            "sequence": seq,
            "publishedDate": str(book.get("release_year") or ""),
            "isbn13": isbn13,
            "isbn10": isbn10,
            "hardcoverSlug": book.get("slug") or "",
        }
        if current_idx < 0 and title_n and _titles_compatible(title, item["title"]):
            current_idx = len(books_out)
        books_out.append(item)

    if len(books_out) < 2:
        _cache_set(cache_key, empty)
        return empty

    out = {
        "seriesName": (series.get("name") or canonical_name).strip(),
        "books": books_out,
        "currentBookIndex": current_idx,
    }
    _cache_set(cache_key, out)
    return out


async def get_book_by_id(hc_id: int) -> dict | None:
    """Fetch a single Hardcover book by numeric id."""
    if not await get_api_key():
        return None
    cache_key = f"hc_book:{hc_id}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    data = await _graphql(
        """
        query BookById($id: Int!) {
          books(where: {id: {_eq: $id}}) {
            id
            title
            subtitle
            slug
            rating
            ratings_count
            reviews_count
            pages
            release_year
            description
            image { url }
            contributions { author { name } }
            editions(limit: 5, order_by: {users_count: desc}) {
              isbn_13
              isbn_10
              image { url }
            }
            book_series(limit: 1, order_by: {position: asc}) {
              position
              series { name }
            }
          }
        }
        """,
        {"id": int(hc_id)},
    )
    rows = data.get("books") or []
    if not rows:
        _cache_set(cache_key, None)
        return None
    book = rows[0]
    isbns = []
    for ed in book.get("editions") or []:
        if ed.get("isbn_13"):
            isbns.append(ed["isbn_13"])
        if ed.get("isbn_10"):
            isbns.append(ed["isbn_10"])
    series_name = ""
    series_pos = ""
    for bs in book.get("book_series") or []:
        series_name = ((bs.get("series") or {}).get("name") or "").strip()
        if bs.get("position") is not None:
            series_pos = str(bs["position"])
        break
    shaped = {
        "id": book.get("id"),
        "title": book.get("title"),
        "subtitle": book.get("subtitle"),
        "slug": book.get("slug"),
        "rating": book.get("rating"),
        "ratings_count": book.get("ratings_count"),
        "reviews_count": book.get("reviews_count"),
        "pages": book.get("pages"),
        "release_year": book.get("release_year"),
        "description": book.get("description"),
        "image": book.get("image"),
        "contributions": book.get("contributions"),
        "isbns": isbns,
        "seriesName": series_name,
        "seriesBookNumber": series_pos,
    }
    norm = _hc_book_to_summary(shaped)
    if norm and series_name:
        norm["seriesName"] = series_name
        norm["seriesBookNumber"] = series_pos
    _cache_set(cache_key, norm)
    return norm


async def resolve_store_volume(hc_book: dict, *, quick: bool = False) -> dict:
    """Map a Hardcover book summary onto a local OL/ISBN volume when possible.

    Prefers ISBN → local catalog, then title search (unless ``quick``). Falls back
    to the HC:* id so detail pages still work via Hardcover metadata.
    """
    if not hc_book:
        return hc_book
    from app.services import google_books, ol_catalog

    isbn13 = (hc_book.get("isbn13") or "").strip()
    isbn10 = (hc_book.get("isbn10") or "").strip()
    title = (hc_book.get("title") or "").strip()
    authors = hc_book.get("authors") or []
    author = authors[0] if authors else ""

    local: dict | None = None
    for isbn in (isbn13, isbn10):
        if not isbn:
            continue
        try:
            if ol_catalog.catalog_ready():
                local = await ol_catalog.lookup_isbn(isbn)
            if not local:
                local = await google_books.get_catalog_volume(f"ISBN:{isbn}")
        except Exception:
            local = None
        if local:
            break

    if not local and title and not quick:
        try:
            if ol_catalog.catalog_ready():
                hits = await ol_catalog.search_by_title(title, limit=5)
                title_n = _norm_title(title)
                for h in hits or []:
                    if _norm_title(h.get("title") or "") == title_n:
                        local = h
                        break
                if not local and hits:
                    # Accept close title when author also overlaps.
                    for h in hits:
                        ht = _norm_title(h.get("title") or "")
                        if title_n in ht or ht in title_n:
                            local = h
                            break
        except Exception:
            pass

    if not local:
        return hc_book

    # Keep Hardcover series/rating hints when local metadata is thinner.
    out = dict(local)
    if hc_book.get("sequence"):
        out["sequence"] = hc_book["sequence"]
    if not out.get("seriesName") and hc_book.get("seriesName"):
        out["seriesName"] = hc_book["seriesName"]
        out["seriesBookNumber"] = hc_book.get("seriesBookNumber") or hc_book.get("sequence") or ""
    elif hc_book.get("sequence") and not out.get("seriesBookNumber"):
        out["seriesBookNumber"] = hc_book["sequence"]
    # Prefer Hardcover art when local OL has none, or only a tiny OL stub image.
    hc_cover = (hc_book.get("coverUrl") or "").strip()
    local_cover = (out.get("coverUrl") or "").strip()
    if hc_cover and (
        not local_cover
        or (
            "covers.openlibrary.org" in local_cover
            and not await google_books._cover_url_looks_real(local_cover)
        )
    ):
        out["coverUrl"] = hc_cover
        out["coverUrlLarge"] = hc_book.get("coverUrlLarge") or hc_cover
    if not out.get("averageRating") and hc_book.get("averageRating"):
        out["averageRating"] = hc_book["averageRating"]
        out["ratingsCount"] = hc_book.get("ratingsCount") or 0
    out["source"] = out.get("source") or "openlibrary"
    out["hardcoverId"] = hc_book.get("hardcoverId")
    return out


async def resolve_store_volumes(books: list[dict], *, quick: bool = False) -> list[dict]:
    if not books:
        return []
    sem = asyncio.Semaphore(4)

    async def _one(b: dict) -> dict:
        async with sem:
            return await resolve_store_volume(b, quick=quick)

    return list(await asyncio.gather(*(_one(b) for b in books)))


def _score_list_doc(
    doc: dict,
    *,
    require_terms: list[str],
    extras: list[str] | None = None,
) -> float:
    """Rank community lists — prefer liked/followed themed lists over empty stubs."""
    name = _norm_title(doc.get("name") or "")
    if not name:
        return -1e9
    likes = int(doc.get("likes_count") or 0)
    followers = int(doc.get("followers_count") or 0)
    books_count = int(doc.get("books_count") or 0)
    if books_count < 8:
        return -1e9

    terms = [t for t in require_terms if t]
    extra_terms = extras or []
    has_theme = any(t in name for t in terms) or any(t in name for t in extra_terms)
    # Hard reject off-topic popular lists (Typesense often returns Esquire Sci-Fi
    # for "best horror books" because of text match on "best … books").
    if not has_theme:
        return -1e9

    score = likes * 4.0 + followers * 3.0 + min(books_count, 100) * 0.5
    score += 80  # theme term present

    if "best" in name or "must read" in name or "award" in name or "everyone should" in name:
        score += 40
    if "prize" in name or "winner" in name:
        score += 20
    if any(bad in name for bad in _LIST_NAME_PENALTY):
        score -= 200
    # Prefer all-time / evergreen over single-year roundups when likes are similar.
    if re.search(r"\b20\d{2}\b", name) and likes < 20:
        score -= 25
    # Prefer lists that lead with the theme ("Best Horror…") over compound awards.
    if terms and (
        name.startswith(tuple(terms)) or any(name.startswith(f"best {t}") for t in terms)
    ):
        score += 25
    return score


def _shelf_search_config(slug: str) -> tuple[list[str], list[str], list[str]]:
    """Return (queries, require_terms, extras) for a curated / genre shelf slug."""
    home = _home_shelf_by_slug(slug)
    if home:
        queries = list(home.get("queries") or [slug.replace("-", " ")])
        terms = list(home.get("require_terms") or [])
        genre = home.get("genre") or ""
        extras = list(_GENRE_ALLOW_EXTRA.get(genre) or [])
        return queries, terms, extras
    queries = GENRE_LIST_QUERIES.get(slug) or [slug.replace("-", " ")]
    terms = list(_GENRE_NAME_TERMS.get(slug) or [slug.replace("-", " ")])
    extras = list(_GENRE_ALLOW_EXTRA.get(slug) or [])
    return queries, terms, extras


async def _pick_best_list(slug: str) -> tuple[int | None, str]:
    """Return (list_id, list_name) for the best matching public list."""
    queries, require_terms, extras = _shelf_search_config(slug)
    best_doc: dict | None = None
    best_score = -1e9
    for q in queries:
        data = await _graphql(
            """
            query SearchLists($q: String!) {
              search(query: $q, query_type: "List", per_page: 10, page: 1) {
                results
              }
            }
            """,
            {"q": q},
        )
        for doc in _extract_search_docs((data.get("search") or {}).get("results")):
            score = _score_list_doc(doc, require_terms=require_terms, extras=extras)
            if score > best_score and doc.get("id") is not None:
                best_score = score
                best_doc = doc
        # Early exit when we already found a strongly liked themed list.
        if best_doc and best_score >= 200:
            break

    if not best_doc or best_score < 0:
        return None, ""
    try:
        return int(best_doc["id"]), (best_doc.get("name") or "").strip()
    except (TypeError, ValueError):
        return None, ""


async def get_curated_list_books(genre_slug: str, *, limit: int = 20) -> list[dict]:
    """Popular themed Hardcover lists for a genre → book summaries.

    Also attaches ``_listName`` on the first book dict via a side channel is awkward;
    callers should use :func:`get_curated_shelf` for title + books together.
    """
    shelf = await get_curated_shelf(genre_slug, limit=limit)
    return shelf.get("books") or []


async def get_curated_shelf(slug: str, *, limit: int = 20) -> dict:
    """Return ``{listName, listId, books, source}`` for a curated recommendation shelf."""
    empty = {"listName": "", "listId": None, "books": [], "source": "none"}
    if not await get_api_key():
        return empty

    cache_key = f"hc_shelf:v2:{slug}:{limit}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    list_id, list_name = await _pick_best_list(slug)
    if list_id is None:
        # Last resort: book search for the first query — still themed, not random cache.
        queries, _, _ = _shelf_search_config(slug)
        q = queries[0] if queries else slug
        books = await search_books(q, limit=limit)
        # Drop hits that are clearly list/meta titles.
        books = [
            b for b in books
            if "best " not in (b.get("title") or "").lower()[:12]
            and "novels" not in (b.get("title") or "").lower()[-8:]
        ]
        out = {
            "listName": list_name or q.title(),
            "listId": None,
            "books": books[:limit],
            "source": "hardcover_search" if books else "none",
        }
        _cache_set(cache_key, out)
        return out

    detail = await _graphql(
        """
        query ListBooks($id: Int!) {
          lists(where: {id: {_eq: $id}}) {
            id
            name
            list_books(order_by: {position: asc}, limit: 40) {
              position
              book {
                id
                title
                subtitle
                slug
                rating
                ratings_count
                reviews_count
                pages
                release_year
                image { url }
                contributions { author { name } }
                editions(limit: 2, order_by: {users_count: desc}) {
                  isbn_13
                  isbn_10
                }
              }
            }
          }
        }
        """,
        {"id": int(list_id)},
    )
    lists = detail.get("lists") or []
    if not lists:
        out = {**empty, "listName": list_name, "listId": list_id}
        _cache_set(cache_key, out)
        return out

    list_name = (lists[0].get("name") or list_name).strip()
    books: list[dict] = []
    for entry in lists[0].get("list_books") or []:
        book = entry.get("book") or {}
        if not book:
            continue
        shaped = {
            "id": book.get("id"),
            "title": book.get("title"),
            "subtitle": book.get("subtitle"),
            "slug": book.get("slug"),
            "rating": book.get("rating"),
            "ratings_count": book.get("ratings_count"),
            "reviews_count": book.get("reviews_count"),
            "pages": book.get("pages"),
            "release_year": book.get("release_year"),
            "image": book.get("image"),
            "contributions": book.get("contributions"),
            "isbns": [
                e.get("isbn_13") or e.get("isbn_10")
                for e in (book.get("editions") or [])
                if e.get("isbn_13") or e.get("isbn_10")
            ],
        }
        norm = _hc_book_to_summary(shaped)
        if norm:
            books.append(norm)
        if len(books) >= limit:
            break

    logger.info("Hardcover list %r (%s) → %s books", list_name, list_id, len(books))
    out = {
        "listName": list_name,
        "listId": list_id,
        "books": books,
        "source": "hardcover" if books else "none",
    }
    _cache_set(cache_key, out)
    return out
