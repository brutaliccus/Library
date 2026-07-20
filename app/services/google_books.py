import asyncio
import logging
import random
import re
import time
from typing import Any

import httpx
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

GOOGLE_BOOKS_URL = "https://www.googleapis.com/books/v1/volumes"
OPEN_LIBRARY_SUBJECTS_URL = "https://openlibrary.org/subjects"

# Open Library aggressively rate-limits / IP-bans clients that send a generic
# User-Agent (e.g. the default "python-httpx/x.y"). Always identify ourselves.
OPEN_LIBRARY_HEADERS = {
    "User-Agent": settings.open_library_user_agent
    or "LibrarySite/1.0 (+https://library.example.com)",
    "Accept": "application/json",
}

_cache: dict[str, tuple[float, Any]] = {}
CACHE_TTL = 1800  # 30 minutes

# With an API key we can handle more concurrency; still cap to avoid bursts.
_gbooks_semaphore = asyncio.Semaphore(5)
_last_gbooks_ts: float = 0.0
_MIN_GBOOKS_GAP = 0.1  # seconds between requests

# ---------------------------------------------------------------------------
# Open Library circuit breaker
# Open Library (Internet Archive) will IP-ban clients that keep hammering it
# while it's failing. Once we see repeated connection-level failures we stop
# hitting it for a cooldown window instead of firing hundreds of doomed
# requests (which is what perpetuates the ban).
# ---------------------------------------------------------------------------
_OL_FAIL_THRESHOLD = 3
_OL_COOLDOWN_SECONDS = 900  # 15 minutes
_ol_consecutive_failures = 0
_ol_cooldown_until = 0.0


def _ol_available() -> bool:
    """False while the Open Library circuit breaker is open (cooling down)."""
    return time.time() >= _ol_cooldown_until


def _ol_record_success() -> None:
    global _ol_consecutive_failures, _ol_cooldown_until
    if _ol_consecutive_failures or _ol_cooldown_until:
        logger.info("Open Library reachable again; circuit closed")
    _ol_consecutive_failures = 0
    _ol_cooldown_until = 0.0


