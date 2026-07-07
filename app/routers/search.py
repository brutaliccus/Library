import asyncio
import logging
import re
import time
from difflib import SequenceMatcher

from fastapi import APIRouter, Depends, Query

from app.config import get_settings
from app.models import User
from app.utils.auth import get_current_user
from app.services import prowlarr, audiobookshelf, kavita, annas_archive, debrid, real_debrid
from app.services.download_discovery import (
    build_annas_archive_query,
    build_audiobookbay_queries,
    build_prowlarr_queries,
    build_search_result_payload,
    filter_irrelevant_results,
    merge_indexer_results,
    order_results_for_display,
    rank_indexer_results,
    resolve_book_search_context,
)
from app.services import indexer_cache

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(prefix="/api/search", tags=["search"])

_NOISE_RE = re.compile(
    r"\[.*?\]|\(.*?\)"
    r"|\.mp3|\.m4b|\.epub|\.mobi|\.pdf|\.azw\d?"
    r"|audiobook|unabridged|abridged|narrated\s+by"
    r"|complete\s+series|series|book\s+\d+",
    re.IGNORECASE,
)

SHORT_TITLE_WORDS = 3


def _clean(s: str) -> str:
    """Strip noise (tags, extensions, common words) for better comparison."""
    s = _NOISE_RE.sub(" ", s or "")
    s = re.sub(r"[^a-zA-Z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def _tokenize(s: str) -> set[str]:
    return set(_clean(s).split())


def _token_overlap(library_title: str, result_title: str) -> float:
    """Fraction of the library title's tokens that appear in the result title."""
    lib_tokens = _tokenize(library_title)
    res_tokens = _tokenize(result_title)
    if not lib_tokens:
        return 0.0
    return len(lib_tokens & res_tokens) / len(lib_tokens)


def _seq_ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, _clean(a), _clean(b)).ratio()


def _author_in_result(library_author: str, result_title: str) -> bool:
    """Check if the library author's name tokens appear in the result title."""
    if not library_author:
        return False
    author_tokens = _tokenize(library_author)
    result_tokens = _tokenize(result_title)
    if not author_tokens:
        return False
    overlap = len(author_tokens & result_tokens) / len(author_tokens)
    return overlap >= 0.8


def _title_matches(result_title: str, library_title: str, library_author: str = "") -> bool:
    """High-confidence fuzzy match between an indexer result and a library item.

    For short titles (<=3 meaningful words), requires the author to also match
    in the result to avoid false positives from common titles.
    """
    if not library_title:
        return False

    rc = _clean(result_title)
    lc = _clean(library_title)

    if not lc:
        return False

    lib_word_count = len(lc.split())
    is_short_title = lib_word_count <= SHORT_TITLE_WORDS
    has_author = bool(library_author and _clean(library_author))
    author_found = _author_in_result(library_author, result_title)

    # For short titles with a known author, require author presence in the result
    # to prevent "The Stand" by Stephen King matching "The Stand" by someone else
    if is_short_title and has_author and not author_found:
        return False

    # Fast path: exact or substring on cleaned text
    if lc == rc or lc in rc or rc in lc:
        return True

    # Token overlap: >=80% of library title tokens found in the result
    if _token_overlap(library_title, result_title) >= 0.8:
        return True

    # SequenceMatcher ratio on cleaned text
    if _seq_ratio(result_title, library_title) >= 0.75:
        return True

    # Author confirmed in result: relax title thresholds
    if author_found:
        if _token_overlap(library_title, result_title) >= 0.6:
            return True
        if _seq_ratio(result_title, library_title) >= 0.5:
            return True

    return False


def _enrich_results(
    raw_results: list,
    abs_items: list,
    kavita_items: list,
    source: str,
) -> list:
    """Add inLibrary and source to results."""
    results = []
    for r in raw_results:
        in_library: list[str] = []
        rt = (r.get("title") or "").strip()
        media_type = (r.get("mediaType") or "unknown").lower()
        if media_type == "audiobook":
            for item in abs_items:
                if _title_matches(rt, item["title"], item.get("author") or ""):
                    in_library.append("audiobookshelf")
                    break
        if media_type == "ebook":
            for item in kavita_items:
                if _title_matches(rt, item["title"], item.get("author") or ""):
                    in_library.append("kavita")
                    break
        r = dict(r)
        r["inLibrary"] = in_library
        r.setdefault("source", source)
        results.append(r)
    return results


