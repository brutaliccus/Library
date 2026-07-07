"""Anna's Archive search and download integration.

Scrapes Anna's Archive for both audiobooks and ebooks, returning results
with direct download URLs that bypass the Real-Debrid pipeline entirely.
"""

import asyncio
import logging
import re
import time
from urllib.parse import urlencode
from typing import Any

import httpx
from bs4 import BeautifulSoup

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_MIRROR_DOMAINS = [
    "https://annas-archive.gs",
    "https://annas-archive.gl",
    "https://annas-archive.org",
    "https://annas-archive.li",
    "https://annas-archive.se",
    "https://annas-archive.pk",
]
_BASE_URL: str | None = None
_SEARCH_CACHE: dict[str, tuple[float, list[dict]]] = {}
_CACHE_TTL = 300

_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

AUDIO_EXTENSIONS = {".mp3", ".m4b", ".m4a", ".ogg", ".opus", ".flac", ".wav", ".aac"}
EBOOK_EXTENSIONS = {".epub", ".mobi", ".azw3", ".pdf", ".fb2", ".djvu", ".cbr", ".cbz", ".txt"}

AUDIO_KEYWORDS = re.compile(
    r"audiobook|mp3|m4b|unabridged|abridged|narrated|read\s+by|full[- ]cast",
    re.IGNORECASE,
)
EBOOK_KEYWORDS = re.compile(
    r"epub|mobi|azw|pdf|ebook|e[\-\s]?book|kindle|calibre",
    re.IGNORECASE,
)


def _detect_media_type(title: str, format_text: str, size_bytes: int = 0) -> str:
    combined = f"{title} {format_text}"
    has_audio = bool(AUDIO_KEYWORDS.search(combined))
    has_ebook = bool(EBOOK_KEYWORDS.search(combined))

    fmt_lower = format_text.lower()
    for ext in AUDIO_EXTENSIONS:
        if ext.lstrip(".") in fmt_lower:
            return "audiobook"
    for ext in EBOOK_EXTENSIONS:
        if ext.lstrip(".") in fmt_lower:
            return "ebook"

    if has_audio and not has_ebook:
        return "audiobook"
    if has_ebook and not has_audio:
        return "ebook"
    if size_bytes > 100 * 1024 * 1024:
        return "audiobook"
    if 0 < size_bytes < 50 * 1024 * 1024:
        return "ebook"
    return "unknown"


def _parse_size(size_text: str) -> int:
    m = re.search(r"([\d.]+)\s*(GB|MB|KB)", size_text, re.IGNORECASE)
    if not m:
        return 0
    val = float(m.group(1))
    unit = m.group(2).upper()
    if unit == "GB":
        return int(val * 1024 * 1024 * 1024)
    if unit == "MB":
        return int(val * 1024 * 1024)
    if unit == "KB":
        return int(val * 1024)
    return 0


async def _find_working_mirror() -> str:
    """Try each mirror domain and return the first one that resolves and responds."""
    global _BASE_URL
    if _BASE_URL:
        return _BASE_URL

    for domain in _MIRROR_DOMAINS:
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                resp = await client.head(domain)
                if resp.status_code < 500:
                    _BASE_URL = domain
                    logger.info(f"Anna's Archive mirror selected: {domain}")
                    return domain
        except Exception:
            continue

    logger.warning("No working Anna's Archive mirror found, defaulting to first")
    _BASE_URL = _MIRROR_DOMAINS[0]
    return _BASE_URL


def _clear_mirror_on_dns_error(e: Exception) -> None:
    """Clear cached mirror when DNS/connection fails so next request retries."""
    err = str(e).lower()
    if "name or service not known" in err or "nodename nor servname" in err:
        global _BASE_URL
        _BASE_URL = None


_CLOUDFLARE_INDICATORS = (
    "verifying your connection",
    "checking your browser",
    "cloudflare",
    "please wait while we verify",
)


