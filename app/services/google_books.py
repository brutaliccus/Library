import asyncio
import logging
import random
import time
from typing import Any

import httpx
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

GOOGLE_BOOKS_URL = "https://www.googleapis.com/books/v1/volumes"
OPEN_LIBRARY_SUBJECTS_URL = "https://openlibrary.org/subjects"

_cache: dict[str, tuple[float, Any]] = {}
CACHE_TTL = 1800  # 30 minutes

# With an API key we can handle more concurrency; still cap to avoid bursts.
_gbooks_semaphore = asyncio.Semaphore(5)
_last_gbooks_ts: float = 0.0
_MIN_GBOOKS_GAP = 0.1  # seconds between requests

SPECIAL_CATEGORIES = ["all", "popular", "new"]

# ---------------------------------------------------------------------------
# Hierarchical genre taxonomy (B&N-inspired names where helpful)
# - query: primary Google Books search term
# - ol_subject: Open Library fallback
# - multi_queries: extra queries when user filters by genre (broader coverage)
# ---------------------------------------------------------------------------

GENRE_TAXONOMY: list[dict] = [
    {
        "slug": "fantasy",
        "name": "Fantasy",
        "icon": "wand-sparkles",
        "query": "subject:fantasy",
        "ol_subject": "fantasy",
        "multi_queries": ["fantasy fiction", "fantasy novels"],
        "children": [
            {"slug": "epic-fantasy", "name": "Epic Fantasy", "query": "subject:epic+fantasy", "ol_subject": "epic_fantasy", "multi_queries": ["epic fantasy fiction"]},
            {"slug": "urban-fantasy", "name": "Urban Fantasy", "query": "subject:urban+fantasy", "ol_subject": "urban_fantasy", "multi_queries": ["urban fantasy fiction"]},
            {"slug": "dark-fantasy", "name": "Dark Fantasy", "query": "subject:dark+fantasy", "ol_subject": "dark_fantasy", "multi_queries": ["dark fantasy fiction"]},
            {"slug": "sword-and-sorcery", "name": "Sword & Sorcery", "query": "subject:sword+sorcery", "ol_subject": "sword_and_sorcery", "multi_queries": ["sword and sorcery fantasy"]},
            {"slug": "fairy-tales", "name": "Fairy Tales & Mythology", "query": "subject:fairy+tales", "ol_subject": "fairy_tales", "multi_queries": ["fairy tales mythology"]},
            {"slug": "litrpg", "name": "LitRPG / GameLit", "query": "litrpg+gamelit+fantasy", "ol_subject": "litrpg", "multi_queries": ["litrpg gamelit"]},
            {"slug": "paranormal-fantasy", "name": "Paranormal", "query": "subject:paranormal+fiction", "ol_subject": "paranormal_fiction", "multi_queries": ["paranormal fiction"]},
            {"slug": "high-fantasy", "name": "High Fantasy", "query": "subject:high+fantasy", "ol_subject": "high_fantasy", "multi_queries": ["high fantasy fiction"]},
        ],
    },
    {
        "slug": "science-fiction",
        "name": "Science Fiction",
        "icon": "rocket",
        "query": "subject:science+fiction",
        "ol_subject": "science_fiction",
        "multi_queries": ["science fiction novels", "sci-fi fiction"],
        "children": [
            {"slug": "space-opera", "name": "Space Opera", "query": "subject:space+opera", "ol_subject": "space_opera", "multi_queries": ["space opera science fiction"]},
            {"slug": "cyberpunk", "name": "Cyberpunk", "query": "subject:cyberpunk", "ol_subject": "cyberpunk", "multi_queries": ["cyberpunk fiction"]},
            {"slug": "dystopian", "name": "Dystopian", "query": "subject:dystopian+fiction", "ol_subject": "dystopian_fiction", "multi_queries": ["dystopian fiction"]},
            {"slug": "hard-scifi", "name": "Hard Sci-Fi", "query": "subject:hard+science+fiction", "ol_subject": "hard_science_fiction", "multi_queries": ["hard science fiction"]},
            {"slug": "time-travel", "name": "Time Travel", "query": "subject:time+travel", "ol_subject": "time_travel", "multi_queries": ["time travel fiction"]},
            {"slug": "post-apocalyptic", "name": "Post-Apocalyptic", "query": "subject:post-apocalyptic", "ol_subject": "post-apocalyptic_fiction", "multi_queries": ["post apocalyptic fiction"]},
            {"slug": "military-scifi", "name": "Military Sci-Fi", "query": "subject:military+science+fiction", "ol_subject": "military_science_fiction", "multi_queries": ["military science fiction"]},
            {"slug": "alien-contact", "name": "First Contact / Aliens", "query": "subject:aliens+science+fiction", "ol_subject": "extraterrestrial_beings", "multi_queries": ["first contact aliens science fiction"]},
        ],
    },
    {
        "slug": "mystery",
        "name": "Mystery & Thrillers",
        "icon": "search",
        "query": "subject:mystery",
        "ol_subject": "mystery_and_detective_stories",
        "multi_queries": ["mystery fiction", "mystery novels", "detective fiction"],
        "children": [
            {"slug": "cozy-mystery", "name": "Cozy Mystery", "query": "subject:cozy+mystery", "ol_subject": "cozy_mystery", "multi_queries": ["cozy mystery fiction"]},
            {"slug": "detective", "name": "Detective", "query": "subject:detective+fiction", "ol_subject": "detective_and_mystery_stories", "multi_queries": ["detective fiction novels"]},
            {"slug": "police-procedural", "name": "Police Procedural", "query": "subject:police+procedural", "ol_subject": "police_procedural", "multi_queries": ["police procedural fiction"]},
            {"slug": "noir", "name": "Noir", "query": "subject:noir+fiction", "ol_subject": "noir_fiction", "multi_queries": ["noir fiction"]},
            {"slug": "whodunit", "name": "Whodunit", "query": "subject:whodunit", "ol_subject": "whodunits", "multi_queries": ["whodunit mystery"]},
            {"slug": "amateur-sleuth", "name": "Amateur Sleuth", "query": "subject:amateur+sleuth", "ol_subject": "amateur_detective", "multi_queries": ["amateur sleuth mystery"]},
        ],
    },
    {
        "slug": "thriller",
        "name": "Thriller",
        "icon": "zap",
        "query": "subject:thriller",
        "ol_subject": "thrillers",
        "multi_queries": ["thriller fiction", "thriller novels", "suspense fiction"],
        "children": [
            {"slug": "psychological-thriller", "name": "Psychological", "query": "subject:psychological+thriller", "ol_subject": "psychological_fiction", "multi_queries": ["psychological thriller fiction"]},
            {"slug": "legal-thriller", "name": "Legal", "query": "subject:legal+thriller", "ol_subject": "legal_stories", "multi_queries": ["legal thriller fiction"]},
            {"slug": "espionage", "name": "Espionage / Spy", "query": "subject:espionage+fiction", "ol_subject": "spy_stories", "multi_queries": ["spy thriller espionage fiction"]},
            {"slug": "medical-thriller", "name": "Medical", "query": "subject:medical+thriller", "ol_subject": "medical_fiction", "multi_queries": ["medical thriller fiction"]},
            {"slug": "political-thriller", "name": "Political", "query": "subject:political+thriller", "ol_subject": "political_fiction", "multi_queries": ["political thriller fiction"]},
            {"slug": "action-thriller", "name": "Action", "query": "subject:action+thriller", "ol_subject": "adventure_stories", "multi_queries": ["action thriller fiction"]},
            {"slug": "techno-thriller", "name": "Techno-Thriller", "query": "subject:technothriller", "ol_subject": "techno-thrillers", "multi_queries": ["techno thriller fiction"]},
        ],
    },
    {
        "slug": "romance",
        "name": "Romance",
        "icon": "heart",
        "query": "subject:romance",
        "ol_subject": "romance",
        "multi_queries": ["romance fiction", "romance novels", "love story"],
        "children": [
            {"slug": "contemporary-romance", "name": "Contemporary", "query": "subject:contemporary+romance", "ol_subject": "contemporary_romance", "multi_queries": ["contemporary romance fiction"]},
            {"slug": "historical-romance", "name": "Historical", "query": "subject:historical+romance", "ol_subject": "historical_romance", "multi_queries": ["historical romance fiction"]},
            {"slug": "paranormal-romance", "name": "Paranormal", "query": "subject:paranormal+romance", "ol_subject": "paranormal_romance", "multi_queries": ["paranormal romance fiction"]},
            {"slug": "romantic-suspense", "name": "Romantic Suspense", "query": "subject:romantic+suspense", "ol_subject": "romantic_suspense_fiction", "multi_queries": ["romantic suspense fiction"]},
            {"slug": "dark-romance", "name": "Dark Romance", "query": "dark+romance+fiction", "ol_subject": "dark_romance", "multi_queries": ["dark romance fiction"]},
            {"slug": "romantic-comedy", "name": "Rom-Com", "query": "subject:romantic+comedy", "ol_subject": "romantic_comedy", "multi_queries": ["romantic comedy fiction"]},
            {"slug": "fantasy-romance", "name": "Fantasy Romance", "query": "subject:fantasy+romance", "ol_subject": "fantasy_romance", "multi_queries": ["fantasy romance fiction"]},
        ],
    },
    {
        "slug": "horror",
        "name": "Horror",
        "icon": "skull",
        "query": "subject:horror",
        "ol_subject": "horror",
        "multi_queries": ["horror fiction", "horror novels", "scary books"],
        "children": [
            {"slug": "supernatural-horror", "name": "Supernatural", "query": "subject:supernatural+horror", "ol_subject": "supernatural", "multi_queries": ["supernatural horror fiction"]},
            {"slug": "gothic", "name": "Gothic", "query": "subject:gothic+fiction", "ol_subject": "gothic_fiction", "multi_queries": ["gothic horror fiction"]},
            {"slug": "cosmic-horror", "name": "Cosmic / Lovecraftian", "query": "subject:cosmic+horror+lovecraft", "ol_subject": "cosmic_horror", "multi_queries": ["cosmic horror lovecraftian"]},
            {"slug": "psychological-horror", "name": "Psychological", "query": "subject:psychological+horror", "ol_subject": "psychological_horror", "multi_queries": ["psychological horror fiction"]},
            {"slug": "zombie", "name": "Zombie / Undead", "query": "subject:zombie+fiction", "ol_subject": "zombies", "multi_queries": ["zombie fiction"]},
            {"slug": "vampire", "name": "Vampire", "query": "subject:vampire+fiction", "ol_subject": "vampires", "multi_queries": ["vampire fiction"]},
        ],
    },
    {
        "slug": "young-adult",
        "name": "Teens & YA",
        "icon": "sparkles",
        "query": "subject:young+adult",
        "ol_subject": "young_adult_fiction",
        "multi_queries": ["young adult fiction", "YA novels", "teen fiction"],
        "children": [
            {"slug": "ya-fantasy", "name": "YA Fantasy", "query": "subject:young+adult+fantasy", "ol_subject": "young_adult_fantasy", "multi_queries": ["young adult fantasy"]},
            {"slug": "ya-scifi", "name": "YA Sci-Fi", "query": "subject:young+adult+science+fiction", "ol_subject": "young_adult_science_fiction", "multi_queries": ["young adult science fiction"]},
            {"slug": "ya-romance", "name": "YA Romance", "query": "subject:young+adult+romance", "ol_subject": "young_adult_romance", "multi_queries": ["young adult romance"]},
            {"slug": "ya-dystopian", "name": "YA Dystopian", "query": "subject:young+adult+dystopian", "ol_subject": "young_adult_dystopian", "multi_queries": ["young adult dystopian"]},
            {"slug": "coming-of-age", "name": "Coming of Age", "query": "subject:coming+of+age", "ol_subject": "coming_of_age", "multi_queries": ["coming of age fiction"]},
        ],
    },
    {
        "slug": "literary-fiction",
        "name": "Literary Fiction",
        "icon": "book-open",
        "query": "subject:literary+fiction",
        "ol_subject": "literary_fiction",
        "multi_queries": ["literary fiction", "literature fiction", "literary novels"],
        "children": [
            {"slug": "contemporary-fiction", "name": "Contemporary", "query": "subject:contemporary+fiction", "ol_subject": "contemporary_fiction", "multi_queries": ["contemporary fiction"]},
            {"slug": "historical-fiction", "name": "Historical Fiction", "query": "subject:historical+fiction", "ol_subject": "historical_fiction", "multi_queries": ["historical fiction"]},
            {"slug": "classics", "name": "Classics", "query": "subject:classic+fiction", "ol_subject": "classics", "multi_queries": ["classic literature"]},
            {"slug": "satire", "name": "Satire", "query": "subject:satire", "ol_subject": "satire", "multi_queries": ["satire fiction"]},
            {"slug": "magical-realism", "name": "Magical Realism", "query": "subject:magical+realism", "ol_subject": "magical_realism", "multi_queries": ["magical realism fiction"]},
            {"slug": "short-stories", "name": "Short Stories", "query": "subject:short+stories", "ol_subject": "short_stories", "multi_queries": ["short story collection"]},
        ],
    },
    {
        "slug": "nonfiction",
        "name": "Non-Fiction",
        "icon": "graduation-cap",
        "query": "subject:nonfiction",
        "ol_subject": "nonfiction",
        "multi_queries": ["nonfiction", "non-fiction books", "general nonfiction"],
        "children": [
            {"slug": "biography", "name": "Biography & Memoir", "query": "subject:biography", "ol_subject": "biography", "multi_queries": ["biography memoir"]},
            {"slug": "history", "name": "History", "query": "subject:history", "ol_subject": "history", "multi_queries": ["history books"]},
            {"slug": "true-crime", "name": "True Crime", "query": "subject:true+crime", "ol_subject": "true_crime", "multi_queries": ["true crime"]},
            {"slug": "science", "name": "Popular Science", "query": "subject:popular+science", "ol_subject": "popular_science", "multi_queries": ["popular science"]},
            {"slug": "self-help", "name": "Self-Help & Relationships", "query": "subject:self-help", "ol_subject": "self-help", "multi_queries": ["self help"]},
            {"slug": "philosophy", "name": "Philosophy", "query": "subject:philosophy", "ol_subject": "philosophy", "multi_queries": ["philosophy"]},
            {"slug": "business", "name": "Business & Finance", "query": "subject:business", "ol_subject": "business", "multi_queries": ["business finance"]},
            {"slug": "psychology", "name": "Psychology", "query": "subject:psychology", "ol_subject": "psychology", "multi_queries": ["psychology"]},
            {"slug": "politics", "name": "Politics", "query": "subject:politics", "ol_subject": "politics", "multi_queries": ["politics current affairs"]},
            {"slug": "travel", "name": "Travel & Adventure", "query": "subject:travel", "ol_subject": "travel", "multi_queries": ["travel books"]},
        ],
    },
    {
        "slug": "adventure",
        "name": "Adventure",
        "icon": "compass",
        "query": "subject:adventure",
        "ol_subject": "adventure_stories",
        "multi_queries": ["adventure fiction", "adventure novels", "action adventure"],
        "children": [
            {"slug": "action-adventure", "name": "Action Adventure", "query": "subject:action+adventure", "ol_subject": "adventure_and_adventurers", "multi_queries": ["action adventure fiction"]},
            {"slug": "survival", "name": "Survival", "query": "subject:survival+fiction", "ol_subject": "survival", "multi_queries": ["survival fiction"]},
            {"slug": "pirate", "name": "Pirate / Nautical", "query": "subject:pirate+fiction", "ol_subject": "pirates", "multi_queries": ["pirate fiction nautical"]},
            {"slug": "exploration", "name": "Exploration", "query": "subject:exploration+fiction", "ol_subject": "exploration", "multi_queries": ["exploration adventure fiction"]},
        ],
    },
    {
        "slug": "humor",
        "name": "Humor",
        "icon": "smile",
        "query": "subject:humor",
        "ol_subject": "humor",
        "multi_queries": ["humor fiction", "humorous fiction", "comedy fiction"],
        "children": [
            {"slug": "comedy-fiction", "name": "Comedy Fiction", "query": "subject:humorous+fiction", "ol_subject": "humorous_fiction", "multi_queries": ["humorous fiction"]},
            {"slug": "parody", "name": "Parody & Satire", "query": "subject:parody", "ol_subject": "parody", "multi_queries": ["parody satire fiction"]},
            {"slug": "comic-fantasy", "name": "Comic Fantasy", "query": "subject:humorous+fantasy", "ol_subject": "comic_fantasy", "multi_queries": ["comic fantasy humorous"]},
        ],
    },
    {
        "slug": "children",
        "name": "Children's",
        "icon": "baby",
        "query": "subject:juvenile+fiction",
        "ol_subject": "juvenile_fiction",
        "multi_queries": ["children's fiction", "kids books", "juvenile fiction"],
        "children": [
            {"slug": "picture-books", "name": "Picture Books", "query": "subject:picture+books", "ol_subject": "picture_books", "multi_queries": ["picture books children"]},
            {"slug": "middle-grade", "name": "Middle Grade", "query": "subject:middle+grade", "ol_subject": "middle_grade", "multi_queries": ["middle grade fiction"]},
            {"slug": "childrens-fantasy", "name": "Children's Fantasy", "query": "subject:juvenile+fantasy", "ol_subject": "juvenile_fantasy", "multi_queries": ["children fantasy fiction"]},
        ],
    },
]

