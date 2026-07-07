"""Torbox debrid client.

Exposes the same duck-typed surface as app.services.real_debrid so the
streaming/resolve flows can treat either provider interchangeably:

    add_magnet(magnet) -> {"id": <torrent_id>}
    get_torrent_info(torrent_id) -> {"id", "status", "progress", "speed",
                                     "files": [{"id", "path", "bytes", "selected"}],
                                     "links": [...]}
    select_files(torrent_id, files)   (no-op — Torbox downloads all files)
    poll_until_ready(torrent_id)
    unrestrict_link(link) -> CDN url
    check_instant_availability(hashes) -> set[str]
    get_user_info()

Torbox has no RD-style "restricted link" concept — files are addressed by
(torrent_id, file_id) and turned into short-lived CDN URLs via /requestdl.
We bridge that with pseudo-links of the form:

    torbox://{torrent_id}/{file_id}/{url-quoted filename}

so callers that derive filenames from links (audio filtering) keep working.
"""

import asyncio
import logging
from typing import Any
from urllib.parse import quote, unquote

import httpx
from app.config import get_settings
from app.services import debrid_tokens

logger = logging.getLogger(__name__)

settings = get_settings()
BASE_URL = "https://api.torbox.app/v1/api"

PSEUDO_SCHEME = "torbox://"


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {debrid_tokens.torbox_token()}"}


def _raise_on_error(data: Any, action: str) -> None:
    if isinstance(data, dict) and data.get("success") is False:
        raise RuntimeError(f"Torbox {action} failed: {data.get('detail') or data.get('error')}")


def make_pseudo_link(torrent_id: Any, file_id: Any, filename: str) -> str:
    return f"{PSEUDO_SCHEME}{torrent_id}/{file_id}/{quote(filename or '')}"


def parse_pseudo_link(link: str) -> tuple[str, str, str] | None:
    """-> (torrent_id, file_id, filename) or None if not a torbox pseudo-link."""
    if not link or not link.startswith(PSEUDO_SCHEME):
        return None
    parts = link[len(PSEUDO_SCHEME):].split("/", 2)
    if len(parts) < 2:
        return None
    filename = unquote(parts[2]) if len(parts) > 2 else ""
    return parts[0], parts[1], filename


async def add_magnet(magnet_link: str) -> dict[str, Any]:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{BASE_URL}/torrents/createtorrent",
            headers=_headers(),
            data={"magnet": magnet_link},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        _raise_on_error(data, "createtorrent")
        payload = data.get("data") or {}
        torrent_id = payload.get("torrent_id") or payload.get("id")
        if torrent_id is None:
            raise RuntimeError(f"Torbox createtorrent returned no torrent id: {data}")
        return {"id": str(torrent_id)}


async def add_torrent_file(torrent_url: str) -> dict[str, Any]:
    """Download a .torrent file from a URL and upload it to Torbox.
    Falls back to add_magnet when the URL redirects to a magnet link."""
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

    return await add_torrent_bytes(body)


async def add_torrent_bytes(body: bytes) -> dict[str, Any]:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{BASE_URL}/torrents/createtorrent",
            headers=_headers(),
            files={"file": ("upload.torrent", body, "application/x-bittorrent")},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        _raise_on_error(data, "createtorrent")
        payload = data.get("data") or {}
        torrent_id = payload.get("torrent_id") or payload.get("id")
        if torrent_id is None:
            raise RuntimeError(f"Torbox createtorrent returned no torrent id: {data}")
        return {"id": str(torrent_id)}


async def select_files(torrent_id: str, files: str = "all") -> None:
    """Torbox downloads every file in the torrent — nothing to select."""
    return None


