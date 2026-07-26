"""Open Library dump URLs and lightweight remote-change detection (HEAD only)."""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DUMP_URLS = {
    "authors": "https://openlibrary.org/data/ol_dump_authors_latest.txt.gz",
    "works": "https://openlibrary.org/data/ol_dump_works_latest.txt.gz",
    "editions": "https://openlibrary.org/data/ol_dump_editions_latest.txt.gz",
}


def dump_path(dumps_dir: Path, name: str) -> Path:
    return dumps_dir / f"ol_dump_{name}.txt.gz"


def meta_path(dest: Path) -> Path:
    return dest.with_name(dest.name + ".meta.json")


def load_meta(dest: Path) -> dict[str, Any] | None:
    path = meta_path(dest)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def save_meta(dest: Path, remote: dict[str, Any]) -> None:
    payload = {
        "etag": remote.get("etag"),
        "content_length": remote.get("content_length"),
        "last_modified": remote.get("last_modified"),
        "final_url": remote.get("final_url"),
        "source_url": remote.get("source_url"),
        "saved_at": time.time(),
        "local_size": dest.stat().st_size if dest.is_file() else None,
    }
    path = meta_path(dest)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def head_remote(url: str, ua: str, *, timeout: float = 30.0) -> dict[str, Any]:
    """Lightweight remote probe. Prefers HEAD; falls back to ranged GET."""
    headers = {"User-Agent": ua}
    try:
        req = urllib.request.Request(url, headers=headers, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return _headers_to_remote(resp, source_url=url)
    except Exception as head_err:
        logger.debug("HEAD failed for %s (%s); trying ranged GET", url, head_err)
        try:
            req = urllib.request.Request(
                url,
                headers={**headers, "Range": "bytes=0-0"},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                # Drain at most one byte so we don't pull the dump.
                try:
                    resp.read(1)
                except Exception:
                    pass
                return _headers_to_remote(resp, source_url=url)
        except Exception as get_err:
            raise RuntimeError(f"Could not probe {url}: {get_err}") from get_err


def _headers_to_remote(resp: Any, *, source_url: str) -> dict[str, Any]:
    h = resp.headers
    etag = h.get("ETag") or h.get("Etag") or h.get("etag")
    lm = h.get("Last-Modified") or h.get("last-modified")
    cl_raw = h.get("Content-Length") or h.get("content-length")
    # Ranged responses may use Content-Range: bytes 0-0/12345
    cr = h.get("Content-Range") or h.get("content-range") or ""
    content_length: int | None = None
    if cl_raw:
        try:
            content_length = int(cl_raw)
        except ValueError:
            content_length = None
    if content_length is None and "/" in cr:
        try:
            content_length = int(cr.rsplit("/", 1)[-1])
        except ValueError:
            content_length = None
    # A 1-byte ranged GET is not the real size.
    if content_length is not None and content_length <= 1 and "/" not in cr:
        content_length = None
    return {
        "etag": (etag or "").strip() or None,
        "content_length": content_length,
        "last_modified": (lm or "").strip() or None,
        "final_url": getattr(resp, "geturl", lambda: source_url)(),
        "source_url": source_url,
    }


def remote_differs_from_local(
    dest: Path,
    remote: dict[str, Any],
    *,
    local_meta: dict[str, Any] | None = None,
) -> bool:
    """True when remote dump appears newer/different than the local file."""
    if not dest.is_file() or dest.stat().st_size <= 1024:
        return True

    meta = local_meta if local_meta is not None else load_meta(dest)
    local_size = dest.stat().st_size
    remote_etag = remote.get("etag")
    remote_len = remote.get("content_length")
    remote_lm = remote.get("last_modified")

    if meta:
        meta_etag = meta.get("etag")
        meta_lm = meta.get("last_modified")
        # Explicit identity signals first.
        if remote_etag and meta_etag and remote_etag != meta_etag:
            return True
        if remote_lm and meta_lm and remote_lm != meta_lm:
            return True
        if remote_len and remote_len != local_size:
            return True
        if remote_etag and meta_etag and remote_etag == meta_etag:
            return False
        if (
            remote_lm
            and meta_lm
            and remote_lm == meta_lm
            and (not remote_len or remote_len == local_size)
        ):
            return False
        if remote_len and meta.get("content_length") == remote_len and remote_len == local_size:
            # Same size recorded previously; without etag/lm change treat as same.
            if not remote_etag or remote_etag == meta_etag:
                return False
        # Meta present but inconclusive — trust size match when available.
        if remote_len and remote_len == local_size:
            return False
        return bool(remote_etag or remote_lm or remote_len)

    # No sidecar: best-effort size compare only.
    if remote_len and remote_len == local_size:
        return False
    if remote_len and remote_len != local_size:
        return True
    # Unknown remote size and no meta — assume unchanged to avoid surprise re-downloads.
    return False


def check_dumps(
    dumps_dir: Path | str,
    *,
    names: list[str] | None = None,
    ua: str,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Compare remote ``*_latest`` dumps to local files without downloading.

    Returns a summary suitable for Admin status / notifications.
    """
    root = Path(dumps_dir)
    wanted = names or ["authors", "works"]
    changed: list[str] = []
    missing: list[str] = []
    unchanged: list[str] = []
    errors: dict[str, str] = {}
    remotes: dict[str, dict[str, Any]] = {}

    for name in wanted:
        url = DUMP_URLS.get(name)
        if not url:
            errors[name] = "unknown dump name"
            continue
        dest = dump_path(root, name)
        if not dest.is_file() or dest.stat().st_size <= 1024:
            missing.append(name)
        try:
            remote = head_remote(url, ua, timeout=timeout)
            remotes[name] = remote
            if remote_differs_from_local(dest, remote):
                if name not in missing:
                    changed.append(name)
                elif name not in changed:
                    changed.append(name)
            else:
                unchanged.append(name)
        except Exception as e:
            errors[name] = str(e)
            logger.info("OL dump check failed for %s: %s", name, e)

    # Missing local files count as needing an update when remote was reachable.
    for name in missing:
        if name not in changed and name in remotes:
            changed.append(name)

    signature_parts = []
    for name in wanted:
        r = remotes.get(name) or {}
        signature_parts.append(
            f"{name}:{r.get('etag') or ''}:{r.get('content_length') or ''}:{r.get('last_modified') or ''}"
        )
    signature = "|".join(signature_parts)

    return {
        "checked_at": time.time(),
        "dumps_dir": str(root),
        "names": wanted,
        "changed": changed,
        "missing": missing,
        "unchanged": unchanged,
        "errors": errors,
        "remotes": remotes,
        "update_available": bool(changed),
        "signature": signature,
    }


def should_redownload(dest: Path, url: str, ua: str, *, force: bool = False) -> bool:
    """Whether ``_download`` should fetch ``url`` into ``dest``."""
    if force:
        return True
    if not dest.is_file() or dest.stat().st_size <= 1024:
        return True
    try:
        remote = head_remote(url, ua)
    except Exception as e:
        logger.warning("Could not probe %s (%s); keeping local file", url, e)
        return False
    return remote_differs_from_local(dest, remote)