# Build flat lookup maps from the taxonomy
_genre_by_slug: dict[str, dict] = {}
for _g in GENRE_TAXONOMY:
    _genre_by_slug[_g["slug"]] = _g
    for _c in _g.get("children", []):
        _genre_by_slug[_c["slug"]] = _c

# Legacy flat map for backward compat (old API still works)
CATEGORY_SLUGS: dict[str, str] = {"all": "All", "popular": "Popular", "new": "New"}
for _g in GENRE_TAXONOMY:
    CATEGORY_SLUGS[_g["slug"]] = _g["name"]
    for _c in _g.get("children", []):
        CATEGORY_SLUGS[_c["slug"]] = _c["name"]

FEATURED_CATEGORIES = [g["name"] for g in GENRE_TAXONOMY]


def _params(extra: dict | None = None) -> dict:
    p: dict[str, str] = {}
    if settings.google_books_api_key:
        p["key"] = settings.google_books_api_key
    if extra:
        p.update(extra)
    return p


def _normalize_volume(item: dict) -> dict:
    """Extract a consistent BookSummary from a raw Google Books volume."""
    vi = item.get("volumeInfo", {})
    imgs = vi.get("imageLinks", {})

    cover = (
        imgs.get("thumbnail")
        or imgs.get("smallThumbnail")
        or imgs.get("small")
        or ""
    )
    if cover.startswith("http://"):
        cover = cover.replace("http://", "https://", 1)

    identifiers = {}
    for ident in vi.get("industryIdentifiers", []):
        identifiers[ident.get("type", "")] = ident.get("identifier", "")

    return {
        "id": item.get("id", ""),
        "title": vi.get("title", "Unknown"),
        "subtitle": vi.get("subtitle") or "",
        "authors": vi.get("authors") or [],
        "publisher": vi.get("publisher") or "",
        "publishedDate": vi.get("publishedDate") or "",
        "description": vi.get("description") or "",
        "pageCount": vi.get("pageCount") or 0,
        "categories": vi.get("categories") or [],
        "mainCategory": vi.get("mainCategory") or "",
        "averageRating": vi.get("averageRating") or 0,
        "ratingsCount": vi.get("ratingsCount") or 0,
        "language": vi.get("language") or "en",
        "coverUrl": cover,
        "isbn10": identifiers.get("ISBN_10", ""),
        "isbn13": identifiers.get("ISBN_13", ""),
        "previewLink": vi.get("previewLink") or "",
        "infoLink": vi.get("infoLink") or "",
    }


