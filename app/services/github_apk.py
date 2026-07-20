"""Resolve the latest Library Android APK from a GitHub Release."""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx

from app.services import instance_settings

logger = logging.getLogger(__name__)

_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
_CACHE_TTL = 120.0

_VERSION_CODE_RE = re.compile(
    r"(?im)^\s*versionCode\s*[:=]\s*(\d+)\s*$|^\s*version_code\s*[:=]\s*(\d+)\s*$"
)
_ASSET_CODE_RE = re.compile(r"(?i)(?:^|[._-])(\d+)\.apk$")
_TAG_NAME_RE = re.compile(r"(?i)^(?:android[-_]?)?v?(.+)$")


async def _repo() -> str:
    raw = (await instance_settings.get_effective("config.android_apk_github_repo") or "").strip()
    return raw or "brutaliccus/Library"


async def _token() -> str:
    return (await instance_settings.get_effective("config.github_token") or "").strip()


def _parse_version_name(tag: str, release_name: str) -> str:
    tag = (tag or "").strip()
    m = _TAG_NAME_RE.match(tag)
    if m and m.group(1):
        return m.group(1).strip()
    name = (release_name or "").strip()
    if name:
        # "Library 1.5" → 1.5
        parts = name.split()
        if parts:
            return parts[-1].lstrip("v")
    return tag or "latest"


def _parse_version_code(body: str, asset_name: str, tag: str) -> int | None:
    if body:
        m = _VERSION_CODE_RE.search(body)
        if m:
            return int(m.group(1) or m.group(2))
    m = _ASSET_CODE_RE.search(asset_name or "")
    if m:
        # Prefer codes that look like Android versionCode (not years).
        code = int(m.group(1))
        if code < 10_000:
            return code
    # Tag android-v1.5+6 or v1.5+6
    plus = re.search(r"\+(\d+)$", tag or "")
    if plus:
        return int(plus.group(1))
    return None


def _pick_apk_asset(assets: list[dict]) -> dict | None:
    apks = [
        a
        for a in assets
        if isinstance(a, dict) and str(a.get("name") or "").lower().endswith(".apk")
    ]
    if not apks:
        return None
    # Prefer Library*.apk over generic names.
    for a in apks:
        name = str(a.get("name") or "").lower()
        if "library" in name:
            return a
    return apks[0]


async def fetch_latest_android_apk(*, force: bool = False) -> dict[str, Any] | None:
    """Return latest public GitHub Release APK metadata, or None if unavailable."""
    repo = await _repo()
    cache_key = f"latest:{repo}"
    if not force:
        hit = _cache.get(cache_key)
        if hit and (time.monotonic() - hit[0]) < _CACHE_TTL:
            return hit[1]

    url = f"https://api.github.com/repos/{repo}/releases/latest"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Library-Site-AppUpdate",
    }
    token = await _token()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 404:
                _cache[cache_key] = (time.monotonic(), None)
                return None
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.warning("GitHub latest release fetch failed for %s: %s", repo, e)
        raise

    if data.get("draft") or data.get("prerelease"):
        # /releases/latest already skips drafts; keep guard for safety.
        _cache[cache_key] = (time.monotonic(), None)
        return None

    asset = _pick_apk_asset(data.get("assets") or [])
    if not asset or not asset.get("browser_download_url"):
        _cache[cache_key] = (time.monotonic(), None)
        return None

    tag = str(data.get("tag_name") or "")
    body = str(data.get("body") or "")
    asset_name = str(asset.get("name") or "Library.apk")
    version_name = _parse_version_name(tag, str(data.get("name") or ""))
    version_code = _parse_version_code(body, asset_name, tag)
    published = str(data.get("published_at") or data.get("created_at") or "")
    release_id = data.get("id")

    result: dict[str, Any] = {
        "fileName": asset_name,
        "sizeBytes": asset.get("size"),
        "downloadUrl": asset["browser_download_url"],
        "releaseUrl": data.get("html_url") or f"https://github.com/{repo}/releases",
        "githubRepo": repo,
        "tagName": tag,
        "versionName": version_name,
        "versionCode": version_code,
        "publishedAt": published,
        # Stable identity for dismiss / "already installed" (like Drive modifiedTime).
        "releaseKey": published or (f"id:{release_id}" if release_id else tag),
    }
    _cache[cache_key] = (time.monotonic(), result)
    return result
