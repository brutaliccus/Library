"""Safe host-script runners for the instance setup wizard (no arbitrary shell)."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from app.services import docker_control, server_update

logger = logging.getLogger(__name__)

UPDATE_IMAGE = server_update.UPDATE_IMAGE
ALLOWED_PIPELINES = frozenset({"bootstrap-indexers", "configure-npm", "generate-vapid"})

# Whitelist of .env keys the NPM configure endpoint may set before running the script.
_NPM_ENV_KEYS = frozenset(
    {
        "NPM_DOMAIN",
        "NPM_ABS_DOMAIN",
        "NPM_KAVITA_DOMAIN",
        "NPM_LETSENCRYPT_EMAIL",
        "NPM_ADMIN_EMAIL",
        "NPM_ADMIN_PASSWORD",
    }
)


def _docker_client():
    return server_update._docker_client()  # noqa: SLF001


async def _ensure_image(image: str) -> None:
    await server_update._ensure_image(image)  # noqa: SLF001


async def _cleanup_container(cid: str) -> None:
    await server_update._cleanup_container(cid)  # noqa: SLF001


def _docker_status_code(payload: dict[str, Any] | None, *, default: int = 1) -> int:
    return server_update._docker_status_code(payload, default=default)  # noqa: SLF001


async def _run_host_script(host_root: str, cmd: str, *, timeout: float = 600.0) -> dict[str, Any]:
    """Run a fixed bash pipeline on the host install root via a short-lived sidecar."""
    if not docker_control.socket_available():
        return {"ok": False, "error": "docker.sock not available", "exitCode": None, "log": ""}

    await _ensure_image(UPDATE_IMAGE)
    full = (
        "apk add --no-cache bash curl python3 >/dev/null 2>&1 || true; "
        "cd /library && "
        + cmd
    )
    body = {
        "Image": UPDATE_IMAGE,
        "Entrypoint": ["sh", "-c"],
        "Cmd": [full],
        "WorkingDir": "/library",
        "Env": [
            "GIT_TERMINAL_PROMPT=0",
            f"LIBRARY_HOST_ROOT_BIND={host_root}",
        ],
        "Labels": {"com.library.setup-bootstrap": "1"},
        "HostConfig": {
            "Binds": [
                f"{host_root}:/library",
                f"{docker_control.DOCKER_SOCK}:/var/run/docker.sock",
            ],
            "AutoRemove": False,
            "NetworkMode": "host",
            "RestartPolicy": {"Name": "no"},
        },
    }
    cid: str | None = None
    log_text = ""
    try:
        async with _docker_client() as client:
            create = await client.post(
                f"{docker_control.API_PREFIX}/containers/create",
                params={"name": f"library-setup-{int(time.time())}"},
                json=body,
            )
            if create.status_code == 409:
                create = await client.post(
                    f"{docker_control.API_PREFIX}/containers/create",
                    json=body,
                )
            if create.status_code >= 400:
                return {
                    "ok": False,
                    "error": f"create failed: HTTP {create.status_code} {create.text[:200]}",
                    "exitCode": None,
                    "log": "",
                }
            cid = str((create.json() or {}).get("Id") or "")
            if not cid:
                return {"ok": False, "error": "create returned no id", "exitCode": None, "log": ""}
            start = await client.post(f"{docker_control.API_PREFIX}/containers/{cid}/start")
            if start.status_code >= 400:
                return {
                    "ok": False,
                    "error": f"start failed: HTTP {start.status_code}",
                    "exitCode": None,
                    "log": "",
                }
            wait = await client.post(
                f"{docker_control.API_PREFIX}/containers/{cid}/wait",
                timeout=timeout,
            )
            code = _docker_status_code(wait.json() if wait.content else None, default=1)
            logs = await client.get(
                f"{docker_control.API_PREFIX}/containers/{cid}/logs",
                params={"stdout": "true", "stderr": "true", "timestamps": "false"},
                timeout=30.0,
            )
            raw = logs.content or b""
            # Docker multiplexed stream: strip 8-byte headers when present.
            parts: list[bytes] = []
            i = 0
            while i + 8 <= len(raw):
                size = int.from_bytes(raw[i + 4 : i + 8], "big")
                chunk = raw[i + 8 : i + 8 + size]
                parts.append(chunk)
                i += 8 + size
            if not parts:
                parts = [raw]
            log_text = b"".join(parts).decode("utf-8", errors="replace")[-8000:]
            return {
                "ok": code == 0,
                "error": None if code == 0 else f"exit {code}",
                "exitCode": code,
                "log": log_text,
            }
    except Exception as e:
        logger.exception("setup bootstrap sidecar failed")
        return {"ok": False, "error": str(e)[:300], "exitCode": None, "log": log_text}
    finally:
        if cid:
            await _cleanup_container(cid)


async def bootstrap_indexers() -> dict[str, Any]:
    resolved = await server_update.resolve_validated_host_root()
    host_root = resolved.get("hostRoot")
    if not host_root:
        return {
            "ok": False,
            "error": resolved.get("error") or "LIBRARY_HOST_ROOT not resolved",
            "hostRoot": None,
        }
    cmd = (
        "bash scripts/configure_jackett.sh --force-bundled && "
        "bash scripts/configure_prowlarr.sh --force-bundled && "
        "bash scripts/apply_indexer_keys.sh"
    )
    result = await _run_host_script(str(host_root), cmd, timeout=600.0)
    result["pipeline"] = "bootstrap-indexers"
    result["hostRoot"] = host_root
    return result


async def configure_npm(settings: dict[str, str]) -> dict[str, Any]:
    resolved = await server_update.resolve_validated_host_root()
    host_root = resolved.get("hostRoot")
    if not host_root:
        return {
            "ok": False,
            "error": resolved.get("error") or "LIBRARY_HOST_ROOT not resolved",
            "hostRoot": None,
        }
    exports: list[str] = []
    for key, raw in (settings or {}).items():
        if key not in _NPM_ENV_KEYS:
            continue
        val = str(raw or "").strip()
        if not val and key != "NPM_ADMIN_PASSWORD":
            continue
        # Persist into .env via a tiny python snippet (no arbitrary shell from user).
        exports.append(f"{key}={val}")
    set_block = ""
    if exports:
        # Write whitelisted keys into .env before configure_npm.sh
        py = (
            "import pathlib\n"
            "p=pathlib.Path('/library/.env')\n"
            "text=p.read_text(encoding='utf-8') if p.exists() else ''\n"
            "lines=text.splitlines()\n"
            "keys={}\n"
        )
        for item in exports:
            k, _, v = item.partition("=")
            py += f"keys[{k!r}]={v!r}\n"
        py += (
            "out=[]; seen=set()\n"
            "for line in lines:\n"
            "    if '=' in line and not line.startswith('#'):\n"
            "        k=line.split('=',1)[0]\n"
            "        if k in keys:\n"
            "            out.append(f'{k}={keys[k]}'); seen.add(k); continue\n"
            "    out.append(line)\n"
            "for k,v in keys.items():\n"
            "    if k not in seen: out.append(f'{k}={v}')\n"
            "p.write_text('\\n'.join(out)+('\\n' if out else ''), encoding='utf-8')\n"
        )
        set_block = f"python3 - <<'PY'\n{py}PY\n && "
    cmd = set_block + "bash scripts/configure_npm.sh"
    result = await _run_host_script(str(host_root), cmd, timeout=300.0)
    result["pipeline"] = "configure-npm"
    result["hostRoot"] = host_root
    return result


def _vapid_env_write_script() -> str:
    """Bash snippet: expect PRIV/PUB exported; write .env; recreate app.

    Heredoc closer ``PY`` must be alone on its line — never ``PY; …``.
    """
    return (
        "python3 - <<'PY'\n"
        "import os, pathlib\n"
        "priv = os.environ['PRIV']\n"
        "pub = os.environ['PUB']\n"
        "p = pathlib.Path('/library/.env')\n"
        "text = p.read_text(encoding='utf-8') if p.exists() else ''\n"
        "\n"
        "def setk(t: str, k: str, v: str) -> str:\n"
        "    lines = t.splitlines()\n"
        "    out: list[str] = []\n"
        "    seen = False\n"
        "    for line in lines:\n"
        "        if line.startswith(k + '='):\n"
        "            out.append(f'{k}={v}')\n"
        "            seen = True\n"
        "        else:\n"
        "            out.append(line)\n"
        "    if not seen:\n"
        "        out.append(f'{k}={v}')\n"
        "    return '\\n'.join(out) + ('\\n' if out else '')\n"
        "\n"
        "priv_val = priv if priv.startswith('\"') else f'\"{priv}\"'\n"
        "text = setk(text, 'VAPID_PRIVATE_KEY', priv_val)\n"
        "text = setk(text, 'VAPID_PUBLIC_KEY', pub)\n"
        "p.write_text(text, encoding='utf-8')\n"
        "print('ok')\n"
        "PY\n"
        "docker compose up -d --force-recreate --no-deps app || true\n"
    )


async def generate_vapid_keys() -> dict[str, Any]:
    """Generate VAPID keys and write them into the host .env (restart may be required)."""
    resolved = await server_update.resolve_validated_host_root()
    host_root = resolved.get("hostRoot")
    if not host_root:
        return {
            "ok": False,
            "error": resolved.get("error") or "LIBRARY_HOST_ROOT not resolved",
            "hostRoot": None,
        }
    wrapped = (
        "set -euo pipefail; "
        "OUT=$(docker compose exec -T app python scripts/generate_vapid.py 2>/dev/null || true); "
        "PRIV=$(printf '%s\\n' \"$OUT\" | grep '^VAPID_PRIVATE_KEY=' | head -1 | sed 's/^VAPID_PRIVATE_KEY=//;s/^\"//;s/\"$//'); "
        "PUB=$(printf '%s\\n' \"$OUT\" | grep '^VAPID_PUBLIC_KEY=' | head -1 | cut -d= -f2-); "
        "if [ -z \"$PRIV\" ] || [ -z \"$PUB\" ]; then echo 'VAPID generation failed' >&2; exit 1; fi; "
        "export PRIV PUB; "
        + _vapid_env_write_script()
    )
    result = await _run_host_script(str(host_root), wrapped, timeout=300.0)
    result["pipeline"] = "generate-vapid"
    result["hostRoot"] = host_root
    result["restartHint"] = "App recreated to load VAPID keys from .env"
    return result


async def media_path_hints() -> dict[str, str]:
    return {
        "audiobookHostDir": (os.environ.get("AUDIOBOOK_HOST_DIR") or "").strip()
        or "/opt/library/media/audiobooks",
        "ebookHostDir": (os.environ.get("EBOOK_HOST_DIR") or "").strip()
        or "/opt/library/media/ebooks",
        "openlibraryHostDir": (os.environ.get("OPENLIBRARY_HOST_DIR") or "").strip()
        or "/opt/library/media/openlibrary",
        "libraryHostRoot": (os.environ.get("LIBRARY_HOST_ROOT") or "").strip() or "/opt/library",
    }