def _normalize_volume_full(item: dict) -> dict:
    """Full detail including larger images and access info."""
    base = _normalize_volume(item)
    vi = item.get("volumeInfo", {})
    imgs = vi.get("imageLinks", {})

    large_cover = (
        imgs.get("extraLarge")
        or imgs.get("large")
        or imgs.get("medium")
        or imgs.get("small")
        or imgs.get("thumbnail")
        or ""
    )
    if large_cover.startswith("http://"):
        large_cover = large_cover.replace("http://", "https://", 1)

    base["coverUrlLarge"] = large_cover
    base["printType"] = vi.get("printType") or ""

    series_info = vi.get("seriesInfo", {})
    if series_info:
        vols = series_info.get("volumeSeries", [])
        if vols:
            vol = vols[0]
            base["seriesName"] = vol.get("seriesId", "")
            base["seriesBookNumber"] = vol.get("orderNumber") or vol.get("bookDisplayNumber", "")
            issue = vol.get("issue", [])
            if issue and isinstance(issue, list):
                first = issue[0] if issue else {}
                base["seriesName"] = first.get("seriesTitle", base["seriesName"])
    if "seriesName" not in base:
        base["seriesName"] = ""
        base["seriesBookNumber"] = ""
    return base


def _cache_get(key: str) -> Any | None:
    if key in _cache:
        ts, data = _cache[key]
        if time.time() - ts < CACHE_TTL:
            return data
        del _cache[key]
    return None


