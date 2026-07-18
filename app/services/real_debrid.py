import asyncio
import base64
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from app.config import get_settings
from app.services import debrid_tokens

logger = logging.getLogger(__name__)

_MAGNET_BTIH_RE = re.compile(
    r"btih:([a-fA-F0-9]{40})|btih:([A-Za-z0-9]{32})(?:&|$|/)",
    re.IGNORECASE,
)

settings = get_settings()
BASE_URL = "https://api.real-debrid.com/rest/1.0"

# RD disabled /torrents/instantAvailability in late 2024 (error_code 37).
_instant_availability_disabled: bool | None = None
_account_hash_cache: tuple[float, dict[str, str]] | None = None  # hash -> torrent_id
_ACCOUNT_CACHE_TTL = 300.0


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {debrid_tokens.rd_token()}"}


async def add_magnet(magnet_link: str) -> dict[str, Any]:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{BASE_URL}/torrents/addMagnet",
            headers=_headers(),
            data={"magnet": magnet_link},
            timeout=30,
        )
        resp.raise_for_status()
        invalidate_account_cache()
        return resp.json()


async def ensure_magnet_in_account(
    magnet_link: str,
    info_hash: str | None = None,
) -> dict[str, Any]:
    """Add magnet to RD or return the existing account torrent id."""
    h = extract_info_hash(magnet_link, info_hash)
    if h:
        existing = await find_account_torrent_id(h)
        if existing:
            return {"id": existing, "existing": True}
    try:
        result = await add_magnet(magnet_link)
        return {**result, "existing": False}
    except httpx.HTTPStatusError:
        if h:
            invalidate_account_cache()
            existing = await find_account_torrent_id(h)
            if existing:
                return {"id": existing, "existing": True}
        raise


async def add_torrent_file(torrent_url: str) -> dict[str, Any]:
    """Download a .torrent file from a URL and upload it to Real-Debrid.
    If the URL redirects to a magnet link, falls back to add_magnet."""
    async with httpx.AsyncClient(follow_redirects=False) as client:
        torrent_resp = await client.get(torrent_url, timeout=60)

        while torrent_resp.is_redirect:
            location = torrent_resp.headers.get("location", "")
            if location.startswith("magnet:"):
                return await add_magnet(location)
            torrent_resp = await client.get(location, timeout=60)

        torrent_resp.raise_for_status()

        content_type = torrent_resp.headers.get("content-type", "")
        body = torrent_resp.content
        if body[:7] == b"magnet:" or "magnet" in content_type:
            return await add_magnet(body.decode("utf-8").strip())

    async with httpx.AsyncClient() as client:
        resp = await client.put(
            f"{BASE_URL}/torrents/addTorrent",
            headers=_headers(),
            content=body,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()


async def select_files(torrent_id: str, files: str = "all") -> None:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{BASE_URL}/torrents/selectFiles/{torrent_id}",
            headers=_headers(),
            data={"files": files},
            timeout=30,
        )
        resp.raise_for_status()


async def get_torrent_info(torrent_id: str) -> dict[str, Any]:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{BASE_URL}/torrents/info/{torrent_id}",
            headers=_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()


async def poll_until_ready(
    torrent_id: str,
    interval: float = 3,
    timeout: float = 7200,
    on_progress: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
) -> dict[str, Any]:
    elapsed = 0.0
    while elapsed < timeout:
        info = await get_torrent_info(torrent_id)
        status = info.get("status")
        if on_progress:
            result = on_progress(info)
            if asyncio.iscoroutine(result):
                await result
        if status == "downloaded":
            return info
        if status in ("magnet_error", "error", "virus", "dead"):
            raise RuntimeError(f"Real-Debrid torrent failed with status: {status}")
        await asyncio.sleep(interval)
        elapsed += interval

    raise TimeoutError(f"Real-Debrid torrent {torrent_id} did not complete within {timeout}s")


