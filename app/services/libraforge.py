"""HTTP client for the sibling LibraForge stack (Metadata / M4B / Folder Forge).

LibraForge is AGPL â€” we only call its HTTP API; we never import its code.
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
AbortCb = Callable[[], Awaitable[bool] | bool] | None


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
        raise LibraForgeError(f"LibraForge {method} {path} â†’ HTTP {resp.status_code}: {detail}")
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


async def cancel_run(run_id: str) -> None:
    """Best-effort stop of an in-flight LibraForge run (DELETE or POST cancel)."""
    run_id = (run_id or "").strip()
    if not run_id:
        return
    for method, path in (
        ("DELETE", f"/api/runs/{run_id}"),
        ("POST", f"/api/runs/{run_id}/cancel"),
    ):
        try:
            await _request(method, path, timeout=15.0)
            return
        except LibraForgeError:
            continue
        except Exception:
            logger.debug("LibraForge cancel via %s %s failed", method, path, exc_info=True)


async def wait_for_run(
    run_id: str,
    *,
    poll_seconds: float = 3.0,
    timeout_seconds: float | None = None,
    on_progress: ProgressCb = None,
    should_abort: AbortCb = None,
) -> dict[str, Any]:
    """Poll until a LibraForge run finishes. Returns the final report/status dict.

    ``should_abort``: optional async/sync predicate; when true, raises
    ``LibraForgeError("cancelled")`` so callers can stop without waiting out the run.
    """
    deadline = None
    if timeout_seconds is not None and timeout_seconds > 0:
        deadline = asyncio.get_event_loop().time() + timeout_seconds

    # Only treat run *status* as terminal â€” phase labels like "complete" appear
    # while work is still flushing and must not short-circuit the poll loop.
    terminal = {"completed", "failed", "cancelled", "canceled", "success", "error", "done"}
    while True:
        if should_abort is not None:
            abort = should_abort()
            if asyncio.iscoroutine(abort):
                abort = await abort
            if abort:
                try:
                    await cancel_run(run_id)
                except Exception:
                    logger.debug("LibraForge cancel_run(%s) failed", run_id, exc_info=True)
                raise LibraForgeError("cancelled")
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
    """Prefer the persisted report once Pass-2 write facets are present.

    Match facets (``mode:full`` / ``status:matched``) land during Pass 1. Apply
    facets (``write:written``) land during Pass 2. Returning too early makes a
    1.0 match look "done" before tags/covers are written.
    """
    def _has_write_signal(rep: dict[str, Any]) -> bool:
        return metadata_auto_applied(rep)

    if _has_write_signal(state) and (state.get("report_items") or state.get("items")):
        return state

    # Settle for disk flush + late WRITE_ACTION_JSON merges.
    best = state
    for _ in range(10):
        await asyncio.sleep(0.5)
        try:
            disk = await get_run(run_id)
        except LibraForgeError:
            break
        best = disk
        if _has_write_signal(disk):
            return disk
        # No match either â€” nothing to wait for (skip/fail path).
        cats = _category_map(disk)
        if _has_category_items(cats, "status:skipped", "write:write_skipped", "mode:none"):
            return disk
        if disk.get("report_items") or disk.get("items") or disk.get("files_by_category"):
            # Keep looping briefly in case write facets arrive next.
            continue
    return best


async def start_metadata_run(
    target_path: str,
    *,
    apply: bool = True,
    min_score: float | None = None,
    # Above-threshold match = identity is correct, not that existing tags/cover are.
    # Always full-overwrite tags and replace embedded cover from the match.
    cover_if_missing: bool = False,
    replace_cover: bool = True,
    write_mode: str = "overwrite",
    limit: int = 50,
    script_name: str | None = None,
) -> str:
    fixer, _ = await resolve_script_names()
    body: dict[str, Any] = {
        "script_name": script_name or fixer,
        "target_path": target_path,
        "apply": apply,
        "backup": False,
        # Maps to Metadata Forge CLI: --apply --write-mode overwrite --replace-cover
        # (not --cover-if-missing / smart â€” those leave torrent tags/covers in place).
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

async def manual_review_load(
    path: str,
    *,
    script_name: str | None = None,
    use_backup_tags: bool = False,
) -> dict[str, Any]:
    """Load Manual Review clues for a staging file/folder (LibraForge)."""
    fixer, _ = await resolve_script_names()
    body: dict[str, Any] = {
        "path": path,
        "script_name": script_name or fixer,
        "use_backup_tags": use_backup_tags,
    }
    return await _request("POST", "/api/manual-review/load", json_body=body, timeout=120.0)


async def manual_review_search(
    *,
    query: str = "",
    metadata: dict[str, Any] | None = None,
    limit: int = 10,
    script_name: str | None = None,
) -> dict[str, Any]:
    """Search Audible candidates via LibraForge Manual Review / M4B search."""
    fixer, _ = await resolve_script_names()
    body: dict[str, Any] = {
        "query": query or "",
        "metadata": metadata or {},
        "limit": max(1, min(int(limit or 10), 25)),
        "script_name": script_name or fixer,
        "auth_file": "/auth/audible-metadata.json",
    }
    return await _request("POST", "/api/m4b/search", json_body=body, timeout=120.0)


async def manual_review_apply(
    *,
    path: str,
    selected_result: dict[str, Any],
    edit_mode: str = "full",
    write_policy: str = "overwrite",
    replace_cover: bool = True,
    cover_if_missing: bool = False,
    backup: bool = False,
    metadata_override: dict[str, Any] | None = None,
    script_name: str | None = None,
) -> dict[str, Any]:
    """Apply a Manual Review match to staging (overwrite tags + optional cover)."""
    fixer, _ = await resolve_script_names()
    body: dict[str, Any] = {
        "path": path,
        "script_name": script_name or fixer,
        "selected_result": selected_result,
        "edit_mode": edit_mode or "full",
        "backup": backup,
        "cover_if_missing": cover_if_missing,
        "replace_cover": replace_cover,
        "writer": "auto",
        "metadata_override": metadata_override or {},
        "write_policy": write_policy or "overwrite",
    }
    return await _request("POST", "/api/manual-review/apply", json_body=body, timeout=300.0)


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
    """True only when Metadata Forge actually wrote tags / metadata.json.

    A perfect score / ``mode:full`` / ``status:matched`` is NOT enough. Pass-1
    match results appear before Pass-2 writes; treating match as apply lets the
    pipeline continue with stale tags. Require write evidence.
    """
    cats = _category_map(report)

    # Written / applied facets from the API (live or disk report).
    if _has_category_items(
        cats,
        "write:written",
        "status:manual_applied",
        "status:updated",
        "status:applied",
    ):
        return True

    report_items = report.get("report_items") or []
    if isinstance(report_items, list) and report_items:
        for item in report_items:
            if not isinstance(item, dict):
                continue
            action = str(item.get("write_action") or "").lower()
            status = str(item.get("status") or "").lower()
            if action in {"written", "updated", "applied"}:
                return True
            if status in {"updated", "applied", "manual_applied"}:
                return True
            if item.get("was_manually_applied"):
                return True
        # Explicit per-item report with zero writes â†’ not applied (even if
        # mode:full / score 1.0). Dry-run "would_write" also lands here.
        return False

    stats = report.get("stats") or {}
    if not isinstance(stats, dict):
        return False

    # Legacy counters only â€” never trust mode_breakdown.full alone.
    for key in ("updated", "applied", "edited", "written"):
        if int(stats.get(key) or 0) > 0:
            return True
    return False


def metadata_matched_without_apply(report: dict[str, Any]) -> bool:
    """True when a high-confidence full match exists but tags were not written.

    Low-score / mode:none skips are handled by skip_reasons messaging instead.
    """
    if metadata_auto_applied(report):
        return False
    cats = _category_map(report)
    stats = report.get("stats") or {}
    breakdown = stats.get("mode_breakdown") if isinstance(stats, dict) else {}
    full = int(breakdown.get("full") or 0) if isinstance(breakdown, dict) else 0
    if full > 0 or _has_category_items(cats, "mode:full"):
        return True
    for item in report.get("report_items") or []:
        if not isinstance(item, dict):
            continue
        try:
            score = float(item.get("score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        mode = str(item.get("mode") or "").lower()
        status = str(item.get("status") or "").lower()
        action = str(item.get("write_action") or "").lower()
        if status == "skipped" and mode not in {"full", ""}:
            continue
        if mode == "full" or (score >= 0.7 and status in {"matched", "updated", ""}):
            if action in {"", "would_write", "write_skipped", "no_op", "smart_skipped"}:
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
    if metadata_matched_without_apply(report):
        best = None
        for item in report.get("report_items") or []:
            if not isinstance(item, dict):
                continue
            try:
                score = float(item.get("score") or 0)
            except (TypeError, ValueError):
                score = 0.0
            action = str(item.get("write_action") or "none")
            title = ((item.get("match") or {}).get("title") if isinstance(item.get("match"), dict) else None) or item.get("path")
            if best is None or score > best[0]:
                best = (score, action, title)
        if best:
            return (
                f"Metadata Forge matched (score={best[0]}) but did not apply tags "
                f"(write_action={best[1]}, title={best[2]!s}). Quarantining before M4B/Folder Forge."
            )[:500]
        return (
            "Metadata Forge matched a book but did not write tags/covers "
            "(apply missing or incomplete). Quarantining before M4B/Folder Forge."
        )

    stats = report.get("stats") or {}
    if isinstance(stats, dict):
        skip_reasons = stats.get("skip_reasons") or {}
        if isinstance(skip_reasons, dict) and skip_reasons:
            parts = [f"{k} Ã—{v}" for k, v in list(skip_reasons.items())[:4]]
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
