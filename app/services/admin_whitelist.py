"""LibraForge admin IP allowlist file (for NPM access-list sync)."""

from __future__ import annotations

import ipaddress
import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import User

logger = logging.getLogger(__name__)

HEADER = (
    "# LibraForge admin client IPs (auto-managed by Library Site).\n"
    "# Add these to Nginx Proxy Manager access list home-or-vpn\n"
    "# (or your forge proxy allowlist) for remote forge editing.\n"
)


def whitelist_path() -> Path:
    url = get_settings().database_url or ""
    if "sqlite" in url and "///" in url:
        data_dir = Path(url.split("///")[-1]).resolve().parent
    else:
        data_dir = Path("data").resolve()
    return data_dir / "libraforge_admin_whitelist.txt"


def normalize_ip(raw: str | None) -> str | None:
    s = (raw or "").strip()
    if not s:
        return None
    if s.startswith("["):
        end = s.find("]")
        if end > 0:
            s = s[1:end]
    elif s.count(":") == 1 and "." in s:
        s = s.split(":", 1)[0]
    try:
        return str(ipaddress.ip_address(s))
    except ValueError:
        return None


def client_ip_from_request(request) -> str | None:
    """Best-effort client IP (prefer first X-Forwarded-For hop)."""
    xff = (request.headers.get("x-forwarded-for") or "").strip()
    if xff:
        first = xff.split(",")[0].strip()
        ip = normalize_ip(first)
        if ip:
            return ip
    real = (request.headers.get("x-real-ip") or "").strip()
    ip = normalize_ip(real)
    if ip:
        return ip
    if request.client and request.client.host:
        return normalize_ip(request.client.host)
    return None


async def sync_admin_ips(db: AsyncSession) -> dict:
    """Rewrite whitelist file from active admins last_client_ip values."""
    rows = (
        await db.execute(
            select(User).where(User.role == "admin", User.is_active.is_(True))
        )
    ).scalars().all()
    ips: list[str] = []
    seen: set[str] = set()
    for u in rows:
        ip = normalize_ip(getattr(u, "last_client_ip", None))
        if not ip or ip in seen:
            continue
        seen.add(ip)
        ips.append(ip)
    path = whitelist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    body = HEADER + ("\n".join(ips) + ("\n" if ips else ""))
    path.write_text(body, encoding="utf-8")
    logger.info("Wrote LibraForge admin whitelist (%s IPs) -> %s", len(ips), path)
    return {
        "ips": ips,
        "file_path": str(path),
        "count": len(ips),
        "note": (
            "These IPs are written for the LibraForge NPM access list (home-or-vpn). "
            "Add new IPs to that access list when promoting remote admins."
        ),
    }


def read_whitelist() -> dict:
    path = whitelist_path()
    ips: list[str] = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            ip = normalize_ip(s)
            if ip:
                ips.append(ip)
    return {
        "ips": ips,
        "file_path": str(path),
        "note": (
            "These IPs are written for the LibraForge NPM access list (home-or-vpn). "
            "Add new IPs to that access list when promoting remote admins."
        ),
    }