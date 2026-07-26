"""Background builder for the local Open Library catalog database.

Runs ``scripts/ol_import_dumps.py`` as a subprocess and exposes status for the
Admin → Config UI. The finished DB is large (multi‑GB) and the dump download
takes a long time — operators must opt in explicitly.

A separate lightweight check (HEAD/etag/size) can flag newer remote dumps and
notify admins; download + rebuild only start from the Admin "Update catalog"
button (or an explicit build with force_download).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_STATUS_NAME = "ol_catalog_build.json"
_DEFAULT_IDLE_MESSAGE = "Open Library catalog has not been built yet."
_MIN_READY_BYTES = 1024 * 1024
# Don't re-probe openlibrary.org more often than this when Admin opens Config.
_CHECK_THROTTLE_SECONDS = 6 * 3600
_proc: asyncio.subprocess.Process | None = None
_lock = asyncio.Lock()
_check_lock = asyncio.Lock()


def _status_path() -> Path:
    settings = get_settings()
    # Prefer next to the catalog DB; fall back to ./data
    try:
        db = Path(settings.ol_catalog_db_path)
        parent = db.parent if db.parent.as_posix() not in ("", ".") else Path("data")
    except Exception:
        parent = Path("data")
    parent.mkdir(parents=True, exist_ok=True)
    return parent / _STATUS_NAME


def _read_status() -> dict[str, Any]:
    path = _status_path()
    if not path.exists():
        return {
            "status": "idle",
            "message": _DEFAULT_IDLE_MESSAGE,
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {"status": "unknown", "message": "Could not read build status."}
    return data


def _write_status(data: dict[str, Any]) -> None:
    path = _status_path()
    tmp = path.with_suffix(".tmp")
    payload = {**data, "updated_at": time.time()}
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def _dir_size_bytes(path: Path) -> int:
    if not path.is_dir():
        return 0
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += (Path(root) / name).stat().st_size
                except OSError:
                    continue
    except OSError:
        return 0
    return total


def _dumps_stats() -> tuple[bool, int]:
    """Return (has_dump_files, total_size_bytes) for the dumps directory."""
    settings = get_settings()
    dumps = Path(settings.ol_dumps_dir)
    size = _dir_size_bytes(dumps)
    # Treat any non-trivial dump payload as "present"
    return size > 1024 * 1024, size


def _stat1_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Approximate row counts from ANALYZE stats (instant; importer runs ANALYZE)."""
    out: dict[str, int] = {}
    try:
        rows = conn.execute(
            "SELECT tbl, stat FROM sqlite_stat1 WHERE tbl IN ('works', 'authors', 'isbns')"
        ).fetchall()
    except sqlite3.Error:
        return out
    for tbl, stat in rows:
        if not stat:
            continue
        try:
            out[str(tbl)] = int(str(stat).split()[0])
        except (TypeError, ValueError):
            continue
    return out


def _catalog_stats() -> dict[str, Any]:
    """Inspect the SQLite catalog file. Ready only when the DB is usable."""
    settings = get_settings()
    path = Path(settings.ol_catalog_db_path)
    result: dict[str, Any] = {
        "ready": False,
        "size_bytes": 0,
        "mtime": None,
        "works": None,
        "authors": None,
        "isbns": None,
        "error": None,
    }
    try:
        if not path.is_file():
            return result
        st = path.stat()
        result["size_bytes"] = st.st_size
        result["mtime"] = st.st_mtime
        if st.st_size < _MIN_READY_BYTES:
            result["error"] = "Catalog DB is too small to be a finished build."
            return result
    except OSError as e:
        result["error"] = str(e)
        return result

    try:
        uri = f"file:{path.as_posix()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=2.0) as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='works'"
            ).fetchone()
            if not row:
                result["error"] = "works table missing"
                return result
            counts = _stat1_counts(conn)
            if "works" in counts:
                result["works"] = counts["works"]
            if "authors" in counts:
                result["authors"] = counts["authors"]
            if "isbns" in counts:
                result["isbns"] = counts["isbns"]
        result["ready"] = True
    except sqlite3.Error as e:
        result["error"] = f"Catalog DB unreadable: {e}"
    return result