def _cache_set(key: str, data: Any) -> None:
    _cache[key] = (time.time(), data)
    if len(_cache) > 500:
        oldest = min(_cache, key=lambda k: _cache[k][0])
        del _cache[oldest]


async def search_volumes(
    query: str,
    max_results: int = 20,
    start_index: int = 0,
    order_by: str = "relevance",
) -> dict:
    cache_key = f"search:{query}:{max_results}:{start_index}:{order_by}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    params = _params({
        "q": query,
        "maxResults": str(min(max_results, 40)),
        "startIndex": str(start_index),
        "orderBy": order_by,
        "printType": "books",
        "langRestrict": "en",
    })

    for attempt in range(5):
        if attempt > 0:
            wait = min(5 * (2 ** attempt) + random.uniform(1, 3), 60)
            logger.info("Google Books backoff %.1fs before attempt %d", wait, attempt + 1)
            await asyncio.sleep(wait)

        async with _gbooks_semaphore:
            global _last_gbooks_ts
            gap = _MIN_GBOOKS_GAP - (time.monotonic() - _last_gbooks_ts)
            if gap > 0:
                await asyncio.sleep(gap)
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(GOOGLE_BOOKS_URL, params=params, timeout=15)
                    _last_gbooks_ts = time.monotonic()
                    if resp.status_code == 429:
                        logger.warning("Google Books 429 for %s (attempt %d)", query[:60], attempt + 1)
                        continue
                    resp.raise_for_status()
                    data = resp.json()
            except httpx.HTTPStatusError:
                _last_gbooks_ts = time.monotonic()
                if attempt < 4:
                    continue
                return {"books": [], "totalItems": 0}
            except httpx.TimeoutException:
                _last_gbooks_ts = time.monotonic()
                if attempt < 4:
                    continue
                return {"books": [], "totalItems": 0}

        total = data.get("totalItems", 0)
        items = [_normalize_volume(v) for v in data.get("items", [])]

        result = {"books": items, "totalItems": total}
        _cache_set(cache_key, result)
        return result

    logger.warning("Google Books exhausted retries for: %s -- falling back to Open Library", query)
    ol_books = await _open_library_search(query, limit=max_results)
    if ol_books:
        result = {"books": ol_books, "totalItems": len(ol_books)}
        _cache_set(cache_key, result)
        return result
    return {"books": [], "totalItems": 0}


