"""Parse series names and book numbers from titles (Google Books, torrent names)."""

import re

SERIES_PATTERNS = [
    re.compile(r"^(.+?)\s*[\(#]\s*(\d+\.?\d*)\s*\)?$"),
    re.compile(r"^(.+?),?\s+Book\s+(\d+\.?\d*)$", re.IGNORECASE),
    re.compile(r"^(.+?),?\s+Vol\.?\s*(\d+\.?\d*)$", re.IGNORECASE),
    re.compile(r"^(.+?)\s+(\d+)$"),
]

# ABS / folder style: "Book 01 - Harry Potter and the Philosopher's Stone"
_BOOK_NN_PREFIX = re.compile(
    r"^(?:Book|Bk|Vol\.?|Volume)\s*0*(\d+(?:\.\d+)?)\s*[-–:—]\s*(.+)$",
    re.IGNORECASE,
)
_AND_THE = re.compile(r"^(.+?)\s+and\s+the\s+.+$", re.IGNORECASE)
_PAREN_SERIES = re.compile(
    r"[\(\[]\s*(?P<series>[^)\]]+?)\s*(?:,\s*(?:Book|Vol\.?|#)?\s*\d+(?:\.\d+)?)?\s*[\)\]]\s*$",
    re.IGNORECASE,
)

# Media-format labels that must never appear as series/genre shelves
JUNK_LIBRARY_LABELS = frozenset({
    "audiobook", "audiobooks", "ebook", "ebooks", "unabridged", "abridged",
    "mp3", "m4b", "m4a", "epub", "pdf", "retail", "graphicaudio",
    "fiction", "literature", "general", "adult", "young adult", "ya",
})


def is_junk_library_label(name: str | None) -> bool:
    n = (name or "").strip().lower()
    return not n or n in JUNK_LIBRARY_LABELS


# ABS / Amazon often stuff ASINs, "Kindle Edition", product noise into series fields.
_ASIN_RE = re.compile(r"\bB0[A-Z0-9]{8,}\b", re.IGNORECASE)
_AMAZON_SERIES_JUNK = re.compile(
    r"(?:amazon\.com|kindle\s*edition|audible\.com|asin\b|"
    r"^book\s*#?\s*\d+$|^\d{6,}$)",
    re.IGNORECASE,
)


def is_junk_series_hint(name: str | None) -> bool:
    """True for labels that must never be passed to Hardcover as a series hint."""
    n = (name or "").strip()
    if not n or is_junk_library_label(n):
        return True
    if _ASIN_RE.search(n) or _AMAZON_SERIES_JUNK.search(n):
        return True
    # Folder-style book titles mistakenly stored as series ("Book 01 - …")
    if _BOOK_NN_PREFIX.match(n):
        return True
    # Pure punctuation / digits
    if re.fullmatch(r"[\d\W_]+", n):
        return True
    return False


# ABS Folder Forge / quick-match often puts "Series Name #1" in metadata.seriesName
# while leaving metadata.series null.
_ABS_SERIES_HASH = re.compile(
    r"^(?P<name>.+?)\s*#\s*(?P<seq>\d+(?:\.\d+)?)\s*$",
)
_ABS_SERIES_BOOK = re.compile(
    r"^(?P<name>.+?)\s*,?\s+Book\s+(?P<seq>\d+(?:\.\d+)?)\s*$",
    re.IGNORECASE,
)


def parse_abs_series_label(raw: str | None) -> tuple[str, str]:
    """Parse ABS ``metadata.seriesName`` into (series_name, sequence).

    Handles Folder Forge / Audible-style labels like ``Dungeon Crawler Carl #1``
    and plain names like ``Practical Magic``. Returns ``("", "")`` for junk.
    """
    n = (raw or "").strip()
    if not n:
        return "", ""
    for pat in (_ABS_SERIES_HASH, _ABS_SERIES_BOOK):
        m = pat.match(n)
        if m:
            name = m.group("name").strip().rstrip(",:-")
            seq = m.group("seq").strip()
            if name and not is_junk_series_hint(name):
                return name, seq
            return "", ""
    if is_junk_series_hint(n):
        return "", ""
    return n, ""


