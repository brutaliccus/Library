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

    # Only treat run *status* as terminal — phase labels like "complete" appear
    # while work is still flushing and must not short-circuit the poll loop.
    terminal = {"completed", "failed", "cancelled", "canceled", "success", "error", "done"}
    while True:
        state = await get_run(run_id)
        status = str(state.get("status") or "").lower()
        if on_progress:
            try:
                await on_progress(state)
            except Exception:
                logger.debug("LibraForge progress callback failed", exc_info=True)
        if state.get("done") is True or status in terminal:
            # Live in-memory responses omit report_items; prefer the on-disk
            # report once available so auto-apply / quarantine checks see writes.
            return await _finalize_run_report(run_id, state)
        if deadline is not None and asyncio.get_event_loop().time() >= deadline:
            raise LibraForgeError(f"LibraForge run {run_id} timed out after {timeout_seconds}s")
        await asyncio.sleep(poll_seconds)


async def _finalize_run_report(run_id: str, state: dict[str, Any]) -> dict[str, Any]:
    """Prefer the persisted report JSON when the live poll payload is thin."""
    if state.get("report_items") or state.get("items"):
        return state
    # Brief settle so LibraForge can flush write_final_report to disk.
    for _ in range(5):
        await asyncio.sleep(0.4)
        try:
            disk = await get_run(run_id)
        except LibraForgeError:
            break
        if disk.get("report_items") or disk.get("items") or disk.get("files_by_category"):
            return disk
    return state


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


def _category_map(report: dict[str, Any]) -> dict[str, Any]:
    """LibraForge exposes facets as files_by_category (live) or categories (disk)."""
    cats = report.get("files_by_category")
    if isinstance(cats, dict) and cats:
        return cats
    cats = report.get("categories")
    return cats if isinstance(cats, dict) else {}


def _has_category_items(cats: dict[str, Any], *keys: str) -> bool:
    for key in keys:
        items = cats.get(key) or []
        if items:
            return True
    return False


def metadata_auto_applied(report: dict[str, Any]) -> bool:
    """True when Metadata Forge wrote a full (or equivalent) match for at least one file.

    ``status:matched`` alone is NOT enough — that can mean a weak candidate was
    considered and then skipped (write:write_skipped). Require write evidence or
    an explicit applied/updated status.
    """
    cats = _category_map(report)

    # Strongest signal: tags / metadata.json were actually written.
    if _has_category_items(cats, "write:written", "status:manual_applied", "status:updated", "status:applied"):
        return True

    # mode:full without a write facet can appear mid-run; only trust it when
    # there is no contradictory skip/manual-review payload.
    report_items = report.get("report_items") or []
    if isinstance(report_items, list) and report_items:
        written = 0
        for item in report_items:
            if not isinstance(item, dict):
                continue
            action = str(item.get("write_action") or "").lower()
            status = str(item.get("status") or "").lower()
            if action in {"written", "updated", "applied"} or status in {
                "updated",
                "applied",
                "manual_applied",
            }:
                written += 1
            elif item.get("was_manually_applied"):
                written += 1
        if written > 0:
            return True
        # Explicit per-item report with zero writes → not auto-applied.
        return False

    stats = report.get("stats") or {}
    if not isinstance(stats, dict):
        stats = {}

    for key in ("updated", "applied", "edited", "written"):
        if int(stats.get(key) or 0) > 0:
            return True

    matched = int(stats.get("matched") or 0)
    skipped = int(stats.get("skipped") or 0)
    breakdown = stats.get("mode_breakdown") or {}
    full = int(breakdown.get("full") or 0) if isinstance(breakdown, dict) else 0
    manual = report.get("manual_review_items") or []

    # Full matches counted and nothing skipped / queued for review.
    if full > 0 and matched > 0 and skipped == 0 and not manual:
        if _has_category_items(cats, "mode:full"):
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


def organizer_moved_files(report: dict[str, Any]) -> bool:
    """True when Folder Forge actually moved at least one book into the library."""
    stats = report.get("stats") or {}
    if not isinstance(stats, dict):
        return False
    if int(stats.get("moves_succeeded") or 0) > 0:
        return True
    moves = stats.get("move_items") or []
    return isinstance(moves, list) and len(moves) > 0


def quarantine_reason_from_report(report: dict[str, Any]) -> str:
    stats = report.get("stats") or {}
    if isinstance(stats, dict):
        skip_reasons = stats.get("skip_reasons") or {}
        if isinstance(skip_reasons, dict) and skip_reasons:
            parts = [f"{k} ×{v}" for k, v in list(skip_reasons.items())[:4]]
            return "Metadata Forge did not auto-apply: " + "; ".join(parts)[:400]

    report_items = report.get("report_items") or []
    if isinstance(report_items, list):
        for item in report_items:
            if not isinstance(item, dict):
                continue
            skip = (item.get("skip_reason") or "").strip()
            if skip:
                score = item.get("score")
                score_bit = f" (score={score})" if score not in (None, "", 0, 0.0) else ""
                return f"Metadata Forge skipped book: {skip}{score_bit}"[:500]

    manual = report.get("manual_review_items") or []
    if isinstance(manual, list) and manual:
        reasons = []
        for item in manual[:5]:
            if isinstance(item, dict):
                raw = item.get("reasons") or item.get("reason") or item.get("title")
                if isinstance(raw, list):
                    raw = ", ".join(str(x) for x in raw if x)
                reasons.append(str(raw or item.get("path") or "review"))
            else:
                reasons.append(str(item))
        return "Metadata match needs admin review: " + "; ".join(reasons)[:400]

    cats = _category_map(report)
    skipped = cats.get("status:skipped") or []
    if skipped:
        first = skipped[0] if isinstance(skipped[0], dict) else {}
        reason = first.get("reason") or first.get("title") or "score below minimum / no match"
        return f"Metadata Forge skipped book ({reason})"
    return "Metadata Forge did not auto-apply a high-confidence match"
