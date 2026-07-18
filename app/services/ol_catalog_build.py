"""Background builder for the local Open Library catalog database.

Runs ``scripts/ol_import_dumps.py`` as a subprocess and exposes status for the
Admin → Config UI. The finished DB is large (multi‑GB) and the dump download
takes a long time — operators must opt in explicitly.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_STATUS_NAME = "ol_catalog_build.json"
_proc: asyncio.subprocess.Process | None = None
_lock = asyncio.Lock()


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
            "message": "Open Library catalog has not been built yet.",
            "catalog_ready": False,
            "catalog_size_bytes": 0,
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


def _catalog_stats() -> tuple[bool, int]:
    settings = get_settings()
    path = Path(settings.ol_catalog_db_path)
    try:
        if path.is_file():
            size = path.stat().st_size
            return size > 1024 * 1024, size
    except OSError:
        pass
    return False, 0


def get_status() -> dict[str, Any]:
    ready, size = _catalog_stats()
    status = _read_status()
    running = _proc is not None and _proc.returncode is None
    if running:
        status["status"] = "running"
    status["catalog_ready"] = ready
    status["catalog_size_bytes"] = size
    status["catalog_path"] = get_settings().ol_catalog_db_path
    status["dumps_dir"] = get_settings().ol_dumps_dir
    status["warnings"] = [
        "Downloads multi-GB Open Library dump files (authors + works; editions optional and much larger).",
        "The finished catalog database is typically several GB (10-20+ GB if editions are included).",
        "On a Raspberry Pi this often takes many hours. Keep the container running until it finishes.",
        "Dumps download to OPENLIBRARY_HOST_DIR (/openlibrary); the catalog DB is written to the configured path (usually under ./data).",
    ]
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


async def _run_build(*, include_editions: bool, skip_download: bool) -> None:
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

    _write_status(
        {
            "status": "running",
            "message": "Starting Open Library catalog build…",
            "include_editions": include_editions,
            "skip_download": skip_download,
            "command": cmd,
            "started_at": time.time(),
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
        ready, size = _catalog_stats()
        if code == 0 and ready:
            _write_status(
                {
                    "status": "done",
                    "message": f"Catalog ready ({size / 1e9:.2f} GB).",
                    "include_editions": include_editions,
                    "finished_at": time.time(),
                    "exit_code": code,
                }
            )
        else:
            _write_status(
                {
                    "status": "error",
                    "message": f"Build exited with code {code}. Check container logs for [ol-import] lines.",
                    "include_editions": include_editions,
                    "finished_at": time.time(),
                    "exit_code": code,
                }
            )
    except Exception as e:
        logger.exception("OL catalog build failed")
        _write_status(
            {
                "status": "error",
                "message": str(e),
                "finished_at": time.time(),
            }
        )
    finally:
        _proc = None


async def start_build(*, include_editions: bool = False, skip_download: bool = False) -> dict[str, Any]:
    """Start a build if none is running. Returns current status."""
    async with _lock:
        if _proc is not None and _proc.returncode is None:
            return get_status()
        asyncio.create_task(
            _run_build(include_editions=include_editions, skip_download=skip_download)
        )
        # Give the task a tick to write running status
        await asyncio.sleep(0.05)
        return get_status()