async def get_volume(volume_id: str) -> dict | None:
    cache_key = f"volume:{volume_id}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    for attempt in range(4):
        if attempt > 0:
            wait = min(5 * (2 ** attempt) + random.uniform(1, 3), 60)
            await asyncio.sleep(wait)

        async with _gbooks_semaphore:
            global _last_gbooks_ts
            gap = _MIN_GBOOKS_GAP - (time.monotonic() - _last_gbooks_ts)
            if gap > 0:
                await asyncio.sleep(gap)
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(
                        f"{GOOGLE_BOOKS_URL}/{volume_id}",
                        params=_params(),
                        timeout=15,
                    )
                    _last_gbooks_ts = time.monotonic()
                    if resp.status_code == 404:
                        return None
                    if resp.status_code == 429:
                        continue
                    resp.raise_for_status()
                    data = resp.json()
            except (httpx.HTTPStatusError, httpx.TimeoutException):
                _last_gbooks_ts = time.monotonic()
                if attempt < 3:
                    continue
                return None

        result = _normalize_volume_full(data)
        _cache_set(cache_key, result)
        return result
    return None


async def get_trending(max_results: int = 20) -> list[dict]:
    """Real bestsellers from NYT when API key is set; else improved Google Books fallback."""
    cache_key = "trending"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    # Try NYT Bestsellers first (real trending data)
    try:
        from app.services import nyt_books
        items = await nyt_books.get_trending_from_nyt(max_results=max_results)
        if items:
            _cache_set(cache_key, items)
            return items
    except Exception as e:
        logger.debug("NYT trending fallback: %s", e)

    # Fallback: broad fiction search by relevance (surfaces popular/well-known books)
    result = await search_volumes(
        "subject:fiction", max_results=max_results, order_by="relevance",
    )
    items = result.get("books", [])
    _cache_set(cache_key, items)
    return items


