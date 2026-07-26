"""Parse display title + author from indexer release names (ABB, Knaben, etc.).

AudioBookBay listings are almost always ``Title - Author`` (sometimes with an
extra narrator / year / format segment). Knaben and scene packs sometimes use
``Author - Title``. Never decide by raw string length alone — short titles like
``Dune`` were wrongly swapped with longer author names.
"""

from __future__ import annotations

import re

_NOISE_PART_RE = re.compile(
    r"^(?:"
    r"\d{4}|"
    r"unabridged|abridged|retail|"
    r"audiobook|audio\s*book|ebook|"
    r"mp3|m4b|m4a|flac|aac|ogg|vorbis|"
    r"epub|pdf|mobi|azw3?|"
    r"\d+\s*kbps|"
    r"64\s*k|128\s*k|192\s*k|256\s*k|320\s*k|"
    r"complete\s+series|full\s+series|box\s*set|boxset|"
    r"graphic\s*audio|dramatized"
    r")$",
    re.I,
)

_NARRATOR_PART_RE = re.compile(
    r"^(?:narrated\s+by|read\s+by|narrator[:\s]+)\s*(.+)$",
    re.I,
)

_SERIES_MARK_RE = re.compile(
    r"(?:#\s*\d|\bbooks?\s+\d|\bvol(?:ume)?\.?\s*\d|\bpart\s+\d|\bepisode\s+\d)",
    re.I,
)

_BY_RE = re.compile(
    r"^(?P<title>.+?)\s+by\s+(?P<author>.+?)(?:\s*[\(\[]|$)",
    re.I,
)

_ARTICLE_RE = re.compile(r"^(the|a|an)\b", re.I)

_RELEASE_GROUP_RE = re.compile(r"\s*-\s*[A-Za-z0-9]{2,}\s*$")


def _clean_brackets(text: str) -> str:
    text = re.sub(r"\[.*?\]|\(.*?\)|\{.*?\}", " ", text or "")
    return re.sub(r"\s+", " ", text).strip(" -,.")


def _is_noise_part(part: str) -> bool:
    p = (part or "").strip()
    if not p:
        return True
    if _NOISE_PART_RE.match(p):
        return True
    if _NARRATOR_PART_RE.match(p):
        return True
    return False


def _word_count(part: str) -> int:
    return len([w for w in re.split(r"\s+", part.strip()) if w])


def looks_like_book_title(part: str) -> bool:
    """True when a dash segment looks more like a book title than a person."""
    p = (part or "").strip()
    if not p:
        return False
    if _SERIES_MARK_RE.search(p):
        return True
    if _ARTICLE_RE.match(p) and _word_count(p) >= 2:
        return True
    # Internal articles are a strong title signal ("Mistborn The Final Empire").
    if re.search(r"\b(?:the|a|an)\b", p, re.I) and _word_count(p) >= 3:
        return True
    lower_words = {w.lower().strip(".,'") for w in re.split(r"\s+", p) if w}
    titleish = {
        "chronicles", "trilogy", "saga", "omnibus", "collection", "anthology",
        "edition", "volume", "novel", "story", "stories", "tales", "empire",
        "wars", "game", "throne", "tower", "song", "house", "court",
    }
    if lower_words & titleish and _word_count(p) >= 2:
        return True
    return False