def _ol_record_failure() -> None:
    global _ol_consecutive_failures, _ol_cooldown_until
    _ol_consecutive_failures += 1
    if _ol_consecutive_failures >= _OL_FAIL_THRESHOLD and _ol_available():
        _ol_cooldown_until = time.time() + _OL_COOLDOWN_SECONDS
        logger.warning(
            "Open Library circuit OPEN: pausing OL requests for %ss after %s consecutive failures",
            _OL_COOLDOWN_SECONDS,
            _ol_consecutive_failures,
        )


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
    """Search the store catalog for user-facing browse/search.

    The local dump-based catalog (app.services.ol_catalog, on the SSD) is the
    primary source: it's fast and never rate-limited/IP-banned. The live Open
    Library API and Google Books are only fallbacks for when the local catalog
    is missing or returns nothing.
    """
    cache_key = f"catalog_search:{query}:{max_results}:{start_index}:{order_by}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    q = query.strip()
    is_subject = q.lower().startswith("subject:")

    # Primary: local catalog (SSD). Subject queries use the subjects index;
    # everything else uses title FTS.
    result: dict = {"books": [], "totalItems": 0}
    try:
        from app.services import ol_catalog
        if ol_catalog.catalog_ready():
            if is_subject:
                subj = q.split(":", 1)[1].replace("+", " ").strip()
                result = await ol_catalog.browse_by_subject(
                    subj, limit=max_results, offset=start_index,
                )
            else:
                books = await ol_catalog.search_by_title(
                    q, limit=max_results, offset=start_index,
                )
                if books:
                    result = {"books": books, "totalItems": start_index + len(books) + (
                        max_results if len(books) == max_results else 0)}
    except Exception as e:
        logger.debug("ol_catalog search failed for %r: %s", q[:60], e)

    # Fallback: live Open Library API (circuit-breaker guarded).
    if not result.get("books"):
        result = await _open_library_search_volumes(query, max_results, start_index, order_by)

    # Fallback / supplement: ISBNdb (larger commercial catalog than OL).
    if not result.get("books"):
        try:
            from app.services import isbndb

            page = (start_index // max(1, max_results)) + 1
            result = await isbndb.search_books(q, limit=max_results, page=page)
        except Exception as e:
            logger.debug("ISBNdb search failed for %r: %s", q[:60], e)

    # Fallback: Google Books.
    if not result.get("books"):
        result = await _google_books_search(query, max_results, start_index, order_by)

    if result.get("books"):
        # Local OL dump often omits cover_id or only has a blank stub image —
        # enrich (and strip stubs) so store cards aren't silent empty tiles.
        books = result["books"]
        enriched = await asyncio.gather(
            *(enrich_cover_if_missing(b) for b in books)
        )
        result = {**result, "books": list(enriched)}
        _cache_set(cache_key, result)
    return result


async def _google_books_search(
    query: str,
    max_results: int,
    start_index: int,
    order_by: str,
) -> dict:
    """Search Google Books. Returns empty result on any failure (caller falls back)."""
    if not settings.google_books_api_key:
        return {"books": [], "totalItems": 0}

    q = query.strip()
    # Normalise a leading "subject:foo+bar" into Google's "subject:foo bar" form.
    if q.lower().startswith("subject:"):
        q = f"subject:{q.split(':', 1)[1].replace('+', ' ').strip()}"

    params = _params({
        "q": q,
        "maxResults": str(min(max(1, max_results), 40)),
        "startIndex": str(max(0, start_index)),
        "orderBy": "newest" if order_by == "newest" else "relevance",
        "printType": "books",
        "langRestrict": "en",
    })

    request_timeout = min(10.0, float(settings.google_books_search_timeout))
    max_retries = max(0, int(settings.google_books_max_429_retries))
    global _last_gbooks_ts
    resp = None
    try:
        async with _gbooks_semaphore:
            for attempt in range(max_retries + 1):
                gap = time.time() - _last_gbooks_ts
                if gap < _MIN_GBOOKS_GAP:
                    await asyncio.sleep(_MIN_GBOOKS_GAP - gap)
                async with httpx.AsyncClient() as client:
                    resp = await client.get(GOOGLE_BOOKS_URL, params=params, timeout=request_timeout)
                _last_gbooks_ts = time.time()
                if resp.status_code not in (429, 503) or attempt >= max_retries:
                    break
                await asyncio.sleep(0.5 * (attempt + 1))
        if resp is None:
            return {"books": [], "totalItems": 0}
        if resp.status_code == 429 or resp.status_code >= 500:
            logger.info("Google Books search %s for %r; falling back", resp.status_code, q[:60])
            return {"books": [], "totalItems": 0}
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning("Google Books search failed for %r: %s", q[:60], e)
        return {"books": [], "totalItems": 0}

    items = data.get("items", []) or []
    books = [_normalize_volume(it) for it in items]
    total = int(data.get("totalItems") or len(books))
    return {"books": books, "totalItems": total}


async def _open_library_search_volumes(
    query: str,
    max_results: int,
    start_index: int,
    order_by: str,
) -> dict:
    """Fallback catalog search via Open Library's search API."""
    if not _ol_available():
        return {"books": [], "totalItems": 0}

    q = query.strip()
    if q.lower().startswith("subject:"):
        q = f"subject:{q.split(':', 1)[1].replace('+', ' ').strip()}"

    page_size = min(max(1, max_results), 50)
    page = (start_index // page_size) + 1
    skip = start_index % page_size

    params: dict[str, str] = {
        "q": q,
        "limit": str(page_size),
        "page": str(page),
        "language": "eng",
    }
    if order_by == "newest":
        params["sort"] = "new"

    try:
        async with httpx.AsyncClient(headers=OPEN_LIBRARY_HEADERS) as client:
            resp = await client.get(OPEN_LIBRARY_SEARCH_URL, params=params, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        _ol_record_success()
    except Exception as e:
        _ol_record_failure()
        logger.warning("Open Library search failed for %r: %s", q[:60], e)
        return {"books": [], "totalItems": 0}

    docs = data.get("docs") or []
    if skip:
        docs = docs[skip:]
    books = _normalize_open_library_docs(docs[:page_size])
    total = int(data.get("numFound") or len(books))
    return {"books": books, "totalItems": total}


async def get_volume(volume_id: str) -> dict | None:
    """Legacy Google Books volume lookup (older cache rows only)."""
    if volume_id.startswith("OL:"):
        return await get_open_library_work(volume_id[3:])
    if volume_id.startswith("ISBN:"):
        from app.services import isbndb

        return await isbndb.get_volume(volume_id)

    cache_key = f"volume:{volume_id}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    if not settings.google_books_api_key:
        return None

    request_timeout = min(8.0, float(settings.google_books_search_timeout))
    try:
        async with _gbooks_semaphore:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{GOOGLE_BOOKS_URL}/{volume_id}",
                    params=_params(),
                    timeout=request_timeout,
                )
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
                data = resp.json()
    except Exception:
        return None

    result = _normalize_volume_full(data)
    _cache_set(cache_key, result)
    return result


async def get_catalog_volume(volume_id: str) -> dict | None:
    """Resolve a store volume id (Open Library, ISBNdb, or legacy Google Books)."""
    if volume_id.startswith("OL:"):
        book = await get_open_library_work(volume_id[3:])
    elif volume_id.startswith("ISBN:"):
        from app.services import isbndb

        book = await isbndb.get_volume(volume_id)
    else:
        book = await get_volume(volume_id)
    if book:
        return await enrich_cover_if_missing(book)
    return None


async def enrich_cover_if_missing(book: dict) -> dict:
    """Fill empty/broken coverUrl from ISBNdb / Google / Hardcover / OL ISBN.

    Many works in the local Open Library dump have no cover_id (or a tiny OL
    placeholder). Series carousels often still show art via Hardcover — detail
    pages need the same enrichment path.
    """
    if not book:
        return book

    existing = (book.get("coverUrl") or "").strip()
    if existing:
        # Keep non-OL covers; re-check OL URLs that commonly 200 with a ~40B stub.
        if "covers.openlibrary.org" not in existing:
            return book
        if await _cover_url_looks_real(existing):
            return book

    title = (book.get("title") or "").strip()
    # Skip OL "Duplicate of …" junk titles for enrichment lookups.
    if "duplicate of ol" in title.lower():
        title = title.split("(")[0].strip()
    authors = book.get("authors") or []
    author = authors[0] if authors else ""
    isbn13 = (book.get("isbn13") or "").strip()
    isbn10 = (book.get("isbn10") or "").strip()

    cover = ""
    cover_large = ""

    # 1) Direct Open Library cover-by-ISBN (fast, no auth).
    for isbn in (isbn13, isbn10):
        if not isbn:
            continue
        candidate = f"https://covers.openlibrary.org/b/isbn/{isbn}-M.jpg"
        if await _cover_url_looks_real(candidate):
            cover = candidate
            cover_large = f"https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg"
            break

    # 2) ISBNdb (often has commercial covers OL dump lacks).
    if not cover:
        try:
            from app.services import isbndb

            if isbn13 or isbn10:
                hit = await isbndb.lookup_isbn(isbn13 or isbn10)
                if hit and hit.get("coverUrl"):
                    cover = hit["coverUrl"]
                    cover_large = hit.get("coverUrlLarge") or cover
            if not cover and title:
                q = f"{title} {author}".strip()
                result = await isbndb.search_books(q, limit=3)
                for b in result.get("books") or []:
                    if b.get("coverUrl") and _titles_roughly_match(title, b.get("title") or ""):
                        cover = b["coverUrl"]
                        cover_large = b.get("coverUrlLarge") or cover
                        break
        except Exception as e:
            logger.debug("cover enrich ISBNdb failed: %s", e)

    # 3) Google Books title search.
    if not cover and title and settings.google_books_api_key:
        try:
            queries = [f'intitle:"{title}"']
            if author:
                queries.append(f'intitle:"{title}" inauthor:"{author}"')
            queries.append(title)  # plain fallback for self-pub / audiobook-only
            for q in queries:
                result = await _google_books_search(
                    q, max_results=5, start_index=0, order_by="relevance"
                )
                books = result.get("books") or []
                for b in books:
                    if not b.get("coverUrl"):
                        continue
                    if _titles_roughly_match(title, b.get("title") or ""):
                        cover = b["coverUrl"]
                        cover_large = b.get("coverUrlLarge") or cover
                        break
                if cover:
                    break
                if len(books) == 1 and books[0].get("coverUrl"):
                    cover = books[0]["coverUrl"]
                    cover_large = books[0].get("coverUrlLarge") or cover
                    break
        except Exception as e:
            logger.debug("cover enrich Google Books failed: %s", e)

    # 4) Hardcover — same source that powers "More in this series" covers.
    if not cover and title:
        try:
            from app.services import hardcover

            q = f"{title} {author}".strip()
            hits = await hardcover.search_books(q, limit=5)
            for b in hits or []:
                if not b.get("coverUrl"):
                    continue
                if _titles_roughly_match(title, b.get("title") or ""):
                    cover = b["coverUrl"]
                    cover_large = b.get("coverUrlLarge") or cover
                    break
        except Exception as e:
            logger.debug("cover enrich Hardcover failed: %s", e)

    if cover:
        book = dict(book)
        book["coverUrl"] = cover
        book["coverUrlLarge"] = cover_large or cover
        # Refresh cache entry for OL works so the next hit is cheap.
        vid = book.get("volumeId") or book.get("id") or ""
        if vid.startswith("OL:"):
            _cache_set(f"ol_work:{vid[3:]}", book)
    else:
        # Drop OL stub URLs (~43B blank JPEG) so cards show a placeholder, not a
        # silent empty tile (stubs return HTTP 200 and never fire img.onError).
        existing = (book.get("coverUrl") or "").strip()
        if existing and "covers.openlibrary.org" in existing:
            if not await _cover_url_looks_real(existing):
                book = dict(book)
                book["coverUrl"] = ""
                book["coverUrlLarge"] = ""
                vid = book.get("volumeId") or book.get("id") or ""
                if vid.startswith("OL:"):
                    _cache_set(f"ol_work:{vid[3:]}", book)
    return book


def _titles_roughly_match(a: str, b: str) -> bool:
    def toks(s: str) -> set[str]:
        # Normalize curly apostrophes so "Anarchist's" matches "Anarchist's".
        s = (s or "").lower().replace("\u2019", "'").replace("\u2018", "'")
        return {t for t in re.findall(r"[a-z0-9]+", s) if len(t) >= 3}

    ta, tb = toks(a), toks(b)
    if not ta or not tb:
        return False
    return len(ta & tb) / max(len(ta), 1) >= 0.4


async def _cover_url_looks_real(url: str) -> bool:
    """OL returns a tiny placeholder for missing covers — reject those.

    HEAD often omits Content-Length on covers.openlibrary.org, so we must GET a
    small prefix and measure the body. Real covers are >>2KB; stubs are ~40 bytes.
    """
    if not (url or "").strip():
        return False
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=6.0) as client:
            resp = await client.get(
                url,
                headers={"Range": "bytes=0-4095"},
            )
            if resp.status_code >= 400:
                return False
            body_len = len(resp.content or b"")
            # Range responses may be 206 with a short slice of a large file —
            # treat a full 4KB chunk as real. Tiny bodies are OL stubs.
            if body_len > 2000:
                return True
            cl = resp.headers.get("content-length") or resp.headers.get("Content-Length")
            if cl is not None:
                try:
                    return int(cl) > 2000
                except ValueError:
                    pass
            return False
    except Exception:
        return False


async def get_trending(max_results: int = 20) -> list[dict]:
    """Popular / recent fiction from the local catalog."""
    cache_key = "trending_ol"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    items: list[dict] = []
    try:
        from app.services import ol_catalog
        if ol_catalog.catalog_ready():
            # Year-indexed scan is much faster than matching the ultra-broad
            # "fiction" subject token across 20M+ works.
            result = await ol_catalog.recent_works(limit=max_results, min_year=2005)
            items = result.get("books", [])
    except Exception as e:
        logger.debug("ol_catalog trending failed: %s", e)

    if not items:
        result = await _local_subject_browse("fantasy", max_results, 0)
        items = result.get("books", [])
    if not items:
        result = await _open_library_subject("fiction", limit=max_results, offset=0)
        items = result.get("books", [])
    if not items:
        result = await search_volumes("subject:fiction", max_results=max_results)
        items = result.get("books", [])
    if items:
        _cache_set(cache_key, items)
    return items


async def get_new_releases(max_results: int = 20) -> list[dict]:
    """Recently published works from Open Library search."""
    cache_key = "new_releases_ol"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    items: list[dict] = []
    try:
        from app.services import ol_catalog
        if ol_catalog.catalog_ready():
            result = await ol_catalog.recent_works(limit=max_results)
            items = result.get("books", [])
    except Exception as e:
        logger.debug("ol_catalog recent_works failed: %s", e)
    if not items:
        result = await search_volumes("fiction", max_results=max_results, order_by="newest")
        items = result.get("books", [])
    if items:
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


def genre_subject_fts_expr(slug: str) -> str:
    """Build an FTS5 MATCH expression for a genre's subjects.

    Combines the genre's ``ol_subject`` plus any ``multi_queries`` into an OR of
    quoted phrases (e.g. ``"science fiction" OR "sci fi" OR "space opera"``). Used
    to filter the matched-volume subject index — broad on purpose so a genre
    surfaces everything we have tagged with a related subject. Safe against FTS
    syntax errors: only alphanumeric tokens survive.
    """
    genre = _genre_by_slug.get(slug)
    phrases: list[str] = []
    seen: set[str] = set()

    def _add(raw: str) -> None:
        toks = re.findall(r"[0-9a-zA-Z]+", (raw or "").lower())
        if not toks:
            return
        phrase = " ".join(toks)
        if phrase not in seen:
            seen.add(phrase)
            phrases.append(f'"{phrase}"')

    if genre:
        _add((genre.get("ol_subject") or "").replace("_", " "))
        for mq in genre.get("multi_queries", []) or []:
            _add(mq)
        _add(genre.get("name", ""))
    else:
        _add(slug.replace("-", " ").replace("_", " "))

    return " OR ".join(phrases)


async def search_by_genre(
    slug: str,
    max_results: int = 20,
    start_index: int = 0,
    order_by: str = "relevance",
    multi_query: bool = False,
) -> dict:
    """Genre browse: local subjects index first, then live OL, then Google Books."""
    genre = _genre_by_slug.get(slug)
    subject = (genre.get("ol_subject") if genre else "") or slug.replace("-", " ")

    local = await _local_subject_browse(subject, max_results, start_index)
    if local.get("books"):
        return local

    if genre and genre.get("ol_subject"):
        result = await _open_library_subject(
            genre["ol_subject"], limit=max_results, offset=start_index,
        )
        if result.get("books"):
            return result
    gq = (genre.get("query") if genre else None) or f"subject:{subject}"
    result = await _google_books_search(gq, max_results, start_index, order_by)
    if result.get("books"):
        return result

    label = genre.get("name", slug) if genre else slug.replace("_", " ")
    return await search_volumes(label, max_results=max_results, start_index=start_index, order_by=order_by)


async def _local_subject_browse(subject: str, max_results: int, start_index: int) -> dict:
    try:
        from app.services import ol_catalog
        if ol_catalog.catalog_ready():
            return await ol_catalog.browse_by_subject(
                subject, limit=max_results, offset=start_index,
            )
    except Exception as e:
        logger.debug("ol_catalog subject browse failed for %r: %s", subject, e)
    return {"books": [], "totalItems": 0}


async def _search_by_genre_single(
    genre: dict, max_results: int, start_index: int, order_by: str,
) -> dict:
    subject = genre.get("ol_subject") or genre.get("name", "fiction")
    local = await _local_subject_browse(subject, max_results, start_index)
    if local.get("books"):
        return local
    if genre.get("ol_subject"):
        result = await _open_library_subject(
            genre["ol_subject"], limit=max_results, offset=start_index,
        )
        if result.get("books"):
            return result
    gq = genre.get("query") or f"subject:{subject}"
    result = await _google_books_search(gq, max_results, start_index, order_by)
    if result.get("books"):
        return result
    return await search_volumes(
        genre.get("name", genre.get("query", "fiction")),
        max_results=max_results,
        start_index=start_index,
        order_by=order_by,
    )


def _normalize_open_library_docs(docs: list[dict]) -> list[dict]:
    books: list[dict] = []
    for doc in docs:
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
            "volumeId": f"OL:{ol_key}",
            "title": doc.get("title", "Unknown"),
            "subtitle": "",
            "authors": authors[:3] if isinstance(authors, list) else [],
            "publisher": (doc.get("publisher", []) or [""])[0],
            "publishedDate": str(doc.get("first_publish_year", "")),
            "description": "",
            "pageCount": doc.get("number_of_pages_median", 0) or 0,
            "categories": (doc.get("subject", []) or [])[:5],
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
    return books


async def _open_library_subject(
    subject: str, limit: int = 20, offset: int = 0,
) -> dict:
    """Fetch books from Open Library's Subjects API and normalise them."""
    cache_key = f"ol_subject:{subject}:{limit}:{offset}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    if not _ol_available():
        return {"books": [], "totalItems": 0}

    url = f"{OPEN_LIBRARY_SUBJECTS_URL}/{subject}.json"
    params = {"limit": str(min(limit, 50)), "offset": str(max(0, offset))}

    try:
        async with httpx.AsyncClient(headers=OPEN_LIBRARY_HEADERS) as client:
            resp = await client.get(url, params=params, timeout=15)
            if resp.status_code != 200:
                if resp.status_code == 429 or resp.status_code >= 500:
                    _ol_record_failure()
                return {"books": [], "totalItems": 0}
            data = resp.json()
        _ol_record_success()
    except Exception:
        _ol_record_failure()
        logger.warning("Open Library subject fetch failed for %s", subject)
        return {"books": [], "totalItems": 0}

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
            "volumeId": f"OL:{ol_key}",
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

    result = {"books": books, "totalItems": int(data.get("work_count") or len(books))}
    _cache_set(cache_key, result)
    return result


OPEN_LIBRARY_SEARCH_URL = "https://openlibrary.org/search.json"


async def _local_ol_work(ol_key: str, cache_key: str) -> dict | None:
    """Resilience fallback: read a work from the local dump catalog when the live
    Open Library API is unavailable."""
    try:
        from app.services import ol_catalog
        if ol_catalog.catalog_ready():
            local = await ol_catalog.get_work(ol_key)
            if local:
                _cache_set(cache_key, local)
                return local
    except Exception as e:
        logger.debug("ol_catalog get_work fallback failed for %s: %s", ol_key, e)
    return None


async def get_open_library_work(ol_key: str) -> dict | None:
    """Fetch a single work, local catalog first then live Open Library.

    The local dump catalog (SSD) is authoritative for the store so detail pages
    never depend on the live API. We only hit live Open Library when the work is
    absent from the local catalog (rare) or the catalog isn't built yet.
    """
    cache_key = f"ol_work:{ol_key}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    local = await _local_ol_work(ol_key, cache_key)
    if local is not None:
        return local

    if not _ol_available():
        return None

    url = f"https://openlibrary.org{ol_key}.json"
    try:
        async with httpx.AsyncClient(follow_redirects=True, headers=OPEN_LIBRARY_HEADERS) as client:
            resp = await client.get(url, timeout=15)
            if resp.status_code != 200:
                if resp.status_code == 429 or resp.status_code >= 500:
                    _ol_record_failure()
                return await _local_ol_work(ol_key, cache_key)
            data = resp.json()
        _ol_record_success()
    except Exception:
        _ol_record_failure()
        logger.warning("Open Library work fetch failed for %s", ol_key)
        return await _local_ol_work(ol_key, cache_key)

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
    async with httpx.AsyncClient(follow_redirects=True, headers=OPEN_LIBRARY_HEADERS) as aclient:
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
        "volumeId": f"OL:{ol_key}",
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


async def search_open_library(query: str, *, limit: int = 20) -> list[dict]:
    """Search Open Library; each book has id like OL:/works/OL123W."""
    result = await search_volumes(query, max_results=limit, start_index=0)
    return result.get("books") or []


async def _open_library_search(
    query: str, limit: int = 20,
) -> list[dict]:
    """General-purpose Open Library search."""
    result = await search_volumes(query, max_results=limit, start_index=0)
    return result.get("books") or []
