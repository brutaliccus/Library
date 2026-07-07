"""Provider-agnostic debrid layer.

Both Real-Debrid (app.services.real_debrid) and Torbox (app.services.torbox)
expose the same module-level surface, so routers pick a provider once and use
the returned module for every subsequent call.

Provider keys: "rd" (Real-Debrid) and "torbox".
"""

import asyncio
import logging
from typing import Any

from app.config import get_settings
from app.services import debrid_tokens, real_debrid, torbox

logger = logging.getLogger(__name__)
settings = get_settings()

RD = "rd"
TORBOX = "torbox"
ALL_PROVIDERS = (RD, TORBOX)

PROVIDER_LABELS = {RD: "Real-Debrid", TORBOX: "Torbox"}

_MODULES = {RD: real_debrid, TORBOX: torbox}


def get_client(provider: str):
    """Returns the duck-typed provider module. Unknown/blank -> Real-Debrid."""
    return _MODULES.get((provider or RD).lower(), real_debrid)


def normalize_provider(provider: str | None) -> str:
    p = (provider or "").lower().strip()
    return p if p in _MODULES else RD


def provider_configured(provider: str) -> bool:
    """Token available in the current context (user's library group or env)."""
    if provider == TORBOX:
        return bool(debrid_tokens.torbox_token())
    return bool(debrid_tokens.rd_token())


def available_providers() -> list[str]:
    return [p for p in ALL_PROVIDERS if provider_configured(p)]


def extract_info_hash(magnet_url: str | None, info_hash: str | None = None,
                      download_url: str | None = None) -> str | None:
    return real_debrid.extract_info_hash(magnet_url, info_hash, download_url)


async def check_cached_all(hashes: list[str]) -> dict[str, set[str]]:
    """Check instant/cached availability on every configured provider concurrently.
    Returns {provider: set(lowercase hashes cached)}."""
    providers = available_providers()
    if not providers or not hashes:
        return {p: set() for p in ALL_PROVIDERS}

    async def _check(p: str) -> set[str]:
        try:
            return await get_client(p).check_instant_availability(hashes)
        except Exception as e:
            logger.warning("%s cache check failed: %s", PROVIDER_LABELS[p], e)
            return set()

    results = await asyncio.gather(*[_check(p) for p in providers])
    out: dict[str, set[str]] = {p: set() for p in ALL_PROVIDERS}
    for p, cached in zip(providers, results):
        out[p] = cached
    return out


def pick_provider(
    info_hash: str | None,
    cached_by_provider: dict[str, set[str]] | None,
    preferred: str | None,
) -> str:
    """Auto-pick: a provider that has the torrent cached wins (preferred one
    first when both do); otherwise the user's preferred provider; otherwise
    whichever is configured."""
    providers = available_providers()
    if not providers:
        return RD
    pref = normalize_provider(preferred)
    order = [pref] + [p for p in providers if p != pref] if pref in providers else list(providers)

    if info_hash and cached_by_provider:
        h = info_hash.lower()
        for p in order:
            if h in cached_by_provider.get(p, set()):
                return p

    return order[0]


async def pick_provider_for_magnet(magnet_or_hash: str | None, preferred: str | None) -> str:
    """Convenience: resolve the hash, run cache checks, pick a provider."""
    providers = available_providers()
    if len(providers) <= 1:
        return providers[0] if providers else RD

    h = extract_info_hash(magnet_or_hash, magnet_or_hash)
    if not h:
        return pick_provider(None, None, preferred)
    cached = await check_cached_all([h])
    choice = pick_provider(h, cached, preferred)
    if h and any(h in s for s in cached.values()):
        logger.info("Debrid auto-pick: %s has %s cached", PROVIDER_LABELS[choice], h[:12])
    return choice


async def fresh_audio_urls(
    provider: str,
    torrent_id: str | None,
    magnet_link: str | None,
    audio_re,
) -> list[str] | None:
    """Fresh, playable CDN URLs for a torrent's audio files on the given provider.

    Fast path: torrent still exists in the account -> re-unrestrict/request links.
    Slow path: re-add the magnet (instant when cached) and resolve from scratch.
    """
    client = get_client(provider)
    info = None
    if torrent_id:
        try:
            candidate = await client.get_torrent_info(torrent_id)
            if candidate.get("status") == "downloaded" and candidate.get("links"):
                info = candidate
        except Exception as e:
            logger.info("%s torrent %s no longer available (%s); falling back to magnet",
                        provider, torrent_id, e)

    if info is None and magnet_link:
        try:
            result = await client.add_magnet(magnet_link)
            new_id = result.get("id")
            if not new_id:
                return None
            pre = await client.get_torrent_info(new_id)
            audio_ids = []
            for f in pre.get("files", []):
                path = f.get("path", "")
                fname = path.rsplit("/", 1)[-1] if "/" in path else path
                if audio_re.search(fname):
                    audio_ids.append(str(f.get("id")))
            await client.select_files(new_id, ",".join(audio_ids) if audio_ids else "all")
            info = await client.poll_until_ready(new_id, interval=2, timeout=90)
        except Exception as e:
            logger.warning("%s magnet re-add for link refresh failed: %s", provider, e)
            return None

    if info is None:
        return None

    links = info.get("links", [])
    if not links:
        return None
    resolved = await asyncio.gather(
        *[client.unrestrict_link(link) for link in links],
        return_exceptions=True,
    )
    urls: list[str] = []
    for link, u in zip(links, resolved):
        if isinstance(u, Exception):
            continue
        # Torbox CDN URLs may not contain the filename — filter on the
        # pseudo-link filename instead.
        name_source = link if link.startswith(torbox.PSEUDO_SCHEME) else u
        filename = name_source.rsplit("/", 1)[-1].split("?")[0]
        if audio_re.search(filename):
            urls.append(u)
    return urls or None


def link_filename(link: str, resolved_url: str) -> str:
    """Best-effort filename for a provider link (handles torbox:// pseudo-links)."""
    parsed = torbox.parse_pseudo_link(link)
    if parsed and parsed[2]:
        return parsed[2]
    return resolved_url.rsplit("/", 1)[-1].split("?")[0]
