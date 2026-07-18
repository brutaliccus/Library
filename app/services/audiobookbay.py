"""Direct AudioBook Bay search — multi-page, bypasses Jackett's 2-page cap.

Jackett's built-in AudioBookBay indexer only requests pages 1–2 (~18 results).
This module scrapes deeper listing pages **serially** under a global lock so the
Pi cannot spawn parallel FlareSolverr Chromium sessions and OOM/CPU-spike.

Live search streams page batches as they arrive (NDJSON progressive endpoint).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any
from urllib.parse import quote_plus, urljoin

import httpx
from bs4 import BeautifulSoup

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# .is often clears CF first on Pi; .lu is the canonical host when reachable.
_SITE_CANDIDATES = [
    "http://audiobookbay.is/",
    "https://audiobookbay.lu/",
    "http://audiobookbay.se/",
    "http://audiobookbay.fi/",
    "http://audiobookbay.ws/",
]

_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_CACHE_TTL = 300.0
_BASE_URL: str | None = None

# Only ONE ABB scrape (pages + optional FlareSolverr) at a time on this host.
_FETCH_LOCK = asyncio.Lock()
_HASH_LOCK = asyncio.Lock()

# Reuse one FlareSolverr browser session so page 2+ skip Cloudflare re-challenge.
_flare_session: tuple[str, float] | None = None
_abb_cookies: list[dict[str, Any]] | None = None
_abb_cookie_expires: float = 0.0

# Real ABB mirrors are unreachable via direct HTTP from the Pi (ConnectTimeout).
_CF_ONLY_HOSTS = ("audiobookbay.lu", "audiobookbay.is", "audiobookbay.se", "audiobookbay.fi", "audiobookbay.ws")

_HASH_RE = re.compile(r"Info\s*Hash:\s*</?t[dh][^>]*>\s*<t[dh][^>]*>\s*([a-fA-F0-9]{40})", re.I)
_HASH_TD_RE = re.compile(r"([a-fA-F0-9]{40})")
_SIZE_RE = re.compile(r"File\s*Size:\s*(.+?)(?:s?\s*$|\s*Posted)", re.I)
_POSTED_RE = re.compile(r"Posted:\s*(\d{1,2}\s+\w{3}\s+\d{4})", re.I)
_FORMAT_RE = re.compile(r"Format:\s*(.+?)\s*/", re.I)
_BITRATE_RE = re.compile(r"Bitrate:\s*(.+?)File", re.I)
_NON_WORD_RE = re.compile(r"[\W]+", re.UNICODE)


def _normalize_query(query: str) -> str:
    return _NON_WORD_RE.sub(" ", (query or "").strip()).strip().lower()


def _parse_size(size_text: str) -> int:
    m = re.search(r"([\d.]+)\s*(GB|MB|KB|TB)", size_text or "", re.I)
    if not m:
        return 0
    val = float(m.group(1))
    unit = m.group(2).upper()
    mult = {"KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}.get(unit, 1)
    return int(val * mult)


def _is_cloudflare(html: str | None) -> bool:
    if not html:
        return True
    lower = html.lower()
    return (
        "just a moment" in lower
        or "cf-browser-verification" in lower
        or "challenge-platform" in lower
        or "attention required" in lower
    )


def _page_delay() -> float:
    return max(1.5, float(getattr(settings, "abb_deep_page_delay_seconds", 2.5) or 2.5))


def _scrape_enabled(*, for_live: bool = False) -> bool:
    if for_live:
        # On-demand download search through Mullvad Flare is intentional and rare —
        # allow it whenever an ABB proxy is configured even if ABB_LIVE_SEARCH_ENABLED
        # is off (that flag is for unprotected Flare from the home IP).
        if (getattr(settings, "abb_proxy_url", "") or "").strip():
            return True
        return bool(getattr(settings, "abb_live_search_enabled", True))
    return bool(settings.abb_deep_search_enabled)


def _max_pages(override: int | None = None, *, for_live: bool = False) -> int:
    if override is not None:
        raw = override
    elif for_live:
        raw = getattr(settings, "abb_live_search_pages", None) or settings.abb_deep_search_pages
    else:
        raw = settings.abb_deep_search_pages
    return max(1, min(12, int(raw or 6)))


def _needs_flare_only(url: str) -> bool:
    low = url.lower()
    return any(h in low for h in _CF_ONLY_HOSTS)


async def _destroy_flare_session() -> None:
    global _flare_session
    if not _flare_session:
        return
    sid, _ = _flare_session
    _flare_session = None
    flare = (settings.flaresolverr_url or "").rstrip("/")
    if not flare or not sid:
        return
    api = f"{flare}/v1" if "/v1" not in flare else flare
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            await client.post(api, json={"cmd": "sessions.destroy", "session": sid})
    except Exception:
        pass


async def _flare_post(payload: dict[str, Any], *, client_timeout: float) -> dict[str, Any] | None:
    flare = (settings.flaresolverr_url or "").rstrip("/")
    if not flare:
        return None
    api = f"{flare}/v1" if "/v1" not in flare else flare
    try:
        async with httpx.AsyncClient(timeout=client_timeout) as client:
            resp = await client.post(api, json=payload)
            if resp.status_code >= 500:
                logger.warning("FlareSolverr HTTP %s for %s", resp.status_code, payload.get("cmd"))
                return None
            return resp.json()
    except httpx.TimeoutException:
        logger.warning("FlareSolverr client timeout (%.0fs) for %s", client_timeout, payload.get("cmd"))
        return None
    except Exception as e:
        logger.warning("FlareSolverr request failed: %s", e)
        return None


async def _ensure_flare_session() -> str | None:
    global _flare_session
    ttl = float(getattr(settings, "abb_flare_session_ttl", 600) or 600)
    now = time.time()
    if _flare_session and now - _flare_session[1] < ttl:
        return _flare_session[0]

    await _destroy_flare_session()
    create_payload: dict[str, Any] = {"cmd": "sessions.create"}
    proxy = _abb_proxy_dict()
    if proxy:
        # Proxy must be set on session create — request.get ignores it when session is set.
        create_payload["proxy"] = proxy
        logger.info("ABB FlareSolverr session will use Mullvad proxy %s", proxy["url"])
    data = await _flare_post(create_payload, client_timeout=45.0)
    if not data or data.get("status") != "ok":
        return None
    sid = data.get("session")
    if not sid:
        return None
    _flare_session = (sid, now)
    logger.info("ABB FlareSolverr session ready (%s…)", str(sid)[:8])
    return sid


def _abb_proxy_dict() -> dict[str, str] | None:
    """Proxy for ABB Flare only — never touches Knaben / other Flare users."""
    raw = (getattr(settings, "abb_proxy_url", "") or "").strip()
    if not raw:
        return None
    return {"url": raw}


def _flare_max_timeout_ms(*, warmup: bool = False) -> int:
    fs_timeout = float(getattr(settings, "abb_flaresolverr_timeout", 180) or 180)
    # CF warmup needs the full budget; session-reused pages are usually faster.
    if warmup:
        return int(fs_timeout * 1000)
    return max(90_000, int(fs_timeout * 1000))


def _flare_disable_media() -> bool:
    return bool(getattr(settings, "abb_flaresolverr_disable_media", True))


def _store_flare_cookies(solution: dict[str, Any] | None) -> None:
    global _abb_cookies, _abb_cookie_expires
    if not solution:
        return
    cookies = solution.get("cookies") or []
    if cookies:
        _abb_cookies = cookies
        _abb_cookie_expires = time.time() + float(getattr(settings, "abb_flare_session_ttl", 600) or 600)
        logger.info("ABB cached %s FlareSolverr cookies", len(cookies))


def _flare_payload(url: str, *, warmup: bool, session: str | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "cmd": "request.get",
        "url": url,
        "maxTimeout": _flare_max_timeout_ms(warmup=warmup),
    }
    if warmup:
        payload["waitInSeconds"] = 8
    if _flare_disable_media():
        payload["disableMedia"] = True
    if _abb_cookies and time.time() < _abb_cookie_expires:
        payload["cookies"] = _abb_cookies
    if session:
        payload["session"] = session
    else:
        # One-shot tabs: proxy on the request (session path sets it at create time).
        proxy = _abb_proxy_dict()
        if proxy:
            payload["proxy"] = proxy
    return payload


async def _fetch_via_flare(url: str, *, warmup: bool = False) -> str | None:
    """Fetch through FlareSolverr with a reused browser session."""
    fs_timeout = float(getattr(settings, "abb_flaresolverr_timeout", 180) or 180)
    client_timeout = fs_timeout + 60.0
    retries = max(2, int(getattr(settings, "abb_mirror_retries", 3) or 3))

    # First attempt: one-shot tab (less RAM). Later attempts reuse a session.
    session: str | None = None
    for attempt in range(retries):
        use_session = attempt > 0
        if use_session:
            session = await _ensure_flare_session()
        payload = _flare_payload(url, warmup=warmup, session=session if use_session else None)

        data = await _flare_post(payload, client_timeout=client_timeout)
        if data and data.get("status") == "ok":
            solution = data.get("solution") or {}
            _store_flare_cookies(solution)
            html = solution.get("response") or ""
            if html and not _is_cloudflare(html):
                return html
            logger.warning("ABB FlareSolverr returned CF/challenge page for %s", url)
        else:
            msg = str((data or {}).get("message", "no response"))[:200]
            logger.warning("ABB FlareSolverr attempt %s/%s failed for %s: %s", attempt + 1, retries, url, msg)

        await _destroy_flare_session()
        session = None
        if attempt + 1 < retries:
            await asyncio.sleep(4.0 + attempt * 2.0)

    return None


async def _direct_get(url: str, timeout: float = 12.0) -> str | None:
    """Cheap direct HTTP — never FlareSolverr (used for mirror probing)."""
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code < 400 and not _is_cloudflare(resp.text):
                return resp.text
    except Exception as e:
        logger.debug("ABB direct GET failed for %s: %s", url, e)
    return None


async def _fetch_html(url: str, timeout: float = 20.0, allow_flare: bool = True) -> str | None:
    """Direct GET when possible; FlareSolverr session for Cloudflare-only mirrors."""
    async with _FETCH_LOCK:
        if not _needs_flare_only(url):
            html = await _direct_get(url, timeout=min(timeout, 15.0))
            if html:
                return html
        if not allow_flare or not settings.flaresolverr_url:
            if _needs_flare_only(url):
                return await _fetch_via_flare(url, warmup=True)
            return None
        warmup = "page/" not in url and "?" not in url.split("/")[-1]
        return await _fetch_via_flare(url, warmup=warmup)


async def _resolve_base_url() -> str | None:
    """Pick a working ABB mirror. Real mirrors need FlareSolverr from this network."""
    global _BASE_URL
    if _BASE_URL:
        return _BASE_URL

    max_tries = max(1, min(len(_SITE_CANDIDATES), int(getattr(settings, "abb_mirror_max_tries", 3) or 3)))
    for base in _SITE_CANDIDATES[:max_tries]:
        if _needs_flare_only(base):
            html = await _fetch_via_flare(base, warmup=True)
            if html and "postTitle" in html:
                _BASE_URL = base
                logger.info("AudioBook Bay base URL (FlareSolverr): %s", base)
                return base
            await _destroy_flare_session()
            continue
        html = await _direct_get(base, timeout=8.0)
        if html and "postTitle" in html:
            _BASE_URL = base
            logger.info("AudioBook Bay base URL: %s", base)
            return base

    logger.warning("Could not reach any AudioBook Bay mirror")
    return None


def _decode_hidden_posts(soup: BeautifulSoup) -> None:
    """Jackett-compatible: ABB hides some posts as base64 in div.post.re-ab."""
    import base64

    for el in list(soup.select("div.post.re-ab")):
        try:
            raw = base64.b64decode(el.get_text(strip=True))
            inner = BeautifulSoup(raw, "html.parser")
            replacement = soup.new_tag("div")
            replacement["class"] = "post"
            replacement.append(inner)
            el.replace_with(replacement)
        except Exception:
            continue


def _parse_listing_page(html: str, base_url: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html or "", "html.parser")
    _decode_hidden_posts(soup)
    out: list[dict[str, Any]] = []

    for row in soup.select("div.post"):
        title_el = row.select_one("div.postTitle")
        link_el = row.select_one("div.postTitle h2 a") or row.select_one("div.postTitle a")
        if not title_el or not link_el:
            continue
        href = (link_el.get("href") or "").strip()
        if not href:
            continue
        details = urljoin(base_url, href.lstrip("/"))
        title = title_el.get_text(" ", strip=True)
        info = (row.select_one("div.postContent") or soup.new_tag("div")).get_text(" ", strip=True)

        fmt = _FORMAT_RE.search(info)
        if fmt and fmt.group(1).strip() not in ("", "?"):
            title = f"{title} [{fmt.group(1).strip()}]"
        bitrate = _BITRATE_RE.search(info)
        if bitrate and bitrate.group(1).strip() not in ("", "?"):
            title = f"{title} [{bitrate.group(1).strip()}]"

        size_m = _SIZE_RE.search(info)
        size = _parse_size(size_m.group(1) if size_m else "")

        published = None
        posted = _POSTED_RE.search(info)
        if posted:
            try:
                published = datetime.strptime(posted.group(1), "%d %b %Y").isoformat() + "Z"
            except ValueError:
                published = None

        out.append({
            "title": re.sub(r"\s+", " ", title).strip() or "Unknown",
            "size": size,
            "seeders": 1,
            "leechers": 0,
            "indexer": "AudioBookBay",
            "publishDate": published,
            "magnetUrl": None,
            "downloadUrl": details,
            "infoUrl": details,
            "guid": details,
            "infoHash": "",
            "categories": ["Audio/Audiobook"],
            "mediaType": "audiobook",
        })
    return out


async def _resolve_info_hash(details_url: str) -> str | None:
    # Serialize hash scrapes too — detail pages often need FlareSolverr.
    async with _HASH_LOCK:
        html = await _fetch_html(details_url, timeout=20.0, allow_flare=True)
    if not html:
        return None
    m = _HASH_RE.search(html)
    if m:
        return m.group(1).lower()
    soup = BeautifulSoup(html, "html.parser")
    for td in soup.find_all("td"):
        label = td.get_text(" ", strip=True).lower()
        if "info hash" in label:
            sib = td.find_next_sibling("td")
            if sib:
                hm = _HASH_TD_RE.search(sib.get_text(" ", strip=True))
                if hm:
                    return hm.group(1).lower()
    near = re.search(r"Info\s*Hash[\s\S]{0,120}?([a-fA-F0-9]{40})", html, re.I)
    return near.group(1).lower() if near else None


def _abb_details_url(item: dict[str, Any]) -> str:
    """Best URL for scraping the ABB details page (not Jackett /dl/ proxy links)."""
    for key in ("guid", "infoUrl", "comments"):
        url = (str(item.get(key) or "")).strip()
        if not url:
            continue
        compact = url.lower().replace(" ", "")
        if "audiobookbay" in compact or "/abss/" in url.lower():
            return url
    dl = (str(item.get("downloadUrl") or "")).strip()
    if dl and "/dl/" not in dl.lower() and "jackett" not in dl.lower():
        return dl
    return ""


async def _attach_hashes(
    results: list[dict[str, Any]],
    concurrency: int = 1,
) -> list[dict[str, Any]]:
    """Resolve magnets one-at-a-time by default (Pi-safe)."""
    out: list[dict[str, Any]] = []
    for item in results:
        url = _abb_details_url(item)
        if not url:
            out.append(item)
            continue
        try:
            info_hash = await _resolve_info_hash(url)
        except Exception as e:
            logger.debug("ABB hash resolve failed for %s: %s", url, e)
            out.append(item)
            continue
        if not info_hash:
            out.append(item)
            continue
        title = item.get("title") or "Unknown"
        magnet = f"magnet:?xt=urn:btih:{info_hash}&dn={quote_plus(title)}"
        out.append({**item, "infoHash": info_hash, "magnetUrl": magnet})
        if concurrency <= 1:
            await asyncio.sleep(0.4)
    with_hash = sum(1 for r in out if r.get("infoHash"))
    logger.info("ABB hash resolve: %s/%s ok", with_hash, len(out))
    return out


def _result_key(r: dict[str, Any]) -> str:
    key = (r.get("infoHash") or "").lower()
    if key:
        return key
    return r.get("downloadUrl") or r.get("guid") or f"{r.get('title')}|abb"


async def iter_search_pages(
    query: str,
    max_pages: int | None = None,
    *,
    for_live: bool = False,
) -> AsyncIterator[tuple[int, int, list[dict[str, Any]]]]:
    """Yield (page_num, max_pages, new_results) one listing page at a time.

    Serial + delayed. Safe for Pi. Caller displays each batch immediately.
    """
    q = _normalize_query(query)
    if len(q) < 2 or not _scrape_enabled(for_live=for_live):
        return

    pages = _max_pages(max_pages, for_live=for_live)
    base = await _resolve_base_url()
    if not base:
        return

    qs = quote_plus(q)
    seen_urls: set[str] = set()

    for page in range(1, pages + 1):
        if page == 1:
            url = f"{base}?s={qs}&tt=1"
        else:
            url = f"{base}page/{page}/?s={qs}&tt=1"

        html = await _fetch_html(url)
        if not html:
            logger.warning("ABB listing page %s failed for %r — stopping", page, q)
            break

        batch = _parse_listing_page(html, base)
        fresh: list[dict[str, Any]] = []
        for item in batch:
            u = item.get("downloadUrl") or ""
            if u and u not in seen_urls:
                seen_urls.add(u)
                fresh.append(item)

        logger.info("ABB page %s/%s for %r: %s posts (%s new)", page, pages, q, len(batch), len(fresh))
        yield page, pages, fresh

        if not fresh:
            break
        if page < pages:
            await asyncio.sleep(_page_delay())


async def search_deep(
    query: str,
    max_pages: int | None = None,
    resolve_hashes: bool = False,
    hash_concurrency: int = 1,
    *,
    for_live: bool = False,
) -> list[dict[str, Any]]:
    """Collect all ABB listing pages for one query (serial). Default: no hash resolve."""
    q = _normalize_query(query)
    if len(q) < 2:
        return []

    pages = _max_pages(max_pages, for_live=for_live)
    cache_key = f"{q}|{pages}|{int(resolve_hashes)}|{int(for_live)}"
    now = time.time()
    cached = _CACHE.get(cache_key)
    if cached and now - cached[0] < _CACHE_TTL:
        return [dict(r) for r in cached[1]]

    if not _scrape_enabled(for_live=for_live):
        return []

    listings: list[dict[str, Any]] = []
    async for _page, _max_p, fresh in iter_search_pages(query, max_pages=pages, for_live=for_live):
        listings.extend(fresh)

    if resolve_hashes and listings:
        listings = await _attach_hashes(listings, concurrency=max(1, hash_concurrency))

    _CACHE[cache_key] = (now, listings)
    logger.info("ABB deep search %r: %s results across ≤%s pages", q, len(listings), pages)
    return [dict(r) for r in listings]


async def search_deep_multi(
    queries: list[str],
    max_pages: int | None = None,
    resolve_hashes: bool = False,
    *,
    for_live: bool = False,
) -> list[dict[str, Any]]:
    """Search ABB with ONE primary query (serial pages). Extra queries are ignored.

    Running several deep queries in parallel is what crashed the Pi (N queries ×
    M FlareSolverr Chromium tabs). One good title query + deep paging is enough.
    """
    if not queries:
        return []
    # Prefer the first (usually the exact base title from build_audiobookbay_queries).
    return await search_deep(
        queries[0],
        max_pages=max_pages,
        resolve_hashes=resolve_hashes,
        for_live=for_live,
    )


async def fetch_recent_listings(*, max_pages: int = 2) -> list[dict[str, Any]]:
    """Browse ABB chronological recent posts (RSS substitute) via Flare + Mullvad proxy.

    Used for background RSS-only ingest so ABB is never contacted from the home IP
    through Jackett Torznab.
    """
    pages = max(1, min(4, int(max_pages)))
    base = await _resolve_base_url()
    if not base:
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page in range(1, pages + 1):
        url = base if page == 1 else urljoin(base, f"page/{page}/")
        html = await _fetch_html(url, allow_flare=True)
        if not html:
            break
        rows = _parse_listing_page(html, base)
        if not rows:
            break
        for r in rows:
            key = (r.get("guid") or r.get("downloadUrl") or r.get("title") or "").lower()
            if key and key not in seen:
                seen.add(key)
                out.append(r)
    logger.info("ABB recent listings via proxy: %s posts across ≤%s pages", len(out), pages)
    return out


async def resolve_hashes_for_results(
    results: list[dict[str, Any]],
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Resolve info hashes for top ABB rows (enables debrid instant badges)."""
    cap = max(0, int(limit if limit is not None else getattr(settings, "abb_resolve_hash_limit", 25) or 25))
    if cap <= 0 or not results:
        return results

    need = [r for r in results if not (r.get("infoHash") or "").strip()][:cap]
    if not need:
        return results

    resolved = await _attach_hashes(need, concurrency=1)
    by_key: dict[str, dict[str, Any]] = {}
    for r in resolved:
        key = _abb_details_url(r) or (r.get("downloadUrl") or r.get("guid") or "")
        if key:
            by_key[key] = r

    out: list[dict[str, Any]] = []
    for r in results:
        key = _abb_details_url(r) or (r.get("downloadUrl") or r.get("guid") or "")
        out.append(by_key.get(key, r) if key in by_key else r)
    return out