async def _fetch_library_for_enrich(library_query: str) -> tuple[list, list]:
    """ABS + Kavita titles for in-library badges on indexer results."""
    gathered = await asyncio.gather(
        audiobookshelf.search_library(library_query),
        kavita.search_library(library_query),
        return_exceptions=True,
    )
    abs_items = gathered[0] if not isinstance(gathered[0], Exception) else []
    kavita_items = gathered[1] if not isinstance(gathered[1], Exception) else []
    for i, g in enumerate(gathered):
        if isinstance(g, Exception):
            logger.warning("Library enrich source %s failed: %s", ["audiobookshelf", "kavita"][i], g)
    return abs_items, kavita_items


async def _enrich_with_library_timeout(library_query: str) -> tuple[list, list]:
    timeout = max(0.5, float(settings.search_library_enrich_timeout))
    try:
        return await asyncio.wait_for(_fetch_library_for_enrich(library_query), timeout=timeout)
    except asyncio.TimeoutError:
        logger.info("Library enrich timed out after %.1fs (indexer results still returned)", timeout)
        return [], []


def _info_hash_for_result(result: dict) -> str | None:
    return real_debrid.extract_info_hash(
        result.get("magnetUrl"),
        result.get("infoHash") or None,
        result.get("downloadUrl"),
    )


async def _annotate_rd_cached(results: list[dict]) -> list[dict]:
    """Mark indexer torrents already cached on any configured debrid provider
    (instant after add). Does not filter results."""
    if not results:
        return results

    def _blank(r: dict) -> dict:
        return {**r, "rdCached": False, "torboxCached": False, "cachedProviders": []}

    hashes = []
    for r in results:
        h = _info_hash_for_result(r)
        if h:
            hashes.append(h)

    if not hashes:
        logger.info(
            "Debrid instant cache: skipped — no info hashes on %s indexer results (magnet/hash missing)",
            len(results),
        )
        return [_blank(r) for r in results]

    if not debrid.available_providers():
        logger.warning("Debrid instant cache: no provider tokens set — Instant badges disabled")
        return [_blank(r) for r in results]

    cached = await debrid.check_cached_all(hashes)

    annotated = []
    for r in results:
        h = _info_hash_for_result(r)
        rd_hit = bool(h and h in cached.get(debrid.RD, set()))
        tb_hit = bool(h and h in cached.get(debrid.TORBOX, set()))
        providers = [p for p, hit in ((debrid.RD, rd_hit), (debrid.TORBOX, tb_hit)) if hit]
        annotated.append({
            **r,
            "rdCached": rd_hit,
            "torboxCached": tb_hit,
            "cachedProviders": providers,
        })

    n_cached = sum(1 for x in annotated if x.get("cachedProviders"))
    logger.info(
        "Debrid instant cache: %s of %s indexer torrents cached (%s hashes checked; rd=%s torbox=%s)",
        n_cached,
        len(annotated),
        len(hashes),
        sum(1 for x in annotated if x.get("rdCached")),
        sum(1 for x in annotated if x.get("torboxCached")),
    )
    return annotated


