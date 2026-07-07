"""Build indexer queries and rank torrent results for a specific book in a series."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from app.utils.book_series import (
    book_one_shares_series_title,
    detect_series_from_title,
    extract_book_numbers_from_text,
    format_index_for_query,
    looks_like_later_series_volume,
)

logger = logging.getLogger(__name__)

_COMPLETE_RE = re.compile(
    r"\b(?:complete|full)\s+(?:series|collection|set)\b|"
    r"\bbooks?\s+\d+\s*[-–—to]+\s*\d+\b|"
    r"\ball\s+\d+\s+books?\b",
    re.IGNORECASE,
)

# Words too common to prove a torrent matches the book on their own
_STOPWORDS = frozenset({
    "the", "a", "an", "of", "and", "or", "in", "on", "to", "for", "at", "by",
    "with", "from", "as", "is", "it", "be", "are", "was", "were", "been",
    "book", "vol", "volume", "part", "pt", "series", "audiobook", "unabridged",
    "m4b", "mp3", "flac", "aac", "epub", "pdf",
})


@dataclass
class BookSearchContext:
    title: str
    subtitle: str
    author: str
    series_name: str | None
    target_index: str | None
    base_title: str
    display_title: str

    @property
    def has_series_position(self) -> bool:
        return bool(self.target_index)


def resolve_book_search_context(
    title: str,
    subtitle: str = "",
    author: str = "",
    series_name: str | None = None,
    series_index: str | None = None,
) -> BookSearchContext:
    title = (title or "").strip()
    subtitle = (subtitle or "").strip()
    author = (author or "").strip()
    full = f"{title}: {subtitle}" if subtitle else title

    detected = detect_series_from_title(full) or detect_series_from_title(title)
    if subtitle and not detected:
        detected = detect_series_from_title(subtitle)

    idx = (series_index or "").strip() or None
    base = title

    if series_name:
        base = series_name.strip()
        if not idx and detected and detected[0].lower() in series_name.lower():
            idx = detected[1]
    elif detected:
        base = detected[0]
        if not idx:
            idx = detected[1]

    # Standalone numbered title e.g. "Dungeon Crawler Carl 1" without series metadata
    if not idx and detected and detected[0].lower() == title.lower():
        idx = detected[1]

    display = full if subtitle else title
    if idx and base.lower() not in display.lower():
        display = f"{base} — Book {format_index_for_query(idx)}"

    return BookSearchContext(
        title=title,
        subtitle=subtitle,
        author=author,
        series_name=series_name or (detected[0] if detected else None),
        target_index=idx,
        base_title=base,
        display_title=display,
    )


def build_prowlarr_queries(ctx: BookSearchContext, max_queries: int = 6) -> list[str]:
    """Targeted Prowlarr queries; book number disambiguates series entries."""
    author_bit = ""
    if ctx.author:
        first_author = ctx.author.split(",")[0].strip()
        if first_author:
            author_bit = f" {first_author}"

    queries: list[str] = []

    if ctx.target_index:
        n = format_index_for_query(ctx.target_index)
        queries.append(f'"{ctx.base_title}" book {n}{author_bit}')
        queries.append(f'"{ctx.base_title}" #{n}{author_bit}')
        queries.append(f"{ctx.base_title} book {n}{author_bit}")
        queries.append(f"{ctx.base_title} {n}{author_bit}")
        if ctx.subtitle and ctx.subtitle.lower() not in ctx.base_title.lower():
            queries.append(f'"{ctx.title}" "{ctx.subtitle}"{author_bit}')
            queries.append(f"{ctx.title} {ctx.subtitle}{author_bit}")
    else:
        queries.append(f'"{ctx.base_title}"{author_bit}')
        queries.append(f"{ctx.base_title}{author_bit}")
        if ctx.subtitle:
            queries.append(f'"{ctx.title}" "{ctx.subtitle}"{author_bit}')
            queries.append(f"{ctx.title} {ctx.subtitle}{author_bit}")

    # Broader fallback so we still get results if numbered queries miss
    queries.append(f"{ctx.base_title}{author_bit}")

    seen: set[str] = set()
    unique: list[str] = []
    for q in queries:
        key = q.lower()
        if key not in seen:
            seen.add(key)
            unique.append(q)
    return unique[:max_queries]


def build_audiobookbay_queries(ctx: BookSearchContext, max_queries: int = 6) -> list[str]:
    """Unquoted queries — match how ABB's site search works (no Prowlarr quote syntax)."""
    queries: list[str] = []
    base = ctx.base_title
    author = ctx.author.split(",")[0].strip() if ctx.author else ""

    # Volume-specific title (e.g. "A Parade of Horribles") — how users search on ABB for book 8
    if ctx.title and ctx.title.lower().strip() != base.lower().strip():
        queries.append(ctx.title)
        if ctx.target_index:
            n = format_index_for_query(ctx.target_index)
            queries.append(f"{ctx.title} book {n}")

    queries.append(base)

    if ctx.target_index:
        n = format_index_for_query(ctx.target_index)
        queries.append(f"{base} book {n}")
        queries.append(f"{base} - book {n}")
        queries.append(f"{base} {n}")
    if author:
        queries.append(f"{base} {author}")

    seen: set[str] = set()
    unique: list[str] = []
    for q in queries:
        key = q.lower()
        if key not in seen:
            seen.add(key)
            unique.append(q)
    return unique[:max_queries]