async def unrestrict_link(link: str) -> str:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{BASE_URL}/unrestrict/link",
            headers=_headers(),
            data={"link": link},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["download"]


async def get_user_info() -> dict[str, Any]:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{BASE_URL}/user",
            headers=_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()


def _base32_hash_to_hex(b32: str) -> str | None:
    """Convert 32-char BitTorrent base32 info-hash to 40-char hex."""
    s = b32.upper().strip()
    if len(s) != 32:
        return None
    try:
        pad = "=" * ((8 - len(s) % 8) % 8)
        return base64.b32decode(s + pad).hex()
    except Exception:
        return None


def _normalize_info_hash(raw: str | None) -> str | None:
    if not raw:
        return None
    h = raw.strip()
    if len(h) == 40 and re.fullmatch(r"[a-fA-F0-9]{40}", h):
        return h.lower()
    if len(h) == 32:
        return _base32_hash_to_hex(h)
    return None


def _hash_from_magnet_or_url(url: str | None) -> str | None:
    if not url:
        return None
    for m in _MAGNET_BTIH_RE.finditer(url):
        hex_hash, b32_hash = m.group(1), m.group(2)
        if hex_hash:
            return hex_hash.lower()
        if b32_hash:
            norm = _base32_hash_to_hex(b32_hash)
            if norm:
                return norm
    return None


def extract_info_hash(
    magnet_url: str | None,
    info_hash: str | None = None,
    download_url: str | None = None,
) -> str | None:
    """Return lowercase 40-char SHA1 info hash for RD instantAvailability checks."""
    norm = _normalize_info_hash(info_hash)
    if norm:
        return norm
    for url in (magnet_url, download_url):
        found = _hash_from_magnet_or_url(url)
        if found:
            return found
    return None


def _instant_availability_has_files(val: Any) -> bool:
    """True when RD reports at least one instantly available file variant."""
    if val is None:
        return False
    if isinstance(val, list):
        return len(val) > 0
    if not isinstance(val, dict):
        return bool(val)
    rd = val.get("rd")
    if isinstance(rd, list) and len(rd) > 0:
        return True
    for hoster_val in val.values():
        if _instant_availability_has_files(hoster_val):
            return True
    return False


def instant_availability_disabled() -> bool:
    """True when RD has disabled the instantAvailability API (error 37)."""
    return bool(_instant_availability_disabled)


def _mark_instant_availability_disabled() -> None:
    global _instant_availability_disabled
    if not _instant_availability_disabled:
        logger.warning(
            "Real-Debrid instantAvailability is disabled (error 37) — "
            "using account torrent list for cache detection"
        )
    _instant_availability_disabled = True


_RD_READY_STATUSES = frozenset({"downloaded"})
_ACCOUNT_LIST_MAX_PAGES = 200


async def list_account_torrents() -> list[dict[str, Any]]:
    """All torrents currently in the RD account (fully paginated)."""
    out: list[dict[str, Any]] = []
    page = 1
    async with httpx.AsyncClient() as client:
        while page <= _ACCOUNT_LIST_MAX_PAGES:
            resp = await client.get(
                f"{BASE_URL}/torrents",
                headers=_headers(),
                params={"page": str(page), "limit": "100"},
                timeout=30,
            )
            resp.raise_for_status()
            batch = resp.json()
            if not isinstance(batch, list) or not batch:
                break
            out.extend(batch)
            if len(batch) < 100:
                break
            page += 1
    return out


async def delete_torrent(torrent_id: str) -> None:
    async with httpx.AsyncClient() as client:
        resp = await client.delete(
            f"{BASE_URL}/torrents/delete/{torrent_id}",
            headers=_headers(),
            timeout=30,
        )
        resp.raise_for_status()
    invalidate_account_cache()


def _row_hash_id(row: dict[str, Any]) -> tuple[str, str] | None:
    raw = row.get("hash") or ""
    norm = _normalize_info_hash(raw)
    tid = row.get("id")
    if norm and tid is not None:
        return norm, str(tid)
    return None