def looks_like_person_name(part: str) -> bool:
    """True when a dash segment looks more like an author than a book title."""
    p = (part or "").strip()
    if not p or len(p) < 2:
        return False
    if _is_noise_part(p):
        return False
    if looks_like_book_title(p):
        return False

    # "Last, First" / "Last, First Middle"
    if "," in p:
        bits = [b.strip() for b in p.split(",") if b.strip()]
        if 1 <= len(bits) <= 3 and all(_word_count(b) <= 3 for b in bits):
            return True

    # Multi-author joiners
    for sep in (" & ", " and ", " / ", "; ", " + "):
        if sep in p:
            chunks = [c.strip() for c in p.split(sep) if c.strip()]
            if 2 <= len(chunks) <= 4 and all(looks_like_person_name(c) for c in chunks):
                return True

    words = [w for w in re.split(r"\s+", p) if w]
    if not words or len(words) > 5:
        return False

    # Prefer Capitalized / ALLCAPS name tokens (allow initials like "J.K.")
    name_like = 0
    for w in words:
        core = w.strip(".,'")
        if not core:
            continue
        if re.fullmatch(r"[A-Z](?:\.[A-Z])*\.?", core):
            name_like += 1
            continue
        if core[:1].isupper() and core[1:].islower():
            name_like += 1
            continue
        if core.isupper() and 1 < len(core) <= 12:
            name_like += 1
            continue
    return name_like >= max(1, len(words) - 1)


def _is_abb_indexer(indexer: str | None) -> bool:
    compact = re.sub(r"[\s_\-]+", "", (indexer or "").lower())
    return "audiobookbay" in compact or compact in {"abb", "abbays"}


def split_release_title_author(
    raw_title: str,
    *,
    indexer: str | None = None,
) -> tuple[str, str]:
    """Return ``(display_title, author)`` for UI cards and download requests.

    Prefers AudioBookBay's ``Title - Author`` ordering. Uses person-name
    heuristics (not string length) when the indexer is ambiguous.
    """
    raw = (raw_title or "").strip()
    if not raw:
        return "", ""

    cleaned = _clean_brackets(raw)
    cleaned = _RELEASE_GROUP_RE.sub("", cleaned).strip(" -,.") or cleaned
    if not cleaned:
        return raw[:200], ""

    # Prefer dash parsing when present — "Narrated by X" must not trigger
    # the "Title by Author" pattern.
    if " - " not in cleaned:
        by_match = _BY_RE.match(cleaned)
        if by_match:
            title = by_match.group("title").strip(" -,.")
            author = by_match.group("author").strip(" -,.")
            author = re.split(
                r"\s+(?:narrated|read)\s+by\s+", author, maxsplit=1, flags=re.I
            )[0]
            return (title or cleaned)[:200], author[:200]
        return cleaned[:200], ""

    parts = [p.strip(" -") for p in cleaned.split(" - ") if p.strip(" -")]
    parts = [p for p in parts if not _is_noise_part(p)]
    if not parts:
        return cleaned[:200], ""
    if len(parts) == 1:
        return parts[0][:200], ""

    # ABB often appends a bare narrator after the author:
    # "Title - Author - Narrator". Prefer the earlier person-name segment.
    if len(parts) >= 3 and looks_like_person_name(parts[-1]) and looks_like_person_name(parts[-2]):
        author = parts[-2]
        title = " - ".join(parts[:-2]).strip()
        return (title or cleaned)[:200], author[:200]

    first, last = parts[0], parts[-1]
    first_is_author = looks_like_person_name(first)
    last_is_author = looks_like_person_name(last)
    abb = _is_abb_indexer(indexer)

    # Default ABB / most audiobook releases: Title [- Series] - Author
    if abb or (last_is_author and not first_is_author):
        author = last
        title = " - ".join(parts[:-1]).strip()
        return (title or cleaned)[:200], author[:200]

    # Knaben / scene: Author - Title
    if first_is_author and not last_is_author:
        author = first
        title = " - ".join(parts[1:]).strip()
        return (title or cleaned)[:200], author[:200]

    # Both or neither look like authors — prefer Title - Author (ABB convention)
    author = last
    title = " - ".join(parts[:-1]).strip()
    return (title or cleaned)[:200], author[:200]


def parse_torrent_name_parts(title: str, *, indexer: str | None = None) -> tuple[str, str]:
    """Return ``(author, book_title)`` for folder organization (legacy order)."""
    display_title, author = split_release_title_author(title, indexer=indexer)
    if not display_title and not author:
        return "Unknown Author", "Unknown"
    if not author:
        return "Unknown Author", display_title or "Unknown"
    return author, display_title or "Unknown"