def build_annas_archive_query(ctx: BookSearchContext) -> str:
    if ctx.target_index:
        n = format_index_for_query(ctx.target_index)
        q = f"{ctx.base_title} book {n}"
    else:
        q = ctx.base_title
    if ctx.author:
        q += f" {ctx.author.split(',')[0].strip()}"
    return q


def _tokenize(text: str) -> set[str]:
    return {
        w
        for w in re.sub(r"[^a-z0-9\s]", " ", (text or "").lower()).split()
        if len(w) > 2 and w not in _STOPWORDS
    }


def _token_overlap(a: str, b: str) -> float:
    ta, tb = _tokenize(a), _tokenize(b)
    if not ta:
        return 0.0
    return len(ta & tb) / len(ta)


_BRACKET_RE = re.compile(r"[\[\(][^\]\)]*[\]\)]")
_RELEASE_SEG_RE = re.compile(r"\s*[-–—:|/]\s+|\s+[-–—:|/]\s*")


def _clean_for_fuzzy(text: str) -> str:
    text = _BRACKET_RE.sub(" ", text or "")
    text = re.sub(r"[^a-z0-9\s]", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def _fuzzy_title_score(needle: str, release_title: str) -> float:
    """Stremio-style fuzzy match: compare the book title against each dash/colon
    separated segment of the release name, so junk (author, bitrate, year tags)
    in other segments doesn't drag the score down."""
    n = _clean_for_fuzzy(needle)
    if not n:
        return 0.0
    whole = _clean_for_fuzzy(release_title)
    if not whole:
        return 0.0
    if n in whole:
        return 1.0
    best = SequenceMatcher(None, n, whole).ratio()
    for seg in _RELEASE_SEG_RE.split(release_title):
        s = _clean_for_fuzzy(seg)
        if len(s) < 3:
            continue
        best = max(best, SequenceMatcher(None, n, s).ratio())
    return best


def _later_volume_in_series(rt: str, ctx: BookSearchContext) -> bool:
    return looks_like_later_series_volume(
        rt,
        target_index=ctx.target_index,
        series_name=ctx.series_name,
        base_title=ctx.base_title,
        volume_title=ctx.title,
    )


def _book_one_series_title(ctx: BookSearchContext) -> bool:
    return book_one_shares_series_title(
        title=ctx.title,
        series_name=ctx.series_name,
        base_title=ctx.base_title,
        target_index=ctx.target_index,
    )


def _significant_tokens(*texts: str) -> set[str]:
    out: set[str] = set()
    for text in texts:
        if text:
            out |= _tokenize(text)
    return out


def is_relevant_torrent(result_title: str, ctx: BookSearchContext) -> bool:
    """Drop indexer noise with no meaningful overlap to the book/series we searched for."""
    rt = (result_title or "").strip()
    if not rt:
        return False

    if _later_volume_in_series(rt, ctx):
        return False

    lower = rt.lower()
    book_tokens = _significant_tokens(ctx.title, ctx.subtitle)
    series_tokens = _significant_tokens(ctx.base_title)
    title_is_series_only = (
        not book_tokens
        or book_tokens == series_tokens
        or (book_tokens and book_tokens <= series_tokens)
    )
    book_hits = sum(1 for t in book_tokens if t in lower)
    series_hits = sum(1 for t in series_tokens if t in lower)

    relevance = _title_relevance(rt, ctx)
    if relevance >= 0.5:
        return True
    if relevance >= 0.35 and (title_is_series_only or book_hits >= 1):
        return True

    if book_tokens and not title_is_series_only:
        # Volume has its own title (e.g. "Parade of Horribles") — require those words, not just "Carl".
        if book_hits < 1:
            return False
        if book_hits >= 2:
            return True
        if book_hits == 1 and any(len(t) >= 5 and t in lower for t in book_tokens):
            return True
        if book_hits >= 1 and series_hits >= 2:
            if ctx.target_index:
                try:
                    target = float(ctx.target_index)
                    found = extract_book_numbers_from_text(rt)
                    if found and target not in found:
                        return False
                except ValueError:
                    pass
            return True
        return False

    if series_tokens and title_is_series_only:
        if ctx.target_index:
            try:
                target = float(ctx.target_index)
                found = extract_book_numbers_from_text(rt)
                if found and target not in found:
                    return False
            except ValueError:
                pass
        need = 2 if len(series_tokens) >= 3 else 1
        if series_hits >= need:
            return True

    if ctx.author:
        author_tokens = [w for w in ctx.author.lower().split() if len(w) > 2]
        if author_tokens and all(w in lower for w in author_tokens[:1]):
            if series_hits >= 1 or relevance >= 0.2:
                return True

    # No author confirmation and no meaningful title/series overlap → noise.
    return relevance >= 0.34


def filter_irrelevant_results(
    results: list[dict],
    ctx: BookSearchContext,
) -> tuple[list[dict], int]:
    kept: list[dict] = []
    dropped = 0
    for r in results:
        # AA results are already scoped by the AA search query; don't apply torrent heuristics.
        if r.get("source") == "annas_archive":
            kept.append(r)
            continue
        if is_relevant_torrent(r.get("title", ""), ctx):
            kept.append(r)
        else:
            dropped += 1
    if dropped:
        logger.info(
            "Dropped %s irrelevant torrent(s) for %r (book %s)",
            dropped,
            ctx.display_title,
            ctx.target_index,
        )
    return kept, dropped


def _title_relevance(result_title: str, ctx: BookSearchContext) -> float:
    """Overlap with book title first; series-only overlap is down-weighted when the volume has its own title."""
    book_tokens = _significant_tokens(ctx.title, ctx.subtitle)
    series_tokens = _significant_tokens(ctx.base_title)
    distinct_book_title = bool(
        book_tokens
        and book_tokens != series_tokens
        and not book_tokens <= series_tokens
    )

    scores: list[float] = []
    if ctx.title:
        scores.append(_token_overlap(ctx.title, result_title))
        # Fuzzy segment match catches punctuation/apostrophe/ordering variants
        scores.append(_fuzzy_title_score(ctx.title, result_title) * 0.95)
    if ctx.subtitle:
        scores.append(_token_overlap(ctx.subtitle, result_title))
    series_rel = max(
        _token_overlap(ctx.base_title, result_title),
        _fuzzy_title_score(ctx.base_title, result_title) * 0.95,
    )
    scores.append(series_rel * 0.45 if distinct_book_title else series_rel)
    return max(scores) if scores else 0.0


def score_torrent_title(result_title: str, ctx: BookSearchContext) -> tuple[float, str]:
    """Return (score, tier) where tier is exact | likely | weak.

    Tiers are for sorting/filtering in the UI — not whether Prowlarr found the torrent.
    ABB posts often omit 'Book 1' and use '01' or years in parentheses; we avoid
    treating release years as the wrong volume.
    """
    rt = result_title or ""
    lower = rt.lower()
    relevance = _title_relevance(rt, ctx)
    score = relevance * 40.0

    if ctx.author:
        author_tokens = [w for w in ctx.author.lower().split() if len(w) > 2]
        if author_tokens and any(w in lower for w in author_tokens[:2]):
            score += 10.0

    found = extract_book_numbers_from_text(rt)

    if ctx.target_index is not None:
        try:
            target = float(ctx.target_index)
        except ValueError:
            target = None

        if target is not None:
            if _later_volume_in_series(rt, ctx):
                score -= 70.0
            elif target in found:
                score += 100.0
            elif not found:
                if target == 1.0 and _book_one_series_title(ctx):
                    score += 90.0
                elif relevance >= 0.55:
                    score += 35.0
                else:
                    score += 10.0
            else:
                others = {n for n in found if abs(n - target) >= 0.01}
                if others:
                    score -= 40.0
                    if target == 1.0 and min(others) >= 2:
                        score -= 25.0

            if _COMPLETE_RE.search(rt) and target <= 3:
                score -= 20.0
    else:
        if found:
            score -= 5.0

    # Strong title match without contradictory numbers → at least "likely"
    if relevance >= 0.72 and (not ctx.target_index or not found or float(ctx.target_index) in found):
        score = max(score, 55.0)

    if not is_relevant_torrent(rt, ctx):
        return -100.0, "weak"

    tier = "weak"
    if score >= 80:
        tier = "exact"
    elif score >= 38:
        tier = "likely"

    return score, tier


def _seeders_tiebreak_bonus(seeders: int) -> float:
    return min(max(seeders, 0) / 20.0, 8.0)


def is_mismatched_series_book(result: dict, ctx: BookSearchContext) -> bool:
    """True when torrent clearly refers to a different volume in the series."""
    title = result.get("title", "") or ""
    if _later_volume_in_series(title, ctx):
        return True
    if not ctx.target_index:
        return False
    try:
        target = float(ctx.target_index)
    except ValueError:
        return False

    found = extract_book_numbers_from_text(title)
    if not found:
        return False
    if target in found:
        return False
    return True


def merge_indexer_results(*batches: list[dict]) -> list[dict]:
    """Merge torrent lists by info hash; the best-seeded copy of a duplicate wins."""
    merged: dict[str, dict] = {}
    for batch in batches:
        for r in batch:
            key = (r.get("infoHash") or "").lower()
            if not key:
                key = r.get("magnetUrl") or r.get("downloadUrl") or f"{r.get('title')}|{r.get('indexer')}"
            prev = merged.get(key)
            if not prev or (r.get("seeders") or 0) > (prev.get("seeders") or 0):
                merged[key] = r
    return list(merged.values())


def rank_indexer_results(results: list[dict], ctx: BookSearchContext) -> list[dict]:
    scored: list[tuple[float, dict]] = []
    for r in results:
        if r.get("source") == "annas_archive":
            rel = _title_relevance(r.get("title", ""), ctx)
            row = dict(r)
            row["matchScore"] = round(max(45.0, rel * 50.0), 1)
            row["matchTier"] = "exact" if rel >= 0.45 else "likely"
            scored.append((row["matchScore"], row))
            continue
        s, tier = score_torrent_title(r.get("title", ""), ctx)
        s += _seeders_tiebreak_bonus(r.get("seeders") or 0)
        indexer = (r.get("indexer") or "").lower().replace(" ", "")
        if ("audiobook" in indexer and "bay" in indexer) or "audiobookbay" in indexer:
            s += 25.0
        if "knaben" in indexer:
            s += 15.0
        rt_lower = (r.get("title") or "").lower()
        if ctx.target_index and f"book {format_index_for_query(ctx.target_index)}" in rt_lower:
            s += 20.0
        row = dict(r)
        row["matchScore"] = round(s, 1)
        row["matchTier"] = tier
        scored.append((s, row))

    tier_order = {"exact": 0, "likely": 1, "weak": 2}

    def _sort_key(item: tuple[float, dict]) -> tuple:
        score, row = item
        tier = tier_order.get(row.get("matchTier"), 3)
        nums = extract_book_numbers_from_text(row.get("title", ""))
        vol_penalty = 0
        try:
            target = float(ctx.target_index) if ctx.target_index else None
        except ValueError:
            target = None
        if target == 1.0 and nums and 1.0 not in nums:
            vol_penalty = int(min(nums))
        elif target and target > 1 and nums and target not in nums:
            vol_penalty = 50
        cached_anywhere = bool(row.get("rdCached") or row.get("torboxCached"))
        return (tier, vol_penalty, -score, not cached_anywhere)

    scored.sort(key=_sort_key)
    return [row for _, row in scored]


def order_results_for_display(
    ranked: list[dict],
    ctx: BookSearchContext,
) -> list[dict]:
    """Group by match quality; push wrong book numbers to the bottom when we have good hits."""
    exact = [r for r in ranked if r.get("matchTier") == "exact"]
    likely = [r for r in ranked if r.get("matchTier") == "likely"]
    weak = [r for r in ranked if r.get("matchTier") == "weak"]

    if ctx.target_index and (exact or likely):
        weak_match = []
        weak_mismatch = []
        for r in weak:
            if is_mismatched_series_book(r, ctx):
                weak_mismatch.append(r)
            else:
                weak_match.append(r)
        weak = weak_match + weak_mismatch

    return exact + likely + weak


def build_search_result_payload(
    results: list[dict],
    ctx: BookSearchContext,
    max_return: int,
) -> dict:
    """Rank full pool, order for display, return metadata for UI."""
    from app.config import get_settings

    settings = get_settings()
    cap = max_return or settings.search_results_max_return

    relevant, dropped_irrelevant = filter_irrelevant_results(results, ctx)
    ranked = rank_indexer_results(relevant, ctx)
    ordered = order_results_for_display(ranked, ctx)
    returned = ordered[:cap]
    hidden = max(0, len(ordered) - len(returned)) + dropped_irrelevant

    exact_n = sum(1 for r in ordered if r.get("matchTier") == "exact")
    likely_n = sum(1 for r in ordered if r.get("matchTier") == "likely")
    weak_n = sum(1 for r in ordered if r.get("matchTier") == "weak")

    if ctx.target_index:
        logger.info(
            "Search pool %s → showing %s (exact=%s likely=%s weak=%s hidden=%s) for %r #%s",
            len(results),
            len(returned),
            exact_n,
            likely_n,
            weak_n,
            hidden,
            ctx.base_title,
            ctx.target_index,
        )

    return {
        "results": returned,
        "count": len(returned),
        "totalFetched": len(results),
        "totalRanked": len(ordered),
        "hiddenCount": hidden,
        "matchCounts": {
            "exact": exact_n,
            "likely": likely_n,
            "weak": weak_n,
        },
    }