async def get_new_releases(max_results: int = 20) -> list[dict]:
    """Recently published books (new releases) from Google Books."""
    cache_key = "new_releases"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    # Newest fiction across popular genres
    result = await search_volumes(
        "subject:fiction", max_results=max_results, order_by="newest",
    )
    items = result.get("books", [])
    _cache_set(cache_key, items)
    return items


async def get_by_category(
    category: str,
    max_results: int = 20,
    start_index: int = 0,
) -> dict:
    return await search_volumes(
        f"subject:{category}",
        max_results=max_results,
        start_index=start_index,
        order_by="relevance",
    )


# ---------------------------------------------------------------------------
# Genre-aware search: uses the taxonomy query, falls back to Open Library
# if Google Books returns very few results for a niche sub-genre.
# ---------------------------------------------------------------------------


def get_genre_info(slug: str) -> dict | None:
    """Look up a genre/sub-genre by slug."""
    return _genre_by_slug.get(slug)


async def search_by_genre(
    slug: str,
    max_results: int = 20,
    start_index: int = 0,
    order_by: str = "relevance",
    multi_query: bool = False,
) -> dict:
    """Search using the genre taxonomy. When multi_query=True, runs multiple
    queries (subject + free-text) and merges for broader coverage."""
    genre = _genre_by_slug.get(slug)
    if not genre:
        return await search_volumes(
            f"subject:{slug}", max_results=max_results,
            start_index=start_index, order_by=order_by,
        )

    if not multi_query:
        return await _search_by_genre_single(genre, max_results, start_index, order_by)

    # Multi-query: run primary + alt queries, merge and dedupe
    seen_ids: set[str] = set()
    merged_books: list[dict] = []
    total_items = 0

    # 1. Primary query (subject-based)
    primary = await search_volumes(
        genre["query"], max_results=max_results * 2, start_index=0, order_by=order_by,
    )
    for b in primary.get("books", []):
        bid = b.get("id")
        if bid and bid not in seen_ids:
            seen_ids.add(bid)
            merged_books.append(b)
    total_items = max(total_items, primary.get("totalItems", 0))

    # 2. Additional free-text queries
    for alt_q in genre.get("multi_queries", [])[:3]:  # cap at 3 extra queries
        if len(merged_books) >= max_results + 20:
            break
        alt_result = await search_volumes(
            alt_q, max_results=max_results, start_index=0, order_by=order_by,
        )
        for b in alt_result.get("books", []):
            bid = b.get("id")
            if bid and bid not in seen_ids:
                seen_ids.add(bid)
                merged_books.append(b)
        total_items = max(total_items, alt_result.get("totalItems", 0))

    # 3. Open Library fallback if still sparse
    if len(merged_books) < max_results and genre.get("ol_subject"):
        ol_books = await _open_library_subject(
            genre["ol_subject"], limit=max_results * 2,
        )
        seen_titles = {b["title"].lower() for b in merged_books}
        for b in ol_books:
            if b["title"].lower() not in seen_titles and b.get("id") not in seen_ids:
                merged_books.append(b)
                seen_ids.add(b.get("id", ""))
                seen_titles.add(b["title"].lower())

    # Paginate
    total_items = max(total_items, len(merged_books))
    page_books = merged_books[start_index : start_index + max_results]

    return {"books": page_books, "totalItems": total_items}