async def _run_indexer_discovery(
    title: str | None,
    author: str | None,
    subtitle: str | None,
    series_name: str | None,
    series_index: str | None,
) -> tuple[dict, object]:
    """Prowlarr search with series-aware queries; ranks full pool, returns curated list."""
    if not title:
        empty = {
            "results": [],
            "count": 0,
            "totalFetched": 0,
            "totalRanked": 0,
            "hiddenCount": 0,
            "matchCounts": {"exact": 0, "likely": 0, "weak": 0},
        }
        return empty, None

    ctx = resolve_book_search_context(
        title=title,
        subtitle=subtitle or "",
        author=author or "",
        series_name=series_name,
        series_index=series_index,
    )
    abb_queries = build_audiobookbay_queries(ctx)
    use_all_indexers = settings.prowlarr_all_indexers_for_books
    general_queries = build_prowlarr_queries(ctx) if use_all_indexers else []

    logger.info(
        "Indexer discovery for %r (book %s): mode=%s abb=%s",
        ctx.base_title,
        ctx.target_index,
        "all-indexers" if use_all_indexers else "abb-knaben-trusted",
        abb_queries,
    )

    trusted_results: list = []
    general_results: list = []

    if abb_queries:
        try:
            trusted_results = await prowlarr.search_trusted_indexers_multi(abb_queries)
        except Exception as e:
            logger.warning("ABB/trusted indexer search failed: %s", e)

    if use_all_indexers and general_queries:
        try:
            if len(general_queries) > 1:
                general_results = await prowlarr.search_multi(general_queries)
            else:
                general_results = await prowlarr.search(general_queries[0])
        except Exception as e:
            logger.warning("All-indexer Prowlarr search failed: %s", e)

    logger.info(
        "Indexer discovery raw: %s trusted/abb + %s all-indexers",
        len(trusted_results),
        len(general_results),
    )

    indexer_results = merge_indexer_results(trusted_results, general_results)
    if not indexer_results and abb_queries:
        logger.info("ABB/trusted empty — retry single query %r", abb_queries[0])
        indexer_results = await prowlarr.search_trusted_indexers_multi([abb_queries[0]])

    payload = build_search_result_payload(indexer_results, ctx, settings.search_results_max_return)
    return payload, ctx


@router.get("")
async def search_audiobooks(
    q: str = Query(None, min_length=2, description="Search query"),
    title: str = Query(None, description="Book title for structured search"),
    author: str = Query(None, description="Book author for structured search"),
    subtitle: str = Query(None, description="Book subtitle"),
    series_name: str = Query(None, description="Series name when known"),
    series_index: str = Query(None, description="Book number in series (e.g. 1)"),
    exclude_aa: bool = Query(False, description="Exclude Anna's Archive (for progressive loading)"),
    live: bool = Query(False, description="Live Prowlarr search; false reads from indexer cache"),
    _user: User = Depends(get_current_user),
):
    # Cache badges should reflect the requesting user's debrid accounts
    from app.services import debrid_tokens
    await debrid_tokens.apply_tokens_for_user_id(_user.id)

    discovery_ctx = None
    if title:
        library_query = title
        if subtitle:
            library_query = f"{title} {subtitle}".strip()
    elif q:
        library_query = q
    else:
        return {"results": [], "count": 0}

    if exclude_aa:
        # Torrent/indexer search: Prowlarr dominates latency (queries every indexer). Do not block on library lookups.
        t0 = time.perf_counter()
        try:
            if title:
                ctx = resolve_book_search_context(
                    title=title,
                    subtitle=subtitle or "",
                    author=author or "",
                    series_name=series_name,
                    series_index=series_index,
                )
                if live:
                    payload, discovery_ctx = await _run_indexer_discovery(
                        title, author, subtitle, series_name, series_index
                    )
                    await indexer_cache.upsert_torrents(payload.get("results", []))
                    indexer_results = payload["results"]
                else:
                    discovery_ctx = ctx
                    indexer_results = await indexer_cache.get_torrents_for_book(ctx)
                    payload = {
                        "totalFetched": len(indexer_results),
                        "hiddenCount": 0,
                        "matchCounts": {},
                    }
            else:
                ctx = resolve_book_search_context(title=q)
                raw = await prowlarr.search_trusted_indexers_multi([q])
                if settings.prowlarr_all_indexers_for_books:
                    raw = merge_indexer_results(
                        raw,
                        await prowlarr.search(q),
                    )
                relevant, dropped = filter_irrelevant_results(raw, ctx)
                payload = build_search_result_payload(relevant, ctx, settings.search_results_max_return)
                payload["totalFetched"] = len(raw)
                if dropped:
                    payload["hiddenCount"] = payload.get("hiddenCount", 0) + dropped
                discovery_ctx = ctx
                indexer_results = payload["results"]
        except Exception as e:
            logger.warning("Prowlarr search failed: %s", e)
            raise
        abs_items, kavita_items = await _enrich_with_library_timeout(library_query)
        results = _enrich_results(indexer_results, abs_items, kavita_items, "prowlarr")
        results = await _annotate_rd_cached(results)
        if discovery_ctx:
            results = order_results_for_display(results, discovery_ctx)
        logger.info(
            "Indexer search returned %s shown (%s fetched) in %.1fs",
            len(results),
            payload.get("totalFetched", len(results)),
            time.perf_counter() - t0,
        )
        return {
            "results": results,
            "count": len(results),
            "totalFetched": payload.get("totalFetched", len(results)),
            "hiddenCount": payload.get("hiddenCount", 0),
            "matchCounts": payload.get("matchCounts", {}),
        }

    discovery_ctx = None
    if title:
        indexer_coro = _run_indexer_discovery(title, author, subtitle, series_name, series_index)
    else:
        indexer_coro = prowlarr.search(q)

    tasks = [
        indexer_coro,
        audiobookshelf.search_library(library_query),
        kavita.search_library(library_query),
        annas_archive.search(library_query),
    ]
    gathered = await asyncio.gather(*tasks, return_exceptions=True)
    source_names = ["prowlarr", "audiobookshelf", "kavita", "annas_archive"]
    for i, g in enumerate(gathered):
        if isinstance(g, Exception):
            logger.warning("Search source %s failed: %s", source_names[i] if i < len(source_names) else i, g)
    raw_indexer = gathered[0] if not isinstance(gathered[0], Exception) else []
    search_payload: dict = {}
    if title and not isinstance(gathered[0], Exception):
        search_payload, discovery_ctx = raw_indexer
        indexer_results = search_payload["results"]
    else:
        indexer_results = raw_indexer
        search_payload = {
            "totalFetched": len(indexer_results),
            "hiddenCount": 0,
            "matchCounts": {},
        }
    abs_items = gathered[1] if not isinstance(gathered[1], Exception) else []
    kavita_items = gathered[2] if not isinstance(gathered[2], Exception) else []
    aa_results = gathered[3] if len(gathered) > 3 and not isinstance(gathered[3], Exception) else []

    results = _enrich_results(indexer_results, abs_items, kavita_items, "prowlarr")
    results = await _annotate_rd_cached(results)
    if discovery_ctx:
        results = order_results_for_display(results, discovery_ctx)
    aa_enriched = _enrich_results(aa_results, abs_items, kavita_items, "annas_archive")
    if title and discovery_ctx:
        aa_payload = build_search_result_payload(aa_enriched, discovery_ctx, settings.search_results_max_return)
        aa_enriched = aa_payload["results"]
    results.extend(aa_enriched)

    return {
        "results": results,
        "count": len(results),
        "totalFetched": search_payload.get("totalFetched", len(results)),
        "hiddenCount": search_payload.get("hiddenCount", 0),
        "matchCounts": search_payload.get("matchCounts", {}),
    }