def library_series_from_title(title: str) -> tuple[str, str] | None:
    """Infer (series_name, sequence) for local library shelves.

    Prefer store-style title cues over ABS folder junk. Handles:
    - ``Book 01 - Harry Potter and the Philosopher's Stone`` → Harry Potter / 1
    - ``Phoenix and Ashes (Elemental Masters, Book 3)`` → Elemental Masters / 3
    - standard ``Series Name Book 2`` patterns via ``detect_series_from_title``
    """
    t = (title or "").strip()
    if not t:
        return None

    m = _BOOK_NN_PREFIX.match(t)
    if m:
        seq, rest = m.group(1), m.group(2).strip()
        nested = detect_series_from_title(rest)
        if nested and not is_junk_library_label(nested[0]):
            return nested[0], seq
        paren = _PAREN_SERIES.search(rest)
        if paren:
            series = re.sub(
                r",?\s*(?:Book|Vol\.?)\s*\d+(?:\.\d+)?\s*$",
                "",
                paren.group("series"),
                flags=re.IGNORECASE,
            ).strip()
            if series and not is_junk_library_label(series):
                return series, seq
        and_the = _AND_THE.match(rest)
        if and_the:
            series = and_the.group(1).strip()
            if series and not is_junk_library_label(series):
                return series, seq
        if ":" in rest:
            series = rest.split(":", 1)[0].strip()
            if series and not is_junk_library_label(series):
                return series, seq
        return None

    paren = _PAREN_SERIES.search(t)
    if paren:
        series = re.sub(
            r",?\s*(?:Book|Vol\.?)\s*\d+(?:\.\d+)?\s*$",
            "",
            paren.group("series"),
            flags=re.IGNORECASE,
        ).strip()
        seq_m = re.search(r"(?:Book|Vol\.?|#)\s*(\d+(?:\.\d+)?)", paren.group(0), re.I)
        seq = seq_m.group(1) if seq_m else ""
        if series and not is_junk_library_label(series):
            return series, seq

    detected = detect_series_from_title(t)
    if detected and not is_junk_library_label(detected[0]):
        return detected
    return None


TORRENT_BOOK_NUM_PATTERNS = [
    re.compile(r"\b(?:book|bk)\s*[#.]?\s*0*(\d+(?:\.\d+)?)\b", re.IGNORECASE),
    re.compile(r"#[\s]*0*(\d+(?:\.\d+)?)\b"),
    re.compile(r"\b(\d+)(?:st|nd|rd|th)\s+(?:book|volume|part)\b", re.IGNORECASE),
    re.compile(r"\bvol\.?\s*0*(\d+(?:\.\d+)?)\b", re.IGNORECASE),
    re.compile(r"\b(?:part|pt)\.?\s*0*(\d+(?:\.\d+)?)\b", re.IGNORECASE),
    # ABB-style: "Series Name 01" or "- 01 -" between dashes (not 4-digit years)
    re.compile(r"[-–—]\s*0*(\d{1,2})\s*[-–—]"),
    re.compile(r"\b0*(\d{1,2})(?=\s*[-–—])"),
    # Trailing volume: "Series Name - 03"
    re.compile(r"[-–—]\s*0*(\d{1,2})\s*$"),
    # Bracketed volume only when 1–2 digits (skip [2021], [M4B])
    re.compile(r"[\[\(]\s*0*(\d{1,2})\s*[\]\)]"),
]


def detect_series_from_title(title: str) -> tuple[str, str] | None:
    for pat in SERIES_PATTERNS:
        m = pat.match((title or "").strip())
        if m:
            return m.group(1).strip().rstrip(":,-"), m.group(2)
    return None


def series_name_match(candidate: str, target: str) -> bool:
    c = candidate.lower().rstrip("s").strip()
    t = target.lower().rstrip("s").strip()
    return c in t or t in c


def _is_likely_year(n: float) -> bool:
    if n != int(n):
        return False
    y = int(n)
    return 1900 <= y <= 2099


def extract_book_numbers_from_text(text: str) -> set[float]:
    """Volume/book indices from torrent titles; ignores publication years."""
    nums: set[float] = set()
    for pat in TORRENT_BOOK_NUM_PATTERNS:
        for m in pat.finditer(text or ""):
            try:
                n = float(m.group(1))
            except ValueError:
                continue
            if _is_likely_year(n):
                continue
            if 0 < n < 200:
                nums.add(n)
    return nums