async def _search_by_genre_single(
    genre: dict, max_results: int, start_index: int, order_by: str,
) -> dict:
    """Single-query genre search (for carousels / main page browse)."""
    result = await search_volumes(
        genre["query"], max_results=max_results,
        start_index=start_index, order_by=order_by,
    )
    if len(result["books"]) < 5 and genre.get("ol_subject"):
        ol_books = await _open_library_subject(
            genre["ol_subject"], limit=max_results - len(result["books"]),
        )
        seen_titles = {b["title"].lower() for b in result["books"]}
        for b in ol_books:
            if b["title"].lower() not in seen_titles:
                result["books"].append(b)
                seen_titles.add(b["title"].lower())
        result["totalItems"] = max(result["totalItems"], len(result["books"]))
    return result


async def _open_library_subject(
    subject: str, limit: int = 20,
) -> list[dict]:
    """Fetch books from Open Library's Subjects API and normalise them."""
    cache_key = f"ol_subject:{subject}:{limit}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    url = f"{OPEN_LIBRARY_SUBJECTS_URL}/{subject}.json"
    params = {"limit": str(min(limit, 50))}

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, params=params, timeout=15)
            if resp.status_code != 200:
                return []
            data = resp.json()
    except Exception:
        logger.warning("Open Library subject fetch failed for %s", subject)
        return []

    books: list[dict] = []
    for work in data.get("works", []):
        cover_id = work.get("cover_id")
        cover = (
            f"https://covers.openlibrary.org/b/id/{cover_id}-M.jpg"
            if cover_id
            else ""
        )
        authors = [a.get("name", "") for a in work.get("authors", [])]
        ol_key = work.get("key", "")
        books.append({
            "id": f"OL:{ol_key}",
            "title": work.get("title", "Unknown"),
            "subtitle": "",
            "authors": authors,
            "publisher": "",
            "publishedDate": str(work.get("first_publish_year", "")),
            "description": "",
            "pageCount": 0,
            "categories": [subject.replace("_", " ").title()],
            "mainCategory": subject.replace("_", " ").title(),
            "averageRating": 0,
            "ratingsCount": 0,
            "language": "en",
            "coverUrl": cover,
            "isbn10": "",
            "isbn13": "",
            "previewLink": f"https://openlibrary.org{ol_key}" if ol_key else "",
            "infoLink": f"https://openlibrary.org{ol_key}" if ol_key else "",
        })

    _cache_set(cache_key, books)
    return books