async def _account_hash_maps(
    force_refresh: bool = False,
) -> tuple[dict[str, str], dict[str, str]]:
    """Return (all_hashes->id, ready/downloaded_hashes->id) for the RD account."""
    global _account_hash_cache
    now = time.time()
    if (
        not force_refresh
        and _account_hash_cache is not None
        and now - _account_hash_cache[0] < _ACCOUNT_CACHE_TTL
    ):
        ready = _account_hash_cache[1]
        all_map = _account_hash_cache[2] if len(_account_hash_cache) > 2 else ready
        return all_map, ready

    all_mapping: dict[str, str] = {}
    ready_mapping: dict[str, str] = {}
    try:
        for row in await list_account_torrents():
            pair = _row_hash_id(row)
            if not pair:
                continue
            norm, tid = pair
            all_mapping[norm] = tid
            if (row.get("status") or "").lower() in _RD_READY_STATUSES:
                ready_mapping[norm] = tid
    except Exception as e:
        logger.warning("RD account torrent list failed: %s", e)
        if _account_hash_cache is not None:
            ready = _account_hash_cache[1]
            all_map = _account_hash_cache[2] if len(_account_hash_cache) > 2 else ready
            return all_map, ready
        return {}, {}

    _account_hash_cache = (now, ready_mapping, all_mapping)
    return all_mapping, ready_mapping


async def _account_hash_map(force_refresh: bool = False) -> dict[str, str]:
    """Lowercase info-hash -> RD torrent id for downloaded torrents in the account."""
    _, ready = await _account_hash_maps(force_refresh=force_refresh)
    return ready


async def _account_all_hash_map(force_refresh: bool = False) -> dict[str, str]:
    """Lowercase info-hash -> RD torrent id for any torrent in the account."""
    all_map, _ = await _account_hash_maps(force_refresh=force_refresh)
    return all_map


async def find_account_torrent_id(info_hash: str) -> str | None:
    h = _normalize_info_hash(info_hash)
    if not h:
        return None
    return (await _account_all_hash_map()).get(h)


async def probe_magnet_cached(
    magnet_link: str,
    *,
    info_hash: str | None = None,
    timeout: float = 8.0,
    poll_interval: float = 1.0,
    delete_if_miss: bool = True,
) -> bool:
    """Blind-add probe: True when RD already has the torrent in its global cache."""
    if not debrid_tokens.rd_token():
        return False

    existing_id: str | None = None
    if info_hash:
        existing_id = await find_account_torrent_id(info_hash)
        if existing_id:
            info = await get_torrent_info(existing_id)
            if (info.get("status") or "").lower() == "downloaded":
                return True

    created_id: str | None = None
    torrent_id = existing_id
    try:
        if not torrent_id:
            result = await add_magnet(magnet_link)
            torrent_id = str(result.get("id") or "")
            if not torrent_id:
                return False
            if not result.get("existing"):
                created_id = torrent_id

        elapsed = 0.0
        while elapsed < timeout:
            info = await get_torrent_info(torrent_id)
            status = (info.get("status") or "").lower()
            if status == "downloaded":
                return True
            if status == "waiting_files_selection":
                try:
                    await select_files(torrent_id, "all")
                except Exception:
                    pass
            elif status in ("magnet_error", "error", "virus", "dead"):
                return False
            elif status == "downloading":
                progress = info.get("progress")
                if isinstance(progress, (int, float)) and progress >= 100:
                    return True
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
        return False
    finally:
        if created_id and delete_if_miss:
            try:
                info = await get_torrent_info(created_id)
                if (info.get("status") or "").lower() != "downloaded":
                    await delete_torrent(created_id)
            except Exception:
                try:
                    await delete_torrent(created_id)
                except Exception:
                    pass