_BOOK_ONE_MARKER = re.compile(
    r"\b(?:book|bk|vol|volume|#)\s*\.?\s*0*1(?:\b|[^\d])",
    re.IGNORECASE,
)

# Release-name junk that is NOT another volume's title: bracketed tags, years,
# bitrates, formats, "narrated by ...", etc.
_RELEASE_JUNK_RE = re.compile(
    r"[\[\(][^\]\)]*[\]\)]"
    r"|\b(?:19|20)\d{2}\b"
    r"|\b\d+\s*(?:k|kbps)\b"
    r"|\b(?:m4b|m4a|mp3|flac|aac|ogg|opus|epub|pdf|azw3?|mobi)\b"
    r"|\b(?:unabridged|abridged|audiobook|audiobooks|retail|graphicaudio)\b"
    r"|\b(?:read|narrated)\s+by\b.*$",
    re.IGNORECASE,
)


def _strip_release_junk(text: str) -> str:
    cleaned = _RELEASE_JUNK_RE.sub(" ", text or "")
    cleaned = re.sub(r"[^a-zA-Z0-9\s]", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _normalize_title(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower().strip())


def book_one_shares_series_title(
    *,
    title: str,
    series_name: str | None,
    base_title: str | None,
    target_index: str | float | None,
) -> bool:
    """True when volume 1's catalog title is essentially the series name (DCC book 1 case)."""
    try:
        if float(target_index or 0) != 1.0:
            return False
    except (TypeError, ValueError):
        return False
    series = _normalize_title(series_name or base_title or "")
    vol_title = _normalize_title(title)
    if not series or not vol_title:
        return False
    return vol_title == series or (series in vol_title and len(vol_title) <= len(series) + 3)


def looks_like_later_series_volume(
    result_title: str,
    *,
    target_index: str | float | None,
    series_name: str | None,
    base_title: str | None,
    volume_title: str | None,
) -> bool:
    """True when a torrent is probably book 2+ in a same-named series (e.g. 'DCC - Carl's Mercy')."""
    if target_index is None or str(target_index).strip() == "":
        return False
    try:
        target = float(target_index)
    except (TypeError, ValueError):
        return False

    rt = (result_title or "").strip()
    if not rt:
        return False

    found = extract_book_numbers_from_text(rt)
    if any(abs(n - target) >= 0.01 for n in found):
        return True
    if found:
        # Every number in the release matches the target volume (e.g.
        # "Series - 01 - Title" when searching for book 1) — definitely not later.
        return False

    if target != 1.0:
        return False
    if not book_one_shares_series_title(
        title=volume_title or "",
        series_name=series_name,
        base_title=base_title,
        target_index=target_index,
    ):
        return False

    base = (base_title or series_name or "").strip()
    if not base:
        return False

    lower = rt.lower()
    base_lower = base.lower()
    pos = lower.find(base_lower)
    if pos < 0:
        return False

    after = rt[pos + len(base) :].strip()
    if not after:
        return False

    rest = after.lstrip(" :–—-\t.[]")
    if not rest or _BOOK_ONE_MARKER.search(rest[:48]):
        return False
    if re.search(r"\bbook\s*#?\.?\s*0*1\b", lower):
        return False

    # "Series - Series" repetition (book 1 titled like the series, e.g.
    # "Dungeon Crawler Carl - Dungeon Crawler Carl") is the book itself.
    rest_norm = _normalize_title(re.sub(r"[^a-zA-Z0-9\s]+", " ", rest))
    for again in (base, volume_title or ""):
        again_norm = _normalize_title(re.sub(r"[^a-zA-Z0-9\s]+", " ", again))
        if again_norm and rest_norm.startswith(again_norm):
            return False

    # Only release junk left (year, bitrate, format tags, narrator credit)?
    # Then this is just "Series Title (2020) 64k" — the book itself, not a
    # later volume with a different subtitle.
    meaningful = _strip_release_junk(rest)
    if len(meaningful) <= 4:
        return False

    return True


def format_index_for_query(index: str | float | int) -> str:
    try:
        f = float(index)
        if f == int(f):
            return str(int(f))
        return str(f)
    except (TypeError, ValueError):
        return str(index).strip()