def _fmt_bytes(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1e9:.2f} GB"
    if n >= 1_000_000:
        return f"{n / 1e6:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


def _fmt_mtime(ts: float | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _fmt_count(n: int | None) -> str | None:
    if n is None:
        return None
    return f"{n:,}"


def _reconcile_message(
    *,
    job: dict[str, Any],
    catalog: dict[str, Any],
    dumps_present: bool,
    dumps_size: int,
    running: bool,
    new_dumps_available: bool = False,
) -> str:
    """Pick an operator-facing message that cannot contradict catalog_ready."""
    if running:
        return str(job.get("message") or "Build in progress…")

    job_status = str(job.get("status") or "idle")
    job_message = str(job.get("message") or "").strip()
    stale_idle = (
        job_status in ("idle", "unknown", "")
        and (not job_message or job_message == _DEFAULT_IDLE_MESSAGE)
    )
    update_note = "New Open Library dumps available — use Update catalog to download & rebuild."

    if catalog["ready"]:
        # Prefer a recent successful job message; otherwise describe the on-disk DB.
        if job_status == "done" and job_message and job_message != _DEFAULT_IDLE_MESSAGE:
            base = job_message
        else:
            parts = [f"Catalog DB ready ({_fmt_bytes(int(catalog['size_bytes']))}"]
            mtime = _fmt_mtime(catalog.get("mtime"))
            if mtime:
                parts[0] += f", modified {mtime}"
            parts[0] += ")"
            counts = []
            for label, key in (("works", "works"), ("authors", "authors"), ("isbns", "isbns")):
                formatted = _fmt_count(catalog.get(key))
                if formatted is not None:
                    counts.append(f"{formatted} {label}")
            if counts:
                parts.append("; ".join(counts))
            base = " · ".join(parts) if len(parts) > 1 else parts[0]
        if new_dumps_available:
            return f"{base} · {update_note}"
        return base

    if new_dumps_available:
        if dumps_present:
            return (
                f"Dumps present ({_fmt_bytes(dumps_size)}); {update_note}"
            )
        return update_note

    if job_status == "error" and job_message:
        return job_message

    if dumps_present:
        return (
            f"Dumps present ({_fmt_bytes(dumps_size)}); "
            "catalog DB has not been built yet."
        )

    if catalog.get("error") and catalog.get("size_bytes"):
        return f"Catalog DB present but not usable ({catalog['error']})."

    if stale_idle or not job_message:
        return _DEFAULT_IDLE_MESSAGE
    return job_message


def _dump_names(*, include_editions: bool | None = None) -> list[str]:
    settings = get_settings()
    editions = (
        bool(include_editions)
        if include_editions is not None
        else bool(getattr(settings, "ol_catalog_include_editions", False))
    )
    names = ["authors", "works"]
    if editions:
        names.append("editions")
    return names


def get_status() -> dict[str, Any]:
    catalog = _catalog_stats()
    dumps_present, dumps_size = _dumps_stats()
    status = _read_status()
    running = _proc is not None and _proc.returncode is None
    if running:
        status["status"] = "running"
    elif catalog["ready"] and str(status.get("status") or "idle") in (
        "idle",
        "unknown",
        "",
    ):
        # DB exists (e.g. built via cron) even though no Admin job status was written.
        status["status"] = "ready"

    new_dumps = bool(status.get("new_dumps_available"))
    status["catalog_ready"] = bool(catalog["ready"])
    status["catalog_size_bytes"] = int(catalog["size_bytes"] or 0)
    status["catalog_mtime"] = catalog.get("mtime")
    status["catalog_works"] = catalog.get("works")
    status["catalog_authors"] = catalog.get("authors")
    status["catalog_isbns"] = catalog.get("isbns")
    status["dumps_present"] = dumps_present
    status["dumps_size_bytes"] = dumps_size
    status["catalog_path"] = get_settings().ol_catalog_db_path
    status["dumps_dir"] = get_settings().ol_dumps_dir
    status["new_dumps_available"] = new_dumps
    status["changed_dumps"] = list(status.get("changed_dumps") or [])
    status["dumps_checked_at"] = status.get("dumps_checked_at")
    status["message"] = _reconcile_message(
        job=status,
        catalog=catalog,
        dumps_present=dumps_present,
        dumps_size=dumps_size,
        running=running,
        new_dumps_available=new_dumps,
    )
    if catalog.get("error") and not catalog["ready"]:
        status["catalog_error"] = catalog["error"]
    status["warnings"] = [
        "Downloads multi-GB Open Library dump files (authors + works; editions optional and much larger).",
        "The finished catalog database is typically several GB (10-20+ GB if editions are included).",
        "On a Raspberry Pi this often takes many hours. Keep the container running until it finishes.",
        "Dumps download to OPENLIBRARY_HOST_DIR (/openlibrary); the catalog DB is written to the configured path (usually under ./data).",
        "A daily check notifies admins when newer dumps are published; download only starts from Update catalog.",
    ]
    return status


def _persist_check_result(summary: dict[str, Any], *, notify_sent: bool = False) -> None:
    cur = _read_status()
    cur["dumps_checked_at"] = summary.get("checked_at")
    cur["changed_dumps"] = list(summary.get("changed") or [])
    cur["dumps_check_errors"] = summary.get("errors") or {}
    cur["dumps_remote_signature"] = summary.get("signature")
    cur["new_dumps_available"] = bool(summary.get("update_available"))
    if notify_sent:
        cur["dumps_notified_signature"] = summary.get("signature")
        cur["dumps_notified_at"] = time.time()
    if not summary.get("update_available"):
        # Clear stale notify tracking when remote matches local again.
        cur.pop("dumps_notified_signature", None)
    _write_status(cur)


async def check_for_updates(
    *,
    force: bool = False,
    notify: bool = True,
    include_editions: bool | None = None,
) -> dict[str, Any]:
    """HEAD-check remote dumps vs local files. Never downloads.

    When newer dumps are found, sets ``new_dumps_available`` and (once per
    remote signature) notifies admins via web push + WS ``admin_alert``.
    """
    async with _check_lock:
        settings = get_settings()
        cur = _read_status()
        last = float(cur.get("dumps_checked_at") or 0)
        if not force and last and (time.time() - last) < _CHECK_THROTTLE_SECONDS:
            return get_status()

        from app.services import ol_dumps

        names = _dump_names(include_editions=include_editions)
        # Run blocking urllib HEAD calls off the event loop.
        summary = await asyncio.to_thread(
            ol_dumps.check_dumps,
            settings.ol_dumps_dir,
            names=names,
            ua=settings.open_library_user_agent,
        )

        notify_sent = False
        if notify and summary.get("update_available"):
            sig = summary.get("signature") or ""
            already = cur.get("dumps_notified_signature")
            if sig and sig != already:
                try:
                    from app.services import push

                    changed = ", ".join(summary.get("changed") or []) or "dumps"
                    await push.notify_admins_background(
                        {
                            "type": "ol_dumps_available",
                            "title": "New Open Library dumps available",
                            "body": (
                                f"Remote dumps changed ({changed}). "
                                "Open Admin → Config and click Update catalog to download & rebuild."
                            ),
                            "url": "/admin?tab=config",
                        }
                    )
                    notify_sent = True
                except Exception:
                    logger.warning("OL dumps admin notify failed", exc_info=True)

        _persist_check_result(summary, notify_sent=notify_sent)
        status = get_status()
        status["check"] = {
            "update_available": summary.get("update_available"),
            "changed": summary.get("changed"),
            "missing": summary.get("missing"),
            "unchanged": summary.get("unchanged"),
            "errors": summary.get("errors"),
            "notified": notify_sent,
        }
        return status


async def _pump_output(proc: asyncio.subprocess.Process) -> None:
    assert proc.stdout is not None
    last_line = ""
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        text = line.decode("utf-8", errors="replace").rstrip()
        if not text:
            continue
        last_line = text[-500:]
        logger.info("ol-catalog-build: %s", text)
        cur = _read_status()
        cur["status"] = "running"
        cur["message"] = last_line
        cur["log_tail"] = last_line
        _write_status(cur)


async def _run_build(
    *,
    include_editions: bool,
    skip_download: bool,
    force_download: bool,
) -> None:
    global _proc
    settings = get_settings()
    script = _PROJECT_ROOT / "scripts" / "ol_import_dumps.py"
    if not script.is_file():
        script = Path("/app/scripts/ol_import_dumps.py")
    if not script.is_file():
        _write_status(
            {
                "status": "error",
                "message": f"Import script not found: {script}",
                "finished_at": time.time(),
            }
        )
        return

    Path(settings.ol_dumps_dir).mkdir(parents=True, exist_ok=True)
    Path(settings.ol_catalog_db_path).parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "python",
        str(script),
        "--db",
        settings.ol_catalog_db_path,
        "--dumps",
        settings.ol_dumps_dir,
    ]
    if not include_editions:
        cmd.append("--no-editions")
    if skip_download:
        cmd.append("--skip-download")
    elif force_download:
        cmd.append("--force-download")

    prev = _read_status()
    _write_status(
        {
            "status": "running",
            "message": (
                "Starting Open Library catalog update (re-download + rebuild)…"
                if force_download and not skip_download
                else "Starting Open Library catalog build…"
            ),
            "include_editions": include_editions,
            "skip_download": skip_download,
            "force_download": force_download,
            "command": cmd,
            "started_at": time.time(),
            # Keep the banner visible until success; cleared below on done.
            "new_dumps_available": bool(prev.get("new_dumps_available")),
            "changed_dumps": list(prev.get("changed_dumps") or []),
            "dumps_checked_at": prev.get("dumps_checked_at"),
            "dumps_notified_signature": prev.get("dumps_notified_signature"),
        }
    )
    logger.info("Starting OL catalog build: %s", " ".join(cmd))
    try:
        _proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(_PROJECT_ROOT if (_PROJECT_ROOT / "app").is_dir() else Path("/app")),
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        await _pump_output(_proc)
        code = await _proc.wait()
        catalog = _catalog_stats()
        if code == 0 and catalog["ready"]:
            size = int(catalog["size_bytes"] or 0)
            _write_status(
                {
                    "status": "done",
                    "message": f"Catalog ready ({_fmt_bytes(size)}).",
                    "include_editions": include_editions,
                    "finished_at": time.time(),
                    "exit_code": code,
                    "new_dumps_available": False,
                    "changed_dumps": [],
                }
            )
        else:
            prev_err = _read_status()
            _write_status(
                {
                    "status": "error",
                    "message": f"Build exited with code {code}. Check container logs for [ol-import] lines.",
                    "include_editions": include_editions,
                    "finished_at": time.time(),
                    "exit_code": code,
                    "new_dumps_available": bool(prev_err.get("new_dumps_available")),
                    "changed_dumps": list(prev_err.get("changed_dumps") or []),
                    "dumps_checked_at": prev_err.get("dumps_checked_at"),
                    "dumps_notified_signature": prev_err.get("dumps_notified_signature"),
                }
            )
    except Exception as e:
        logger.exception("OL catalog build failed")
        prev_err = _read_status()
        _write_status(
            {
                "status": "error",
                "message": str(e),
                "finished_at": time.time(),
                "new_dumps_available": bool(prev_err.get("new_dumps_available")),
                "changed_dumps": list(prev_err.get("changed_dumps") or []),
            }
        )
    finally:
        _proc = None


async def start_build(
    *,
    include_editions: bool = False,
    skip_download: bool = False,
    force_download: bool = False,
) -> dict[str, Any]:
    """Start a build if none is running. Returns current status.

    ``force_download`` re-fetches dumps (Admin "Update catalog") even when local
    files exist. Normal builds still re-download when remote HEAD differs.
    """
    async with _lock:
        if _proc is not None and _proc.returncode is None:
            return get_status()
        asyncio.create_task(
            _run_build(
                include_editions=include_editions,
                skip_download=skip_download,
                force_download=force_download,
            )
        )
        # Give the task a tick to write running status
        await asyncio.sleep(0.05)
        return get_status()