async def probe_magnets_cached(
    items: list[tuple[str, str]],
    *,
    max_items: int = 20,
    delay: float = 0.45,
) -> set[str]:
    """Probe (hash, magnet) pairs; returns lowercase hashes cached on RD."""
    if not items or not debrid_tokens.rd_token():
        return set()

    hits: set[str] = set()
    sem = asyncio.Semaphore(2)

    async def _one(info_hash: str, magnet: str) -> None:
        async with sem:
            try:
                if await probe_magnet_cached(magnet, info_hash=info_hash):
                    hits.add(info_hash.lower())
            except Exception as e:
                logger.debug("RD magnet probe failed for %s: %s", info_hash[:12], e)
            await asyncio.sleep(delay)

    await asyncio.gather(*[_one(h, m) for h, m in items[:max_items]])
    return hits


async def check_account_availability(hashes: list[str]) -> set[str]:
    """Hashes already present in the RD account (ready or downloading)."""
    if not debrid_tokens.rd_token():
        return set()
    account = await _account_hash_map()
    available: set[str] = set()
    for raw in hashes:
        norm = _normalize_info_hash(raw) or _hash_from_magnet_or_url(raw)
        if norm and norm in account:
            available.add(norm)
    return available


def invalidate_account_cache() -> None:
    global _account_hash_cache
    _account_hash_cache = None


def parse_instant_availability_response(data: dict) -> set[str]:
    """Extract cached info-hash keys from RD /torrents/instantAvailability JSON."""
    available: set[str] = set()
    for key, val in data.items():
        if _instant_availability_has_files(val):
            available.add(key.lower())
    return available


async def check_instant_availability(hashes: list[str]) -> set[str]:
    """Hashes available on Real-Debrid (account or legacy instantAvailability API)."""
    if not debrid_tokens.rd_token():
        return set()

    unique: list[str] = []
    seen: set[str] = set()
    for h in hashes:
        norm = _normalize_info_hash(h) or _hash_from_magnet_or_url(h)
        if norm and norm not in seen:
            seen.add(norm)
            unique.append(norm)
    if not unique:
        return set()

    if _instant_availability_disabled:
        return await check_account_availability(unique)

    available: set[str] = set()
    batch_size = 12
    async with httpx.AsyncClient() as client:
        for i in range(0, len(unique), batch_size):
            batch = unique[i : i + batch_size]
            path = "/".join(batch)
            try:
                resp = await client.get(
                    f"{BASE_URL}/torrents/instantAvailability/{path}",
                    headers=_headers(),
                    timeout=30,
                )
                if resp.status_code == 403:
                    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                    if body.get("error_code") == 37 or body.get("error") == "disabled_endpoint":
                        _mark_instant_availability_disabled()
                        account_hits = await check_account_availability(unique)
                        return account_hits
                resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPStatusError as e:
                code = e.response.status_code
                logger.warning("RD instantAvailability HTTP %s (batch %s hashes)", code, len(batch))
                if code == 403:
                    try:
                        body = e.response.json()
                    except Exception:
                        body = {}
                    if body.get("error_code") == 37 or body.get("error") == "disabled_endpoint":
                        _mark_instant_availability_disabled()
                        return await check_account_availability(unique)
                if code in (403, 429) and len(batch) > 4:
                    for h in batch:
                        try:
                            r2 = await client.get(
                                f"{BASE_URL}/torrents/instantAvailability/{h}",
                                headers=_headers(),
                                timeout=15,
                            )
                            if r2.status_code == 403:
                                _mark_instant_availability_disabled()
                                return await check_account_availability(unique)
                            r2.raise_for_status()
                            d2 = r2.json()
                            if isinstance(d2, dict):
                                available |= parse_instant_availability_response(d2)
                        except Exception:
                            pass
                        await asyncio.sleep(0.35)
                continue
            except Exception as e:
                logger.warning("RD instantAvailability failed: %s", e)
                continue
            else:
                if isinstance(data, dict):
                    available |= parse_instant_availability_response(data)
            await asyncio.sleep(0.25)

    if not available and _instant_availability_disabled:
        return await check_account_availability(unique)
    return available
