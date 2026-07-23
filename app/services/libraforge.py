"""HTTP client for the sibling LibraForge stack (Metadata / M4B / Folder Forge).

LibraForge is AGPL — we only call its HTTP API; we never import its code.
There is no API key; access relies on Docker/LAN network controls.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Awaitable

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

DEFAULT_FIXER_SCRIPT = "audible-metadata-fixer-v5.py"
DEFAULT_ORGANIZER_SCRIPT = "organize-audiobooks-by-metadata-v3_13.py"
DEFAULT_NAMING_TEMPLATE = "{author}/{series} [{edition}]/{title}/{filename}"

ProgressCb = Callable[[dict[str, Any]], Awaitable[None]] | None


class LibraForgeError(RuntimeError):
    """LibraForge API or job failure."""


def _base_url() -> str:
    url = (settings.libraforge_internal_url or settings.libraforge_url or "").strip().rstrip("/")
    if not url:
        raise LibraForgeError("LIBRAFORGE_INTERNAL_URL / LIBRAFORGE_URL not configured")
    return url


def public_manual_review_url() -> str:
    public = (settings.libraforge_url or "").strip().rstrip("/")
    if not public:
        return ""
    return f"{public}/forge#manual-review"


async def _request(
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    timeout: float = 120.0,
) -> dict[str, Any]:
    url = f"{_base_url()}{path}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(method, url, json=json_body)
    except httpx.HTTPError as e:
        raise LibraForgeError(f"LibraForge unreachable ({path}): {e}") from e
    if resp.status_code >= 400:
        detail = (resp.text or "")[:800]
        raise LibraForgeError(f"LibraForge {method} {path} → HTTP {resp.status_code}: {detail}")
    if not resp.content:
        return {}
    data = resp.json()
    return data if isinstance(data, dict) else {"data": data}


async def get_scripts() -> dict[str, Any]:
    return await _request("GET", "/api/scripts", timeout=30.0)


async def resolve_script_names() -> tuple[str, str]:
    """Return (fixer_script, organizer_script), falling back to known defaults."""
    try:
        data = await get_scripts()
        fixer = (data.get("default_script") or "").strip()
        organizer = (data.get("default_organizer_script") or "").strip()
        if not fixer:
            scripts = data.get("fixer_scripts") or []
            fixer = scripts[-1] if scripts else DEFAULT_FIXER_SCRIPT
        if not organizer:
            scripts = data.get("organizer_scripts") or []
            organizer = scripts[-1] if scripts else DEFAULT_ORGANIZER_SCRIPT
        return fixer or DEFAULT_FIXER_SCRIPT, organizer or DEFAULT_ORGANIZER_SCRIPT
    except LibraForgeError:
        return DEFAULT_FIXER_SCRIPT, DEFAULT_ORGANIZER_SCRIPT


async def get_run(run_id: str) -> dict[str, Any]:
    return await _request("GET", f"/api/runs/{run_id}", timeout=60.0)


async def wait_for_run(
    run_id: str,
    *,
    poll_seconds: float = 3.0,
    timeout_seconds: float | None = None,
    on_progress: ProgressCb = None,
) -> dict[str, Any]:
    """Poll until a LibraForge run finishes. Returns the final report/status dict."""
    deadline = None
    if timeout_seconds is not None and timeout_seconds > 0:
        deadline = asyncio.get_event_loop().time() + timeout_seconds

    terminal = {"completed", "failed", "cancelled", "canceled", "success", "error", "done"}
    while True:
        state = await get_run(run_id)
        status = str(state.get("status") or state.get("phase") or "").lower()
        if on_progress:
            try:
                await on_progress(state)
            except Exception:
                logger.debug("LibraForge progress callback failed", exc_info=True)
        if state.get("done") is True or status in terminal:
            return state
        if deadline is not None and asyncio.get_event_loop().time() >= deadline:
            raise LibraForgeError(f"LibraForge run {run_id} timed out after {timeout_seconds}s")
        await asyncio.sleep(poll_seconds)


async def start_metadata_run(
    target_path: str,
    *,
    apply: bool = True,
    min_score: float | None = None,
    cover_if_missing: bool = True,
    replace_cover: bool = True,
    write_mode: str = "smart",
    limit: int = 50,
    script_name: str | None = None,
) -> str:
    fixer, _ = await resolve_script_names()
    body: dict[str, Any] = {
        "script_name": script_name or fixer,
        "target_path": target_path,
        "apply": apply,
        "backup": False,
        "cover_if_missing": cover_if_missing,
        "replace_cover": replace_cover,
        "min_score": min_score if min_score is not None else settings.libraforge_min_score,
        "limit": limit,
        "write_mode": write_mode,
        "provider": "audible",
        "enable_goodreads_fallback": True,
    }
    data = await _request("POST", "/api/runs", json_body=body, timeout=60.0)
    run_id = str(data.get("id") or "").strip()
    if not run_id:
        raise LibraForgeError(f"Metadata Forge did not return a run id: {data}")
    return run_id


async def start_organizer_run(
    root_path: str,
    *,
    destination_root: str | None = None,
    apply: bool = True,
    naming_template: str | None = None,
    script_name: str | None = None,
) -> str:
    _, organizer = await resolve_script_names()
    template = (naming_template or settings.libraforge_naming_template or DEFAULT_NAMING_TEMPLATE).strip()
    body: dict[str, Any] = {
        "root_path": root_path,
        "destination_root": destination_root or settings.audiobook_dir,
        "script_name": script_name or organizer,
        "apply": apply,
        "remove_empty_dirs": True,
        "acknowledge_no_sidecars": True,
        "naming_template": template,
        "use_default_scheme": False,
    }
    data = await _request("POST", "/api/organizer/runs", json_body=body, timeout=60.0)
    run_id = str(data.get("id") or "").strip()
    if not run_id:
        raise LibraForgeError(f"Folder Forge did not return a run id: {data}")
    return run_id


async def m4b_load(path: str) -> dict[str, Any]:
    return await _request("POST", "/api/m4b/metadata/load", json_body={"path": path}, timeout=120.0)


async def start_m4b_run(
    input_path: str,
    output_path: str,
    *,
    metadata: dict[str, Any] | None = None,
    jobs: int | None = None,
    force: bool = True,
) -> str:
    body: dict[str, Any] = {
        "input_path": input_path,
        "output_path": output_path,
        "save_sidecar": True,
        "metadata": metadata or {},
        "force": force,
        "jobs": jobs if jobs is not None else settings.libraforge_m4b_jobs,
        "audio_codec": "libfdk_aac",
        "audio_bitrate": "128k",
        "audio_samplerate": 44100,
    }
    data = await _request("POST", "/api/m4b/runs", json_body=body, timeout=60.0)
    run_id = str(data.get("id") or "").strip()
    if not run_id:
        raise LibraForgeError(f"M4B tool did not return a run id: {data}")
    return run_id


def metadata_auto_applied(report: dict[str, Any]) -> bool:
    """True when Metadata Forge wrote a full (or equivalent) match for at least one file."""
    cats = report.get("files_by_category") or {}
    if not isinstance(cats, dict):
        cats = {}
    for key in ("mode:full", "status:manual_applied", "status:updated", "status:applied"):
        items = cats.get(key) or []
        if items:
            return True
    stats = report.get("stats") or {}
    breakdown = stats.get("mode_breakdown") or {}
    if isinstance(breakdown, dict) and int(breakdown.get("full") or 0) > 0:
        return True
    # Some reports only expose counters
    for key in ("updated", "applied", "edited", "written"):
        if int(stats.get(key) or 0) > 0:
            return True
    return False


def run_failed(report: dict[str, Any]) -> bool:
    status = str(report.get("status") or "").lower()
    if status in {"failed", "error", "cancelled", "canceled"}:
        return True
    if report.get("returncode") not in (None, 0):
        return True
    if report.get("error"):
        return True
    return False


def quarantine_reason_from_report(report: dict[str, Any]) -> str:
    manual = report.get("manual_review_items") or []
    if isinstance(manual, list) and manual:
        reasons = []
        for item in manual[:5]:
            if isinstance(item, dict):
                reasons.append(str(item.get("reason") or item.get("title") or item.get("path") or "review"))
            else:
                reasons.append(str(item))
        return "Metadata match needs admin review: " + "; ".join(reasons)[:400]
    cats = report.get("files_by_category") or {}
    if isinstance(cats, dict):
        skipped = cats.get("status:skipped") or []
        if skipped:
            first = skipped[0] if isinstance(skipped[0], dict) else {}
            reason = first.get("reason") or first.get("title") or "score below minimum / no match"
            return f"Metadata Forge skipped book ({reason})"
    return "Metadata Forge did not auto-apply a high-confidence match"