OPEN_LIBRARY_SEARCH_URL = "https://openlibrary.org/search.json"


async def get_open_library_work(ol_key: str) -> dict | None:
    """Fetch a single work from Open Library and return it in our detail format."""
    cache_key = f"ol_work:{ol_key}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    url = f"https://openlibrary.org{ol_key}.json"
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.get(url, timeout=15)
            if resp.status_code != 200:
                return None
            data = resp.json()
    except Exception:
        logger.warning("Open Library work fetch failed for %s", ol_key)
        return None

    title = data.get("title", "Unknown")
    description = data.get("description", "")
    if isinstance(description, dict):
        description = description.get("value", "")

    cover_ids = data.get("covers", [])
    cover = ""
    cover_large = ""
    if cover_ids:
        cid = cover_ids[0]
        cover = f"https://covers.openlibrary.org/b/id/{cid}-M.jpg"
        cover_large = f"https://covers.openlibrary.org/b/id/{cid}-L.jpg"

    author_keys = [a.get("author", {}).get("key", "") if isinstance(a, dict) else "" for a in data.get("authors", [])]
    authors = []
    async with httpx.AsyncClient(follow_redirects=True) as aclient:
        for ak in author_keys[:3]:
            if not ak:
                continue
            try:
                aresp = await aclient.get(f"https://openlibrary.org{ak}.json", timeout=10)
                if aresp.status_code == 200:
                    authors.append(aresp.json().get("name", ""))
            except Exception:
                pass

    subjects = data.get("subjects", [])[:10]

    result = {
        "id": f"OL:{ol_key}",
        "title": title,
        "subtitle": "",
        "authors": authors,
        "publisher": "",
        "publishedDate": str(data.get("first_publish_date", "")),
        "description": description,
        "pageCount": 0,
        "categories": subjects,
        "mainCategory": subjects[0] if subjects else "",
        "averageRating": 0,
        "ratingsCount": 0,
        "language": "en",
        "coverUrl": cover,
        "coverUrlLarge": cover_large,
        "printType": "BOOK",
        "isbn10": "",
        "isbn13": "",
        "previewLink": f"https://openlibrary.org{ol_key}",
        "infoLink": f"https://openlibrary.org{ol_key}",
    }

    _cache_set(cache_key, result)
    return result


async def _open_library_search(
    query: str, limit: int = 20,
) -> list[dict]:
    """General-purpose Open Library search, used as fallback."""
    cache_key = f"ol_search:{query}:{limit}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    q = query.replace("subject:", "").strip()
    params = {"q": q, "limit": str(min(limit, 50)), "language": "eng"}

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(OPEN_LIBRARY_SEARCH_URL, params=params, timeout=20)
            if resp.status_code != 200:
                return []
            data = resp.json()
    except Exception:
        logger.warning("Open Library search failed for %s", q)
        return []

    books: list[dict] = []
    for doc in data.get("docs", [])[:limit]:
        cover_id = doc.get("cover_i")
        cover = (
            f"https://covers.openlibrary.org/b/id/{cover_id}-M.jpg"
            if cover_id
            else ""
        )
        authors = doc.get("author_name", [])
        ol_key = doc.get("key", "")
        isbn_list = doc.get("isbn", [])
        books.append({
            "id": f"OL:{ol_key}",
            "title": doc.get("title", "Unknown"),
            "subtitle": "",
            "authors": authors[:3],
            "publisher": (doc.get("publisher", []) or [""])[0],
            "publishedDate": str(doc.get("first_publish_year", "")),
            "description": "",
            "pageCount": doc.get("number_of_pages_median", 0) or 0,
            "categories": doc.get("subject", [])[:5],
            "mainCategory": (doc.get("subject", []) or [""])[0],
            "averageRating": doc.get("ratings_average", 0) or 0,
            "ratingsCount": doc.get("ratings_count", 0) or 0,
            "language": "en",
            "coverUrl": cover,
            "isbn10": isbn_list[0] if isbn_list else "",
            "isbn13": next((i for i in isbn_list if len(i) == 13), ""),
            "previewLink": f"https://openlibrary.org{ol_key}" if ol_key else "",
            "infoLink": f"https://openlibrary.org{ol_key}" if ol_key else "",
        })

    _cache_set(cache_key, books)
    return books