def _is_cloudflare_challenge(html: str) -> bool:
    """Detect if the response is a Cloudflare challenge page instead of real content."""
    lower = html.lower()
    return any(ind in lower for ind in _CLOUDFLARE_INDICATORS)


def _parse_timer_seconds_from_html(html: str) -> int | None:
    """Extract countdown/wait timer in seconds from slow download page HTML/JS.

    Returns the detected value, or None if not found. Caller should use a default (e.g. 60).
    """
    if not html or len(html) < 20:
        return None
    text = html.replace("\n", " ").replace("\r", " ")
    candidates: list[int] = []

    # Meta refresh: content="60;url=..." or content="60"
    m = re.search(r'content=["\'](\d+)\s*[;,]', text, re.IGNORECASE)
    if m:
        candidates.append(int(m.group(1)))

    # setTimeout/setInterval - arg is often ms (60000) or seconds (60)
    for m in re.finditer(r"set(?:Timeout|Interval)\s*\(\s*[^,]+,\s*(\d+)\s*\)", text):
        val = int(m.group(1))
        if val > 1000:
            candidates.append(val // 1000)
        elif 5 <= val <= 120:
            candidates.append(val)

    # Common JS variable patterns: countdown=60, seconds=60, waitSeconds:60
    for pat in [
        r"(?:countdown|seconds|waitSeconds?|wait|delay|timer)\s*[=:]\s*(\d+)",
        r"(\d+)\s*seconds?\s*(?:remaining|left|to wait)",
        r"please\s+wait\s+(\d+)\s*seconds?",
        r"data-(?:countdown|wait|seconds?)=[\"'](\d+)[\"']",
        r"(?:in|after)\s+(\d+)\s*seconds?",
    ]:
        for m in re.finditer(pat, text, re.IGNORECASE):
            val = int(m.group(1))
            if 5 <= val <= 120:
                candidates.append(val)

    # Visible countdown: "60" in a likely timer element
    m = re.search(r'(?:id|class)=["\'][^"\']*countdown[^"\']*["\'][^>]*>[\s\d]*(\d+)', text, re.IGNORECASE)
    if m:
        val = int(m.group(1))
        if 5 <= val <= 120:
            candidates.append(val)

    # Anna's Archive partner countdown: .js-partner-countdown
    m = re.search(r'js-partner-countdown[^>]*>[\s\d]*(\d+)', text, re.IGNORECASE)
    if m:
        val = int(m.group(1))
        if 5 <= val <= 120:
            candidates.append(val)

    if not candidates:
        return None
    return max(candidates)


async def _fetch_via_flaresolverr(
    url: str, wait_seconds: int = 0, return_final_url: bool = False, retries: int = 2
) -> str | tuple[str | None, str | None]:
    """Fetch URL via FlareSolverr. Returns HTML, or (html, final_url) if return_final_url.
    Retries on ReadTimeout/ConnectError (FlareSolverr can timeout under concurrent load)."""
    base_url = (settings.flaresolverr_url or "").rstrip("/")
    if not base_url:
        return (None, None) if return_final_url else None
    api_url = f"{base_url}/v1" if "/v1" not in base_url else base_url
    # Timer pages need ~65s wait + page load; allow 7 min for DDoS-Guard delays
    req_timeout = 420 if wait_seconds >= 60 else 180
    payload: dict[str, Any] = {
        "cmd": "request.get",
        "url": url,
        "maxTimeout": max(420000, (wait_seconds + 120) * 1000),
    }
    if wait_seconds > 0:
        payload["waitInSeconds"] = wait_seconds
    for attempt in range(retries):
        try:
            async with httpx.AsyncClient(timeout=req_timeout) as client:
                resp = await client.post(api_url, json=payload)
                resp.raise_for_status()
                data = resp.json()
            if data.get("status") != "ok":
                msg = str(data.get("message", data))
                logger.warning(f"FlareSolverr error: {msg}")
                if "name_not_resolved" in msg.lower() or "err_name_not_resolved" in msg.lower():
                    logger.warning(
                        "FlareSolverr DNS failure — ensure the flaresolverr container has public DNS "
                        "(docker-compose dns: 1.1.1.1 / 8.8.8.8) and restart it"
                    )
                return (None, None) if return_final_url else None
            solution = data.get("solution", {})
            html = solution.get("response") or None
            final_url = solution.get("url") or None
            if return_final_url:
                return (html, final_url)
            return html
        except (httpx.ReadTimeout, httpx.ConnectError, httpx.WriteTimeout) as e:
            if attempt < retries - 1:
                logger.info(f"FlareSolverr timeout/error (attempt {attempt + 1}/{retries}), retrying: {e}")
                await asyncio.sleep(5)
            else:
                logger.warning(f"FlareSolverr fetch failed after {retries} attempts: {type(e).__name__}: {e}")
        except Exception as e:
            logger.warning(f"FlareSolverr fetch failed: {type(e).__name__}: {e}")
            return (None, None) if return_final_url else None
    return (None, None) if return_final_url else None


def _build_session() -> httpx.AsyncClient:
    cookies = {}
    if settings.aa_account_id:
        cookies["aa_account_id2"] = settings.aa_account_id
    return httpx.AsyncClient(
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        cookies=cookies,
        follow_redirects=True,
        timeout=60,
    )


async def search(query: str, content_types: list[str] | None = None) -> list[dict[str, Any]]:
    """Search Anna's Archive. Returns results in the same shape as Prowlarr results."""
    cache_key = f"aa:{query.lower().strip()}"
    now = time.time()
    if cache_key in _SEARCH_CACHE:
        cached_at, results = _SEARCH_CACHE[cache_key]
        if now - cached_at < _CACHE_TTL:
            return results

    params: list[tuple[str, str]] = [
        ("q", query),
        ("lang", "en"),
        ("content", "book_fiction"),
        ("content", "book_nonfiction"),
        ("content", "book_unknown"),
        ("sort", ""),
    ]

    if content_types:
        params = [(k, v) for k, v in params if k != "content"]
        for ct in content_types:
            params.append(("content", ct))

    base = await _find_working_mirror()
    search_url = f"{base}/search?{urlencode(params)}"
    html = ""

    try:
        async with _build_session() as client:
            resp = await client.get(f"{base}/search", params=params)
            resp.raise_for_status()
            html = resp.text
    except Exception as e:
        logger.warning(f"Anna's Archive search failed: {e}")
        _clear_mirror_on_dns_error(e)
        return []

    if _is_cloudflare_challenge(html) and settings.flaresolverr_url:
        html = await _fetch_via_flaresolverr(search_url, wait_seconds=10)
        if html:
            logger.info("Anna's Archive: fetched via FlareSolverr (Cloudflare bypass)")

    soup = BeautifulSoup(html or "", "html.parser")
    result_rows = soup.select("div.js-aarecord-list-outer > div.flex")
    if not result_rows:
        result_rows = soup.select("main div.js-aarecord-list-outer div[class*='flex']")
    if not result_rows:
        result_rows = soup.select("div[class*='js-aarecord-list-outer'] div[class*='flex']")
    if not result_rows:
        md5_links = soup.select('a[href^="/md5/"]')
        result_rows = []
        for a in md5_links:
            parent = a.find_parent("div", class_=lambda c: c and "flex" in str(c))
            if parent and parent not in result_rows:
                result_rows.append(parent)

    results: list[dict[str, Any]] = []
    seen_md5: set[str] = set()
    for row in result_rows:
        md5_link = row.select_one('a[href^="/md5/"]')
        if not md5_link:
            continue
        href = md5_link.get("href", "")
        md5 = href.replace("/md5/", "").strip()
        if not md5 or md5 in seen_md5:
            continue
        seen_md5.add(md5)

        title = "Unknown Title"
        title_link = row.select_one("a.js-vim-focus")
        if title_link:
            title = title_link.get_text(strip=True)

        author = "Unknown Author"
        author_links = row.select('a[href^="/search?q="]')
        for al in author_links:
            icon = al.select_one('span[class*="user-edit"]')
            if icon:
                author = al.get_text(strip=True)
                break

        format_text = ""
        meta_div = row.select_one("div.text-gray-800, div.dark\\:text-slate-400")
        if meta_div:
            format_text = meta_div.get_text(" ", strip=True)

        size_bytes = _parse_size(format_text)
        media_type = _detect_media_type(title, format_text, size_bytes)

        file_ext = ""
        ext_match = re.search(r"\b(epub|pdf|mobi|azw3?|mp3|m4b|m4a|fb2|djvu|cbr|cbz|txt|zip|rar)\b", format_text, re.IGNORECASE)
        if ext_match:
            file_ext = ext_match.group(1).lower()

        results.append({
            "title": title,
            "author": author,
            "size": size_bytes,
            "seeders": 0,
            "leechers": 0,
            "indexer": "Anna's Archive",
            "publishDate": None,
            "magnetUrl": None,
            "downloadUrl": None,
            "infoUrl": f"{base}{href}",
            "categories": [],
            "mediaType": media_type,
            "source": "annas_archive",
            "aaMd5": md5,
            "fileExtension": file_ext,
            "formatInfo": format_text,
        })

    _SEARCH_CACHE[cache_key] = (now, results)
    logger.info(f"Anna's Archive: found {len(results)} results for '{query}'")
    if not results and html:
        snippet = html[:500].replace("\n", " ").strip()
        logger.info(f"Anna's Archive 0 results for '{query}', snippet: {snippet}...")
    return results


async def _fetch_detail_html(md5: str) -> tuple[str, str] | None:
    """Fetch AA detail page HTML. Returns (base_url, html) or None."""
    base = await _find_working_mirror()
    url = f"{base}/md5/{md5}"
    html = ""

    try:
        async with _build_session() as client:
            resp = await client.get(url)
            resp.raise_for_status()
            html = resp.text
    except Exception as e:
        logger.warning(f"AA detail page failed for {md5}: {e}")
        _clear_mirror_on_dns_error(e)
        return None

    if _is_cloudflare_challenge(html) and settings.flaresolverr_url:
        html = await _fetch_via_flaresolverr(url) or html

    return base, html or ""


def _extract_download_urls(html: str, base: str) -> list[str]:
    """Collect partner and slow-download URLs from a detail page (partners first)."""
    soup = BeautifulSoup(html or "", "html.parser")
    partners: list[str] = []
    slow: str | None = None
    seen: set[str] = set()

    for a in soup.select('a[href^="/slow_download/"]'):
        href = a.get("href", "").strip()
        if href and slow is None:
            slow = f"{base}{href}"

    for a in soup.select('a.js-download-link[href^="http"]'):
        href = a.get("href", "").strip()
        if not href or any(x in href for x in ["annas-archive.", "fast_download"]):
            continue
        if ".onion" in href.lower():
            continue
        if href not in seen:
            seen.add(href)
            partners.append(href)
            logger.info(f"AA: found partner link {href[:80]}...")

    def _partner_rank(url: str) -> int:
        lower = url.lower()
        if "archive.org" in lower:
            return 0
        if "libgen" in lower:
            return 1
        if "library.lol" in lower:
            return 2
        if "z-lib" in lower or "zlib" in lower:
            return 3
        return 4

    partners.sort(key=_partner_rank)
    urls = partners
    if slow:
        urls.append(slow)
    return urls


async def get_download_urls(md5: str) -> list[str]:
    """Return candidate download URLs from the AA detail page (slow first, then partners)."""
    fetched = await _fetch_detail_html(md5)
    if not fetched:
        return []
    base, html = fetched
    return _extract_download_urls(html, base)


async def get_download_url(md5: str) -> str | None:
    """Navigate to the book detail page and extract the slow download URL (no membership required)."""
    urls = await get_download_urls(md5)
    return urls[0] if urls else None


_FILE_EXT_IN_URL = re.compile(
    r"\.(epub|pdf|mobi|azw3?|fb2|djvu|cbr|cbz|txt|zip|rar|mp3|m4b|m4a)(?:\?|~|/|$)", re.IGNORECASE
)


def _is_likely_file_url(url: str) -> bool:
    """True if URL looks like a direct file (has extension or known file host)."""
    if "library.lol" in url and "/fiction/" in url and "/get" not in url.lower():
        return False
    if _FILE_EXT_IN_URL.search(url):
        return True
    if "get.php" in url or "/main/" in url:
        return True
    if "cloudflare-ipfs.com" in url or "ipfs.io" in url or "dweb.link" in url:
        return True
    return False


async def _resolve_library_page(url: str, depth: int = 0, max_depth: int = 2) -> str | None:
    """Fetch library.lol/libgen HTML page and extract the actual file download URL."""
    if depth >= max_depth:
        return None
    is_timer_page = "get.php" in url or "/main/" in url
    wait_sec = 65 if is_timer_page else 10
    html = ""
    final_url: str | None = None

    try:
        async with _build_session() as client:
            resp = await client.get(url)
            resp.raise_for_status()
            html = resp.text
    except Exception as e:
        logger.warning(f"AA library page fetch failed: {e}")
        return None

    if _is_cloudflare_challenge(html) and settings.flaresolverr_url:
        result = await _fetch_via_flaresolverr(url, wait_seconds=wait_sec, return_final_url=True)
        if isinstance(result, tuple):
            html, final_url = result
            if final_url and _is_likely_file_url(final_url):
                return final_url
        else:
            html = result or html

    soup = BeautifulSoup(html or "", "html.parser")
    file_links: list[str] = []
    other_links: list[str] = []

    for a in soup.select('a[href^="http"]'):
        href = a.get("href", "")
        if not href.startswith("http"):
            continue
        if _is_likely_file_url(href):
            file_links.append(href)
        elif any(kw in href for kw in ["library.lol", "libgen", "libgen.rs", "libgen.li", "cloudflare", "ipfs"]):
            other_links.append(href)

    if file_links:
        logger.info(f"AA: found file link from library page: {file_links[0][:80]}...")
        return file_links[0]
    for link in other_links:
        if "get.php" in link or "/main/" in link:
            logger.info(f"AA: following get.php/main link: {link[:80]}...")
            if settings.flaresolverr_url:
                fs_result = await _fetch_via_flaresolverr(link, wait_seconds=65, return_final_url=True)
                if isinstance(fs_result, tuple):
                    _, final = fs_result
                    if final and _is_likely_file_url(final):
                        return final
                html2 = fs_result if not isinstance(fs_result, tuple) else fs_result[0]
                if html2:
                    soup2 = BeautifulSoup(html2, "html.parser")
                    for sel in ("span.bg-gray-200.break-all", "span.break-all"):
                        el = soup2.select_one(sel)
                        if el:
                            u = el.get_text(strip=True)
                            if u.startswith("http") and _is_likely_file_url(u):
                                return u
                    dl = soup2.select_one("#download-button")
                    if dl and dl.get("href") and _is_likely_file_url(dl["href"]):
                        return dl["href"]
            return link
    for link in other_links:
        if "library.lol" not in link and "fiction" not in link:
            resolved = await _resolve_library_page(link, depth + 1, max_depth)
            if resolved:
                return resolved
    return None


def _is_library_page_url(url: str) -> bool:
    """True if URL is a library page (libgen, library.lol) that shows GET link, not a timer page."""
    return any(x in url.lower() for x in ["libgen.is/fiction", "library.lol/fiction", "libgen.li/fiction"])


def _is_timer_page_url(url: str) -> bool:
    """True if URL is a timer page - download link appears only after ~60s countdown. Must use FlareSolverr."""
    return "/slow_download/" in url or "get.php" in url or "/main/" in url


def is_unreachable_libgen_cdn(url: str) -> bool:
    """Libgen CDN links are often bare IPs blocked/unreachable from home networks."""
    return bool(re.match(r"https?://\d{1,3}(?:\.\d{1,3}){3}:\d+/", url))


_EBOOK_EXT_ORDER = (".epub", ".pdf", ".mobi", ".azw3", ".djvu", ".txt", ".cbr", ".cbz")
_AUDIO_EXT_ORDER = (".m4b", ".mp3", ".m4a", ".flac", ".ogg", ".opus", ".zip", ".rar")


async def _resolve_archive_org(page_url: str, media_type: str = "ebook") -> str | None:
    """Resolve archive.org/details/… to a direct /download/… file URL."""
    if "archive.org" not in page_url:
        return None
    ident = None
    if "/details/" in page_url:
        ident = page_url.split("/details/")[-1].split("/")[0].split("?")[0]
    elif "/download/" in page_url:
        parts = page_url.split("/download/")[-1].split("/")
        if len(parts) >= 2:
            return page_url.split("?")[0]
        ident = parts[0]
    if not ident:
        return None

    api_url = f"https://archive.org/metadata/{ident}"
    try:
        async with _build_session() as client:
            resp = await client.get(api_url)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.warning(f"AA archive.org metadata failed for {ident}: {e}")
        return None

    files = data.get("files") or []
    ext_order = _AUDIO_EXT_ORDER if media_type == "audiobook" else _EBOOK_EXT_ORDER

    def _score(name: str) -> tuple[int, int]:
        lower = name.lower()
        if "_encrypted" in lower or lower.endswith(".sqlite"):
            return (100, 0)
        for i, ext in enumerate(ext_order):
            if lower.endswith(ext):
                return (i, -len(name))
        if lower.endswith(".torrent"):
            return (99, 0)
        return (50, -len(name))

    candidates = [
        f for f in files
        if isinstance(f, dict)
        and f.get("name")
        and not str(f["name"]).endswith("_files.xml")
        and not str(f["name"]).endswith("_meta.txt")
        and not str(f["name"]).endswith(".sqlite")
        and "_encrypted" not in str(f["name"]).lower()
    ]
    if not candidates:
        return None

    candidates.sort(
        key=lambda f: (_score(str(f["name"])), -int(f.get("size") or 0)),
    )
    chosen = candidates[0]["name"]
    direct = f"https://archive.org/download/{ident}/{chosen}"
    logger.info(f"AA: archive.org file selected: {chosen}")
    return direct


def _finalize_resolved_url(url: str | None) -> str | None:
    if not url:
        return None
    if is_unreachable_libgen_cdn(url):
        logger.warning(f"AA: skipping unreachable libgen CDN: {url[:90]}...")
        return None
    return url


async def resolve_download(page_url: str, media_type: str = "ebook") -> str | None:
    """Follow an AA slow_download page to get the actual file download URL.

    Timer pages (/slow_download/, get.php) need FlareSolverr first - the link appears only after ~60s.
    Other pages: direct HTTP first, FlareSolverr only when Cloudflare detected.
    """
    archive_url = await _resolve_archive_org(page_url, media_type)
    if archive_url:
        return archive_url

    lower = page_url.lower()
    if any(x in lower for x in ("libgen.", "library.lol", "z-lib", "zlib")):
        resolved = _finalize_resolved_url(await _resolve_library_page(page_url))
        if resolved:
            return resolved

    base = await _find_working_mirror()
    html = ""
    final_url: str | None = None

    is_library = _is_library_page_url(page_url)
    is_timer = _is_timer_page_url(page_url)
    wait_sec = 10 if is_library else 65

    if is_timer and settings.flaresolverr_url:
        result = await _fetch_via_flaresolverr(
            page_url, wait_seconds=wait_sec, return_final_url=True
        )
        if isinstance(result, tuple):
            html, final_url = result
        else:
            html = result or ""
    else:
        try:
            async with _build_session() as client:
                resp = await client.get(page_url)
                resp.raise_for_status()
                html = resp.text
        except Exception as e:
            logger.warning(f"AA resolve download failed: {e}")
            _clear_mirror_on_dns_error(e)
            return None

        if _is_cloudflare_challenge(html) and settings.flaresolverr_url:
            result = await _fetch_via_flaresolverr(
                page_url, wait_seconds=wait_sec, return_final_url=True
            )
            if isinstance(result, tuple):
                html, final_url = result
            else:
                html = result or html

    if final_url and _is_likely_file_url(final_url):
        logger.info(f"AA: FlareSolverr redirect to file: {final_url[:100]}...")
        return _finalize_resolved_url(final_url)

    soup = BeautifulSoup(html or "", "html.parser")

    # Anna's Archive partner page: direct URL in span (original selectors first for compatibility)
    # Then dark-mode variants and broader fallbacks for alternate page layouts
    for sel in (
        "span.bg-gray-200.break-all",
        "span.break-all",
        "span[class*='break-all']",
        "span.dark\\:bg-slate-700.break-all",
        "span.dark\\:bg-gray-700.break-all",
        "div.break-all",
        "div[class*='break-all']",
        "code.break-all",
        "pre.break-all",
    ):
        for el in soup.select(sel):
            url_text = el.get_text(strip=True)
            if url_text.startswith("http") and _is_likely_file_url(url_text):
                logger.info(f"AA: found direct URL in {sel[:30]}: {url_text[:80]}...")
                return _finalize_resolved_url(url_text)

    # Download button (id=download-button) - href added when timer completes
    dl_btn = soup.select_one("#download-button")
    if dl_btn:
        href = dl_btn.get("href", "")
        if href.startswith("http") and _is_likely_file_url(href):
            logger.info(f"AA: found file URL in #download-button: {href[:80]}...")
            return _finalize_resolved_url(href)

    download_link = soup.select_one('a[href*="download"]')
    if download_link:
        href = download_link.get("href", "")
        if href.startswith("http"):
            if _is_likely_file_url(href):
                return _finalize_resolved_url(href)
            if any(kw in href for kw in ["library.lol", "libgen"]):
                resolved = await _resolve_library_page(href)
                if resolved:
                    return _finalize_resolved_url(resolved)
        if href.startswith("/"):
            return f"{base}{href}"

    for a in soup.select("a[href]"):
        href = a.get("href", "")
        if not href.startswith("http"):
            continue
        if _is_likely_file_url(href):
            return _finalize_resolved_url(href)
        if any(kw in href for kw in ["partner", "library.lol", "libgen", "ipfs"]):
            resolved = await _resolve_library_page(href)
            if resolved:
                return _finalize_resolved_url(resolved)

    # Fallback: scan raw HTML for CDN-style file URLs (b4mcx2ml.net, libgen CDNs, etc.)
    # Matches .mobi~, .epub, .pdf, .m4b, .mp3 and similar LibGen CDN format
    cdn_pattern = re.compile(
        r'https?://[a-zA-Z0-9.-]+\.(?:net|com|io|org)/[^\s"\'<>]*(?:\.mobi~?|\.epub|\.pdf|\.m4b|\.mp3|\.azw3?)[^\s"\'<>]*',
        re.IGNORECASE,
    )
    for m in cdn_pattern.finditer(html or ""):
        url = m.group(0).rstrip("'\">,)")
        if _is_likely_file_url(url):
            logger.info(f"AA: found file URL in page: {url[:80]}...")
            return _finalize_resolved_url(url)

    # Last resort: data attributes (some pages use data-download-url, data-href)
    for el in soup.select("[data-download-url], [data-href], [data-url]"):
        u = el.get("data-download-url") or el.get("data-href") or el.get("data-url")
        if u and isinstance(u, str) and u.startswith("http") and _is_likely_file_url(u):
            logger.info(f"AA: found file URL in data attribute: {u[:80]}...")
            return _finalize_resolved_url(u)

    return None