@router.get("/annas-archive")
async def search_annas_archive(
    q: str = Query(None, min_length=2, description="Search query"),
    title: str = Query(None, description="Book title for structured search"),
    author: str = Query(None, description="Book author for structured search"),
    subtitle: str = Query(None, description="Book subtitle"),
    series_name: str = Query(None, description="Series name when known"),
    series_index: str = Query(None, description="Book number in series"),
    _user: User = Depends(get_current_user),
):
    """Search Anna's Archive only. Used for progressive loading alongside indexers."""
    if title:
        ctx = resolve_book_search_context(
            title=title,
            subtitle=subtitle or "",
            author=author or "",
            series_name=series_name,
            series_index=series_index,
        )
        aa_query = build_annas_archive_query(ctx)
        library_query = title
    elif q:
        aa_query = q
        library_query = q
    else:
        return {"results": [], "count": 0}

    gathered = await asyncio.gather(
        annas_archive.search(aa_query),
        _enrich_with_library_timeout(library_query),
        return_exceptions=True,
    )
    aa_results = gathered[0] if not isinstance(gathered[0], Exception) else []
    if isinstance(gathered[0], Exception):
        logger.warning("Anna's Archive search failed: %s", gathered[0])
    if isinstance(gathered[1], Exception):
        abs_items, kavita_items = [], []
    else:
        abs_items, kavita_items = gathered[1]
    results = _enrich_results(aa_results, abs_items, kavita_items, "annas_archive")
    ctx = resolve_book_search_context(
        title=title or q or "",
        subtitle=subtitle or "",
        author=author or "",
        series_name=series_name,
        series_index=series_index,
    )
    payload = build_search_result_payload(results, ctx, settings.search_results_max_return)
    return payload
