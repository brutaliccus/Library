"""Match Google Books / catalog titles to Kavita ebook series and chapters."""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

from app.services import kavita
from app.utils.book_series import (
    detect_series_from_title,
    extract_book_numbers_from_text,
    series_name_match,
)

_MIN_SCORE = 0.52


def _normalize(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9\s]", " ", (text or "").lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def _seq_ratio(a: str, b: str) -> float:
    na, nb = _normalize(a), _normalize(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def _author_matches(query_author: str, series: dict) -> bool:
    q = _normalize(query_author)
    if not q:
        return True
    authors = series.get("authors") or []
    for a in authors:
        name = (a.get("name") if isinstance(a, dict) else str(a)) or ""
        n = _normalize(name)
        if not n:
            continue
        if q in n or n in q:
            return True
        if _seq_ratio(q, n) >= 0.8:
            return True
    return False


def _book_number_from_text(*texts: str) -> float | None:
    for text in texts:
        if not text:
            continue
        detected = detect_series_from_title(text.strip())
        if detected:
            try:
                return float(detected[1])
            except ValueError:
                pass
        nums = extract_book_numbers_from_text(text)
        bookish = sorted(n for n in nums if 0 < n < 200)
        if bookish:
            return bookish[0]
    return None


def _volume_index(vol: dict) -> float | None:
    for key in ("minNumber", "number", "maxNumber"):
        val = vol.get(key)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                pass
    return _book_number_from_text(vol.get("name") or vol.get("title") or "")


def _base_title(name: str) -> str:
    detected = detect_series_from_title((name or "").strip())
    if detected:
        return detected[0]
    return (name or "").strip()


def _series_index_compatible(k_index: float | None, query_index: float | None) -> bool:
    """Reject clearly wrong books in a numbered series (e.g. open Book 2 when user wants Book 1)."""
    if query_index is None:
        return True
    if k_index is None:
        # Unnumbered Kavita entry — only assume Book 1 when the catalog asks for #1
        return query_index <= 1.0
    return abs(k_index - query_index) < 0.01


def _score_series(
    series_name: str,
    *,
    query_title: str,
    query_author: str,
    query_series_name: str | None,
    query_index: float | None,
) -> float:
    k_name = (series_name or "").strip()
    if not k_name:
        return 0.0

    k_index = _book_number_from_text(k_name)
    title_ratio = _seq_ratio(query_title, k_name)
    base_ratio = _seq_ratio(_base_title(query_title), _base_title(k_name))
    score = max(title_ratio, base_ratio * 0.95)

    if query_series_name:
        if series_name_match(query_series_name, _base_title(k_name)) or series_name_match(
            query_series_name, k_name
        ):
            score = max(score, 0.78)
        elif series_name_match(query_series_name, query_title):
            score = max(score, 0.7)
        else:
            score *= 0.5

    if query_index is not None and k_index is not None:
        if abs(query_index - k_index) < 0.01:
            score += 0.4
        else:
            return 0.0
    elif query_index is not None and k_index is None:
        if query_index <= 1.0:
            score += 0.12
        else:
            score -= 0.35

    qn, kn = _normalize(query_title), _normalize(k_name)
    if qn and kn and (qn in kn or kn in qn) and len(qn) >= 5:
        if query_index is not None and k_index is not None and abs(query_index - k_index) >= 0.01:
            return 0.0
        if query_index is not None and k_index is not None and abs(query_index - k_index) < 0.01:
            score = max(score, 0.85)
        elif query_index is not None and k_index is None and query_index <= 1.0:
            score = max(score, 0.72)
        else:
            score = max(score, 0.62)

    return min(score, 1.0)


def _pick_chapter_id(volumes: list[dict], target_index: float | None) -> int | None:
    if not volumes:
        return None

    candidates: list[tuple[float, int, float | None]] = []
    for vol in volumes:
        vol_num = _volume_index(vol)
        chapters = vol.get("chapters") or []
        if not chapters:
            continue
        ch_id = chapters[0].get("id")
        if ch_id is None:
            continue
        sort_key = vol_num if vol_num is not None else 999.0
        candidates.append((sort_key, int(ch_id), vol_num))

    if not candidates:
        return None

    if target_index is not None:
        for _, ch_id, vol_num in candidates:
            if vol_num is not None and abs(vol_num - target_index) < 0.01:
                return ch_id
        ordered = sorted(candidates, key=lambda x: x[0])
        idx = int(target_index) - 1
        if 0 <= idx < len(ordered):
            return ordered[idx][1]

    return sorted(candidates, key=lambda x: x[0])[0][1]


async def resolve_kavita_ebook(
    title: str,
    author: str = "",
    series_name: str | None = None,
    series_index: str | float | None = None,
) -> dict[str, Any] | None:
    """Find the best Kavita ebook for a catalog title. Returns chapterId, seriesId, title."""
    title = (title or "").strip()
    if not title:
        return None

    query_index: float | None = None
    if series_index is not None and str(series_index).strip() != "":
        try:
            query_index = float(series_index)
        except ValueError:
            query_index = _book_number_from_text(str(series_index))
    if query_index is None:
        query_index = _book_number_from_text(title, series_name or "")

    series_list = await kavita.get_all_series(formats=kavita.EBOOK_FORMATS)
    if not series_list:
        return None

    best: tuple[float, dict] | None = None
    for s in series_list:
        name = (s.get("name") or s.get("localizedName") or s.get("originalName") or "").strip()
        if not name:
            continue
        k_index = _book_number_from_text(name)
        if not _series_index_compatible(k_index, query_index):
            continue
        if author and not _author_matches(author, s):
            continue
        score = _score_series(
            name,
            query_title=title,
            query_author=author,
            query_series_name=series_name,
            query_index=query_index,
        )
        if score < _MIN_SCORE:
            continue
        if best is None or score > best[0]:
            best = (score, s)

    if not best:
        return None

    series = best[1]
    sid = series.get("id")
    name = (series.get("name") or series.get("localizedName") or series.get("originalName") or "").strip()
    if not sid:
        return None

    volumes = await kavita.get_series_volumes(sid)
    chapter_id = _pick_chapter_id(volumes, query_index)
    if chapter_id is None:
        return None

    return {
        "chapterId": chapter_id,
        "seriesId": sid,
        "title": name,
        "matchScore": round(best[0], 3),
    }
