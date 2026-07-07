import asyncio
import base64
import logging
import re
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
        return resp.json()


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


def parse_instant_availability_response(data: dict) -> set[str]:
    """Extract cached info-hash keys from RD /torrents/instantAvailability JSON."""
    available: set[str] = set()
    for key, val in data.items():
        if _instant_availability_has_files(val):
            available.add(key.lower())
    return available


async def check_instant_availability(hashes: list[str]) -> set[str]:
    """Hashes already cached on Real-Debrid (instant download after addMagnet)."""
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

    available: set[str] = set()
    batch_size = 50
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
                resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPStatusError as e:
                logger.warning("RD instantAvailability HTTP %s: %s", e.response.status_code, e)
                continue
            except Exception as e:
                logger.warning("RD instantAvailability failed: %s", e)
                continue

            if not isinstance(data, dict):
                continue
            available |= parse_instant_availability_response(data)

    return available