def _normalize_info(raw: dict[str, Any]) -> dict[str, Any]:
    """Map a Torbox mylist entry onto the RD get_torrent_info shape."""
    files = []
    for f in raw.get("files") or []:
        files.append({
            "id": f.get("id"),
            "path": f.get("name") or f.get("short_name") or "",
            "bytes": f.get("size") or 0,
            "selected": 1,
        })

    finished = bool(raw.get("download_finished")) and bool(raw.get("download_present"))
    tb_state = (raw.get("download_state") or "").lower()
    if finished:
        status = "downloaded"
    elif tb_state in ("error", "failed"):
        status = "error"
    elif "stalled" in tb_state:
        status = "downloading"
    elif tb_state in ("downloading", "metadl", "checkingresumedata", "queued", "paused",
                      "uploading", "completed", "cached", ""):
        status = "queued" if tb_state == "queued" else "downloading"
    else:
        status = "downloading"

    progress = raw.get("progress")
    progress_pct = int(float(progress) * 100) if isinstance(progress, (int, float)) and progress <= 1 else int(progress or 0)

    torrent_id = raw.get("id")
    links = [
        make_pseudo_link(torrent_id, f["id"], f["path"].rsplit("/", 1)[-1])
        for f in files
    ] if finished else []

    return {
        "id": str(torrent_id),
        "status": status,
        "progress": min(progress_pct, 100),
        "speed": raw.get("download_speed") or 0,
        "files": files,
        "links": links,
        "filename": raw.get("name") or "",
        "hash": (raw.get("hash") or "").lower(),
    }


async def get_torrent_info(torrent_id: str) -> dict[str, Any]:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{BASE_URL}/torrents/mylist",
            headers=_headers(),
            params={"id": str(torrent_id), "bypass_cache": "true"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        _raise_on_error(data, "mylist")
        payload = data.get("data")
        if isinstance(payload, list):
            payload = payload[0] if payload else None
        if not isinstance(payload, dict):
            raise RuntimeError(f"Torbox torrent {torrent_id} not found")
        return _normalize_info(payload)


async def poll_until_ready(
    torrent_id: str,
    interval: float = 30,
    timeout: float = 7200,
) -> dict[str, Any]:
    elapsed = 0.0
    while elapsed < timeout:
        info = await get_torrent_info(torrent_id)
        status = info.get("status")
        if status == "downloaded":
            return info
        if status == "error":
            raise RuntimeError("Torbox torrent failed")
        await asyncio.sleep(interval)
        elapsed += interval
    raise TimeoutError(f"Torbox torrent {torrent_id} did not complete within {timeout}s")


async def request_download_link(torrent_id: str, file_id: str) -> str:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{BASE_URL}/torrents/requestdl",
            params={
                "token": debrid_tokens.torbox_token(),
                "torrent_id": str(torrent_id),
                "file_id": str(file_id),
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        _raise_on_error(data, "requestdl")
        url = data.get("data")
        if not isinstance(url, str) or not url.startswith("http"):
            raise RuntimeError(f"Torbox requestdl returned no url: {data}")
        return url


async def unrestrict_link(link: str) -> str:
    """Accepts a torbox:// pseudo-link and returns a fresh CDN URL."""
    parsed = parse_pseudo_link(link)
    if not parsed:
        if link.startswith("http"):
            return link
        raise ValueError(f"Not a torbox link: {link[:64]}")
    torrent_id, file_id, _ = parsed
    return await request_download_link(torrent_id, file_id)


async def get_user_info() -> dict[str, Any]:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{BASE_URL}/user/me",
            headers=_headers(),
            params={"settings": "false"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        _raise_on_error(data, "user/me")
        return data.get("data") or {}


async def check_instant_availability(hashes: list[str]) -> set[str]:
    """Lowercase info hashes already cached on Torbox."""
    if not debrid_tokens.torbox_token():
        return set()

    unique = sorted({h.lower() for h in hashes if h})
    if not unique:
        return set()

    available: set[str] = set()
    batch_size = 80
    async with httpx.AsyncClient() as client:
        for i in range(0, len(unique), batch_size):
            batch = unique[i : i + batch_size]
            try:
                resp = await client.get(
                    f"{BASE_URL}/torrents/checkcached",
                    headers=_headers(),
                    params={"hash": ",".join(batch), "format": "object"},
                    timeout=30,
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                logger.warning("Torbox checkcached failed: %s", e)
                continue

            payload = data.get("data")
            if isinstance(payload, dict):
                for h, val in payload.items():
                    if val:
                        available.add(h.lower())
            elif isinstance(payload, list):
                for entry in payload:
                    h = (entry or {}).get("hash", "") if isinstance(entry, dict) else ""
                    if h:
                        available.add(h.lower())

    return available