async def resolve_magnet_from_details(details_url: str, title: str = "") -> tuple[str | None, str | None]:
    """Resolve (magnet, info_hash) from an ABB details page. Used by stream/download."""
    info_hash = await _resolve_info_hash(details_url)
    if not info_hash:
        return None, None
    dn = quote_plus(title or "audiobook")
    return f"magnet:?xt=urn:btih:{info_hash}&dn={dn}", info_hash


async def infra_status() -> dict[str, Any]:
    """Lightweight ABB/FlareSolverr health for admin diagnostics (no scrape)."""
    flare = (settings.flaresolverr_url or "").rstrip("/")
    out: dict[str, Any] = {
        "liveSearchEnabled": bool(getattr(settings, "abb_live_search_enabled", True)),
        "deepSearchEnabled": bool(settings.abb_deep_search_enabled),
        "livePages": int(getattr(settings, "abb_live_search_pages", None) or settings.abb_deep_search_pages or 6),
        "flareConfigured": bool(flare),
        "jackettConfigured": bool((settings.jackett_url or "").strip() and (settings.jackett_api_key or "").strip()),
        "baseUrlCached": _BASE_URL,
        "cookiesCached": bool(_abb_cookies and time.time() < _abb_cookie_expires),
        "piNote": (
            "ABB mirrors are CF-only from this network. Pi ARM FlareSolverr often cannot solve "
            "challenges in time — set FLARESOLVERR_URL to an x86 host (PC/VPS) if deep pages stay empty."
        ),
    }
    if flare:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(flare if flare.endswith("/") else f"{flare}/")
                if resp.status_code < 500:
                    data = resp.json()
                    out["flareReady"] = True
                    out["flareVersion"] = data.get("version")
                    out["flareUserAgent"] = (data.get("userAgent") or "")[:80]
                else:
                    out["flareReady"] = False
                    out["flareError"] = f"HTTP {resp.status_code}"
        except Exception as e:
            out["flareReady"] = False
            out["flareError"] = str(e)[:160]
    return out
