"""Admin health probes for external services and local dependencies."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


def _err(exc: BaseException) -> str:
    return str(exc)[:160]


async def _probe_real_debrid() -> dict[str, Any]:
    from app.services import real_debrid

    try:
        info = await real_debrid.get_user_info()
        if not info:
            return {"configured": True, "connected": False, "error": "No response"}
        return {
            "configured": True,
            "connected": True,
            "username": info.get("username"),
            "premium": info.get("premium"),
            "points": info.get("points"),
        }
    except Exception as e:
        return {"configured": True, "connected": False, "error": _err(e)}


async def _probe_torbox() -> dict[str, Any]:
    from app.services import torbox
    from app.services import debrid_tokens

    token = (debrid_tokens.torbox_token() or "").strip()
    if not token:
        return {"configured": False, "connected": False, "error": "No API token"}
    try:
        info = await torbox.get_user_info()
        if not info:
            return {"configured": True, "connected": False, "error": "No response"}
        return {
            "configured": True,
            "connected": True,
            "username": info.get("email") or info.get("customer"),
            "plan": info.get("plan"),
        }
    except Exception as e:
        return {"configured": True, "connected": False, "error": _err(e)}


async def _probe_abs() -> dict[str, Any]:
    from app.services import audiobookshelf

    settings = get_settings()
    ok = await audiobookshelf.health_check()
    return {
        "configured": bool(settings.abs_url),
        "connected": ok,
        "url": settings.abs_url,
    }


async def _probe_kavita() -> dict[str, Any]:
    from app.services import kavita

    settings = get_settings()
    ok = await kavita.health_check()
    return {
        "configured": bool(settings.kavita_url),
        "connected": ok,
        "url": settings.kavita_url,
    }


async def _probe_prowlarr() -> dict[str, Any]:
    settings = get_settings()
    url = (settings.prowlarr_url or "").rstrip("/")
    key = (settings.prowlarr_api_key or "").strip()
    if not url or not key:
        return {
            "configured": False,
            "connected": False,
            "url": url or None,
            "error": "URL or API key missing",
        }
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(
                f"{url}/api/v1/system/status",
                headers={"X-Api-Key": key},
            )
            if resp.status_code != 200:
                return {
                    "configured": True,
                    "connected": False,
                    "url": url,
                    "error": f"HTTP {resp.status_code}",
                }
            data = resp.json() if resp.content else {}
            idx = await client.get(
                f"{url}/api/v1/indexer",
                headers={"X-Api-Key": key},
            )
            indexers = 0
            if idx.status_code == 200 and isinstance(idx.json(), list):
                indexers = sum(1 for i in idx.json() if i.get("enable") is not False)
            return {
                "configured": True,
                "connected": True,
                "url": url,
                "version": data.get("version") or data.get("appName"),
                "indexers": indexers,
            }
    except Exception as e:
        return {"configured": True, "connected": False, "url": url, "error": _err(e)}


async def _probe_jackett() -> dict[str, Any]:
    settings = get_settings()
    url = (settings.jackett_url or "").rstrip("/")
    key = (settings.jackett_api_key or "").strip()
    if not url:
        return {"configured": False, "connected": False, "error": "JACKETT_URL not set"}
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            if key:
                resp = await client.get(
                    f"{url}/api/v2.0/indexers/all/results/torznab",
                    params={"apikey": key, "t": "indexers", "configured": "true"},
                )
                if resp.status_code == 200:
                    return {
                        "configured": True,
                        "connected": True,
                        "url": url,
                        "apiKey": True,
                    }
            resp = await client.get(f"{url}/")
            ok = resp.status_code < 500
            return {
                "configured": bool(key),
                "connected": ok,
                "url": url,
                "apiKey": bool(key),
                "error": None if ok else f"HTTP {resp.status_code}",
            }
    except Exception as e:
        return {
            "configured": bool(key),
            "connected": False,
            "url": url,
            "apiKey": bool(key),
            "error": _err(e),
        }


async def _probe_flaresolverr() -> dict[str, Any]:
    settings = get_settings()
    flare = (settings.flaresolverr_url or "").rstrip("/")
    if not flare:
        return {"configured": False, "connected": False, "error": "FLARESOLVERR_URL not set"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(flare if flare.endswith("/") else f"{flare}/")
            if resp.status_code >= 500:
                return {
                    "configured": True,
                    "connected": False,
                    "url": flare,
                    "error": f"HTTP {resp.status_code}",
                }
            data = resp.json() if resp.content else {}
            return {
                "configured": True,
                "connected": True,
                "url": flare,
                "version": data.get("version"),
            }
    except Exception as e:
        return {"configured": True, "connected": False, "url": flare, "error": _err(e)}


async def _probe_mullvad_proxy() -> dict[str, Any]:
    settings = get_settings()
    proxy = (settings.abb_proxy_url or "").strip()
    if not proxy:
        return {
            "configured": False,
            "connected": False,
            "error": "ABB_PROXY_URL not set",
        }
    try:
        async with httpx.AsyncClient(proxy=proxy, timeout=12.0) as client:
            resp = await client.get("https://am.i.mullvad.net/json")
            if resp.status_code != 200:
                return {
                    "configured": True,
                    "connected": False,
                    "proxy": proxy,
                    "error": f"HTTP {resp.status_code}",
                }
            data = resp.json()
            return {
                "configured": True,
                "connected": True,
                "proxy": proxy,
                "exitIp": data.get("ip"),
                "mullvadExit": bool(data.get("mullvad_exit_ip")),
                "country": data.get("country") or data.get("city"),
            }
    except Exception as e:
        return {
            "configured": True,
            "connected": False,
            "proxy": proxy,
            "error": _err(e),
        }


async def _probe_knaben() -> dict[str, Any]:
    from app.services import knaben

    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            # Same RSS path the scraper uses (audiobook category, small page)
            url = knaben._rss_url_for_category(1_003_000, size=5)
            resp = await client.get(url)
            if resp.status_code != 200:
                return {
                    "configured": True,
                    "connected": False,
                    "url": knaben.RSS_URL,
                    "error": f"RSS HTTP {resp.status_code}",
                }
            body = resp.text.lower()
            ok = "<item>" in body or "<rss" in body
            return {
                "configured": True,
                "connected": ok,
                "url": knaben.RSS_URL,
                "error": None if ok else "RSS response missing items",
            }
    except Exception as e:
        return {"configured": True, "connected": False, "error": _err(e)}


async def _probe_ol_catalog() -> dict[str, Any]:
    settings = get_settings()
    path = Path(settings.ol_catalog_db_path or "")
    if not settings.ol_catalog_enabled:
        return {
            "configured": False,
            "connected": False,
            "path": str(path) if path else None,
            "error": "OL catalog disabled",
        }
    if not path.is_file():
        return {
            "configured": True,
            "connected": False,
            "path": str(path),
            "error": "Database file missing",
        }
    try:
        import aiosqlite

        async with aiosqlite.connect(str(path)) as conn:
            async with conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='works'"
            ) as cur:
                row = await cur.fetchone()
            if not row:
                return {
                    "configured": True,
                    "connected": False,
                    "path": str(path),
                    "error": "works table missing",
                }
            async with conn.execute("SELECT COUNT(*) FROM works") as cur:
                count = (await cur.fetchone())[0]
        return {
            "configured": True,
            "connected": True,
            "path": str(path),
            "works": int(count or 0),
        }
    except Exception as e:
        return {
            "configured": True,
            "connected": False,
            "path": str(path),
            "error": _err(e),
        }


async def _probe_nyt() -> dict[str, Any]:
    from app.services import nyt_books

    key = (await nyt_books.get_api_key() or "").strip()
    if not key:
        return {"configured": False, "connected": False, "error": "No API key"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{nyt_books.NYT_BASE}/overview.json",
                params={"api-key": key},
            )
            if resp.status_code != 200:
                return {
                    "configured": True,
                    "connected": False,
                    "error": f"HTTP {resp.status_code}",
                }
            data = resp.json()
            lists = (data.get("results") or {}).get("lists") or []
            return {
                "configured": True,
                "connected": True,
                "lists": len(lists) if isinstance(lists, list) else 0,
            }
    except Exception as e:
        return {"configured": True, "connected": False, "error": _err(e)}


async def _probe_disk() -> dict[str, Any]:
    import shutil

    settings = get_settings()
    try:
        disk = shutil.disk_usage(settings.audiobook_dir)
        return {
            "configured": True,
            "connected": True,
            "path": settings.audiobook_dir,
            "total_gb": round(disk.total / (1024**3), 1),
            "used_gb": round(disk.used / (1024**3), 1),
            "free_gb": round(disk.free / (1024**3), 1),
        }
    except Exception as e:
        return {"configured": True, "connected": False, "error": _err(e)}


async def _probe_libraforge() -> dict[str, Any]:
    """Probe sibling LibraForge stack. Fail-open — never raises."""
    settings = get_settings()
    public = (settings.libraforge_url or "").strip().rstrip("/")
    internal = (settings.libraforge_internal_url or "").strip().rstrip("/")
    if not public and not internal:
        return {
            "configured": False,
            "connected": False,
            "url": None,
            "internal_url": None,
            "error": "LIBRAFORGE_URL not set",
        }
    probe_base = internal or public
    try:
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
            resp = await client.get(f"{probe_base}/health")
            if resp.status_code >= 500 or resp.status_code == 404:
                resp = await client.get(f"{probe_base}/")
            ok = resp.status_code < 500
            return {
                "configured": bool(public or internal),
                "connected": ok,
                "url": public or None,
                "internal_url": internal or None,
                "error": None if ok else f"HTTP {resp.status_code}",
            }
    except Exception as e:
        return {
            "configured": bool(public or internal),
            "connected": False,
            "url": public or None,
            "internal_url": internal or None,
            "error": _err(e),
        }


async def collect_system_health() -> dict[str, Any]:
    """Run all connection probes in parallel (short timeouts)."""
    from app.services.debrid_tokens import apply_server_debrid_tokens

    try:
        await apply_server_debrid_tokens()
    except Exception:
        pass

    names = [
        "real_debrid",
        "torbox",
        "audiobookshelf",
        "kavita",
        "prowlarr",
        "jackett",
        "flaresolverr",
        "mullvad_proxy",
        "knaben",
        "ol_catalog",
        "nyt",
        "disk",
        "libraforge",
    ]
    coros = [
        _probe_real_debrid(),
        _probe_torbox(),
        _probe_abs(),
        _probe_kavita(),
        _probe_prowlarr(),
        _probe_jackett(),
        _probe_flaresolverr(),
        _probe_mullvad_proxy(),
        _probe_knaben(),
        _probe_ol_catalog(),
        _probe_nyt(),
        _probe_disk(),
        _probe_libraforge(),
    ]
    results = await asyncio.gather(*coros, return_exceptions=True)
    out: dict[str, Any] = {}
    for name, result in zip(names, results):
        if isinstance(result, Exception):
            out[name] = {"configured": True, "connected": False, "error": _err(result)}
        else:
            out[name] = result
    return out
