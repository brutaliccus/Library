"""Admin server stack update via host git + docker compose bridge.

The app container typically has no writable .git checkout. Updates run in a
one-shot sidecar with the compose project directory and docker.sock mounted,
invoking scripts/admin_server_update.sh (same work as update_library.sh).

The sidecar is **detached**: the app must not wait on or force-delete it.
``update_library.sh`` recreates the app container; an in-process waiter that
cleaned up the sidecar on cancel previously killed mid-recreate and left the
stack stopped.

Version checks prefer GitHub compare against data/install_revision.json when
live git is unavailable inside the app.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import httpx

from app.services import docker_control

logger = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("LIBRARY_DATA_DIR", "/app/data"))
REVISION_FILE = DATA_DIR / "install_revision.json"
JOB_FILE = DATA_DIR / "server_update_job.json"
JOB_LOG = DATA_DIR / "server_update_job.log"

DEFAULT_REPO = "brutaliccus/Library"
DEFAULT_BRANCH = "main"
DEFAULT_REMOTE = "origin"
SELF_CONTAINER = "audiobook-request"
UPDATE_IMAGE = os.environ.get("LIBRARY_UPDATE_SIDECAR_IMAGE", "docker:27-cli")
HOST_ROOT_ENV = "LIBRARY_HOST_ROOT"
HOST_MOUNT = Path("/library-host")
# Positive host-root probe cache (host path → (ok_until_monotonic, detail)).
_PROBE_OK_TTL_SEC = 60.0
_probe_ok_cache: dict[str, tuple[float, str]] = {}

_task: asyncio.Task | None = None


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception as e:
        logger.debug("read json %s failed: %s", path, e)
        return None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _short_sha(sha: str | None) -> str | None:
    if not sha:
        return None
    sha = sha.strip()
    return sha[:7] if len(sha) >= 7 else sha


async def _github_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "LibrarySite-ServerUpdate",
    }
    try:
        from app.services import instance_settings

        token = (await instance_settings.get_effective("config.github_token") or "").strip()
    except Exception:
        token = (os.environ.get("GITHUB_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _github_repo() -> str:
    try:
        from app.services import instance_settings

        raw = (await instance_settings.get_effective("config.android_apk_github_repo") or "").strip()
    except Exception:
        raw = ""
    return raw or os.environ.get("ANDROID_APK_GITHUB_REPO") or DEFAULT_REPO


def _local_from_revision_file() -> dict[str, Any] | None:
    data = _read_json(REVISION_FILE)
    if not data:
        return None
    sha = str(data.get("sha") or "").strip() or None
    return {
        "sha": sha,
        "shortSha": str(data.get("shortSha") or _short_sha(sha) or ""),
        "branch": str(data.get("branch") or "") or None,
        "message": str(data.get("message") or "") or None,
        "committedAt": str(data.get("committedAt") or "") or None,
        "tracking": str(data.get("tracking") or "") or None,
        "updatedAt": str(data.get("updatedAt") or "") or None,
        "source": str(data.get("source") or "install_revision.json"),
    }


def _local_from_env() -> dict[str, Any] | None:
    sha = (
        os.environ.get("LIBRARY_INSTALL_SHA")
        or os.environ.get("GITHUB_SHA")
        or os.environ.get("SOURCE_COMMIT")
        or ""
    ).strip()
    if not sha:
        return None
    return {
        "sha": sha,
        "shortSha": _short_sha(sha),
        "branch": (os.environ.get("LIBRARY_INSTALL_BRANCH") or "").strip() or None,
        "message": None,
        "committedAt": None,
        "tracking": None,
        "updatedAt": None,
        "source": "env",
    }


async def _run_cmd(args: list[str], cwd: Path | None = None, timeout: float = 30.0) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(cwd) if cwd else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode or 0, out_b.decode("utf-8", "replace"), err_b.decode("utf-8", "replace")
    except FileNotFoundError:
        return 127, "", "command not found"
    except asyncio.TimeoutError:
        return 124, "", "timeout"
    except Exception as e:
        return 1, "", str(e)


async def _local_from_git_dir(git_root: Path) -> dict[str, Any] | None:
    if not (git_root / ".git").exists():
        return None
    code, sha, _ = await _run_cmd(["git", "-C", str(git_root), "rev-parse", "HEAD"])
    if code != 0 or not sha.strip():
        return None
    sha = sha.strip()
    _, short, _ = await _run_cmd(["git", "-C", str(git_root), "rev-parse", "--short", "HEAD"])
    _, branch, _ = await _run_cmd(["git", "-C", str(git_root), "rev-parse", "--abbrev-ref", "HEAD"])
    _, msg, _ = await _run_cmd(["git", "-C", str(git_root), "log", "-1", "--pretty=format:%s"])
    _, committed, _ = await _run_cmd(["git", "-C", str(git_root), "log", "-1", "--pretty=format:%cI"])
    return {
        "sha": sha,
        "shortSha": short.strip() or _short_sha(sha),
        "branch": branch.strip() or None,
        "message": msg.strip() or None,
        "committedAt": committed.strip() or None,
        "tracking": None,
        "updatedAt": None,
        "source": f"git:{git_root}",
    }


def _configured_host_root() -> str | None:
    """Host path for bind-mounting the install root into the update sidecar.

    ``LIBRARY_HOST_ROOT`` is the path *on the Docker host* (e.g. ``/opt/library``).
    It usually does not exist inside the app container — that is expected.
    """
    env = (os.environ.get(HOST_ROOT_ENV) or "").strip()
    if env:
        return env
    return None


def looks_like_host_abs_path(path: str) -> bool:
    """True for absolute host paths (Unix ``/…`` or Windows ``C:\\…``)."""
    p = (path or "").strip()
    if not p or p in (".", ".."):
        return False
    if p.startswith("/") and len(p) >= 2:
        return True
    if len(p) >= 3 and p[0].isalpha() and p[1] == ":" and p[2] in ("\\", "/"):
        return True
    return False


def host_root_from_app_data_mount(source: str) -> str:
    """``/opt/library/data`` (host) → ``/opt/library``; never use container paths.

    Uses POSIX path rules for ``/…`` sources so Windows-hosted unit tests match
    the Linux Docker host paths the runtime sees.
    """
    src = (source or "").strip().rstrip("/\\")
    if not src:
        return ""
    if src.startswith("/"):
        return str(PurePosixPath(src).parent)
    return str(PureWindowsPath(src).parent)


def _dedupe_candidates(items: list[tuple[str, str]]) -> list[tuple[str, str]]:
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for source, path in items:
        key = path.rstrip("/\\")
        if key in seen:
            continue
        seen.add(key)
        out.append((source, path))
    return out


async def _collect_host_root_candidates() -> tuple[list[tuple[str, str]], list[str]]:
    """Return (ordered candidates, rejection notes) without probing the host FS."""
    candidates: list[tuple[str, str]] = []
    rejected: list[str] = []

    configured = _configured_host_root()
    if configured:
        if looks_like_host_abs_path(configured):
            candidates.append(("env", configured))
        else:
            rejected.append(f"env:{configured!r} (not an absolute host path)")

    if not docker_control.socket_available():
        if not candidates:
            rejected.append("docker.sock not available")
        return _dedupe_candidates(candidates), rejected

    try:
        info = await docker_control._inspect(SELF_CONTAINER)  # noqa: SLF001 — shared sock helper
    except Exception as e:
        rejected.append(f"inspect failed: {e}")
        return _dedupe_candidates(candidates), rejected

    if not info:
        rejected.append(f"container {SELF_CONTAINER} not found")
        return _dedupe_candidates(candidates), rejected

    labels = (info.get("Config") or {}).get("Labels") or {}
    working = (
        labels.get("com.docker.compose.project.working_dir")
        or labels.get("com.docker.compose.project.workingdir")
        or ""
    ).strip()
    if working:
        if looks_like_host_abs_path(working):
            candidates.append(("compose_label", working))
        else:
            rejected.append(f"compose_label:{working!r} (not an absolute host path)")

    for mount in info.get("Mounts") or []:
        if not isinstance(mount, dict):
            continue
        dest = str(mount.get("Destination") or "")
        src = str(mount.get("Source") or "").strip()
        if not src:
            continue
        if dest == "/app/data":
            root = host_root_from_app_data_mount(src)
            if looks_like_host_abs_path(root):
                candidates.append(("mount_app_data", root))
            else:
                rejected.append(f"mount_app_data:{src!r}→{root!r} (not an absolute host path)")
        elif dest == "/library-host":
            if looks_like_host_abs_path(src):
                candidates.append(("mount_library_host", src))
            else:
                rejected.append(f"mount_library_host:{src!r} (not an absolute host path)")

    return _dedupe_candidates(candidates), rejected


def _docker_status_code(payload: dict[str, Any] | None, *, default: int = 1) -> int:
    """Read Docker wait StatusCode. Must not use ``or`` — exit 0 is success and falsy."""
    if not payload:
        return default
    if "StatusCode" not in payload:
        return default
    try:
        return int(payload["StatusCode"])
    except (TypeError, ValueError):
        return default


async def _probe_host_root(host_root: str) -> tuple[bool, str]:
    """Bind-mount ``host_root`` on the Docker *host* and require install markers.

    ``LIBRARY_HOST_ROOT`` is a host path and usually does **not** exist inside the
    app container — never validate with ``Path(host_root).exists()``.
    """
    if not looks_like_host_abs_path(host_root):
        return False, "not an absolute host path"
    if not docker_control.socket_available():
        return False, "docker.sock not available"

    cached = _probe_ok_cache.get(host_root)
    if cached and cached[0] > time.monotonic():
        return True, cached[1]

    await _ensure_image(UPDATE_IMAGE)
    # Clear image ENTRYPOINT (docker-entrypoint.sh) so we run a plain shell test.
    body = {
        "Image": UPDATE_IMAGE,
        "Entrypoint": ["sh", "-c"],
        "Cmd": ["test -e /probe/.git || test -e /probe/scripts/update_library.sh"],
        "HostConfig": {
            "Binds": [f"{host_root}:/probe:ro"],
            "AutoRemove": False,
            "NetworkMode": "none",
        },
    }
    cid: str | None = None
    try:
        async with _docker_client() as client:
            create = await client.post(
                f"{docker_control.API_PREFIX}/containers/create",
                json=body,
            )
            if create.status_code >= 400:
                return False, f"probe create failed: HTTP {create.status_code}"
            cid = str((create.json() or {}).get("Id") or "")
            if not cid:
                return False, "probe create returned no id"
            start = await client.post(f"{docker_control.API_PREFIX}/containers/{cid}/start")
            if start.status_code >= 400:
                return False, f"probe start failed: HTTP {start.status_code}"
            wait = await client.post(
                f"{docker_control.API_PREFIX}/containers/{cid}/wait",
                timeout=60.0,
            )
            if wait.status_code >= 400:
                return False, f"probe wait failed: HTTP {wait.status_code}"
            code = _docker_status_code(wait.json() if wait.content else None, default=1)
            if code == 0:
                _probe_ok_cache[host_root] = (time.monotonic() + _PROBE_OK_TTL_SEC, "ok")
                return True, "ok"
            return False, "missing .git and scripts/update_library.sh"
    except Exception as e:
        return False, str(e)
    finally:
        if cid:
            await _cleanup_container(cid)


async def resolve_validated_host_root() -> dict[str, Any]:
    """Pick the first candidate that probes as a real install root on the host."""
    candidates, rejected = await _collect_host_root_candidates()
    tried: list[str] = list(rejected)
    if not candidates:
        return {
            "hostRoot": None,
            "containerRoot": str(HOST_MOUNT) if HOST_MOUNT.is_dir() else None,
            "source": None,
            "candidates": [],
            "tried": tried,
            "error": "could not resolve compose project directory; tried: "
            + (", ".join(tried) if tried else "(none)"),
        }

    for source, path in candidates:
        ok, detail = await _probe_host_root(path)
        label = f"{source}:{path}"
        if ok:
            return {
                "hostRoot": path,
                "containerRoot": str(HOST_MOUNT) if HOST_MOUNT.is_dir() else None,
                "source": source,
                "candidates": [{"source": s, "path": p} for s, p in candidates],
                "tried": tried,
                "error": None,
            }
        tried.append(f"{label} ({detail})")

    return {
        "hostRoot": None,
        "containerRoot": str(HOST_MOUNT) if HOST_MOUNT.is_dir() else None,
        "source": None,
        "candidates": [{"source": s, "path": p} for s, p in candidates],
        "tried": tried,
        "error": "no valid host install root (need .git or scripts/update_library.sh); tried: "
        + ", ".join(tried),
    }


async def discover_host_root() -> dict[str, Any]:
    """Locate the compose project directory on the Docker host (best candidate, no probe).

    Preference order:
      1. ``LIBRARY_HOST_ROOT`` (absolute host path)
      2. Compose project ``working_dir`` label on the app container
      3. Parent of the host path mounted at ``/app/data`` (e.g. ``/opt/library/data`` → ``/opt/library``)
      4. Host path mounted at ``/library-host``
    """
    candidates, rejected = await _collect_host_root_candidates()
    if candidates:
        source, path = candidates[0]
        return {
            "hostRoot": path,
            "containerRoot": str(HOST_MOUNT) if HOST_MOUNT.is_dir() else None,
            "source": source,
            "candidates": [{"source": s, "path": p} for s, p in candidates],
            "tried": rejected,
            "error": None,
        }
    return {
        "hostRoot": None,
        "containerRoot": None,
        "source": None,
        "candidates": [],
        "tried": rejected,
        "error": "could not resolve compose project directory; tried: "
        + (", ".join(rejected) if rejected else "(none)"),
    }


async def get_local_version() -> dict[str, Any]:
    rev = _local_from_revision_file()
    if rev and rev.get("sha"):
        return rev

    for candidate in (HOST_MOUNT, Path("/app")):
        git_ver = await _local_from_git_dir(candidate)
        if git_ver:
            return git_ver

    env_ver = _local_from_env()
    if env_ver:
        return env_ver

    return {
        "sha": None,
        "shortSha": None,
        "branch": None,
        "message": None,
        "committedAt": None,
        "tracking": None,
        "updatedAt": None,
        "source": None,
    }


def _job_snapshot() -> dict[str, Any]:
    job = _read_json(JOB_FILE) or {}
    log_tail = ""
    try:
        if JOB_LOG.is_file():
            text = JOB_LOG.read_text(encoding="utf-8", errors="replace")
            log_tail = text[-4000:]
    except Exception:
        log_tail = ""
    phase = str(job.get("phase") or "idle")
    running = bool(job.get("running")) or phase == "updating"
    # If the sidecar died without writing finished, avoid stuck "updating" forever (>2h).
    updated_at = job.get("updatedAt") or job.get("startedAt")
    if running and updated_at and isinstance(updated_at, str):
        try:
            # best-effort stale detection via mtime
            age = time.time() - JOB_FILE.stat().st_mtime
            if age > 7200:
                running = False
                phase = "failed"
                job["error"] = job.get("error") or "update job appears stale"
        except Exception:
            pass
    return {
        "phase": phase if phase else "idle",
        "running": running,
        "ok": job.get("ok"),
        "error": job.get("error"),
        "startedAt": job.get("startedAt"),
        "finishedAt": job.get("finishedAt"),
        "updatedAt": job.get("updatedAt"),
        "containerId": job.get("containerId"),
        "logTail": log_tail,
    }


async def _reconcile_job_with_sidecars() -> dict[str, Any]:
    """Refresh job snapshot; if phase is updating but no sidecar remains, mark failed/idle."""
    await _reap_exited_update_sidecars()
    job = _job_snapshot()
    phase = str(job.get("phase") or "")
    if phase not in ("updating", "validating") and not job.get("running"):
        return job
    if await _update_sidecar_running():
        return job
    # No running sidecar — if still "updating", the apply was interrupted (historic bug)
    # or finished without writing status. Prefer sidecar-written terminal phases.
    raw = _read_json(JOB_FILE) or {}
    if str(raw.get("phase") or "") in ("succeeded", "failed"):
        return _job_snapshot()
    # Stale updating with no sidecar: clear running so UI un-wedges.
    age_ok = True
    try:
        age_ok = (time.time() - JOB_FILE.stat().st_mtime) > 90
    except Exception:
        pass
    if age_ok:
        raw.update(
            {
                "phase": "failed",
                "running": False,
                "ok": False,
                "error": raw.get("error")
                or "update sidecar stopped before finishing (stack may need: "
                "bash scripts/update_library.sh --force --yes)",
                "finishedAt": _now_iso(),
                "updatedAt": _now_iso(),
            }
        )
        _write_json(JOB_FILE, raw)
    return _job_snapshot()


async def get_status() -> dict[str, Any]:
    local = await get_local_version()
    root = await discover_host_root()
    job = await _reconcile_job_with_sidecars()
    sock_ok = docker_control.socket_available()
    host_ok = bool(root.get("hostRoot"))
    apply_ready = host_ok and sock_ok and not job.get("running")
    if apply_ready:
        reason = None
    elif job.get("running"):
        reason = "Server update already running"
    elif not sock_ok:
        reason = (
            "docker.sock not available in the app container — mount "
            "/var/run/docker.sock (check still works via GitHub)"
        )
    else:
        reason = root.get("error") or "Host project directory unavailable"
    return {
        "local": local,
        "remote": None,
        "state": "updating" if job.get("running") else "unknown",
        "branch": DEFAULT_BRANCH,
        "remoteName": DEFAULT_REMOTE,
        "repo": await _github_repo(),
        "hostRoot": root.get("hostRoot"),
        "hostRootSource": root.get("source"),
        "applyAvailable": apply_ready,
        "applyUnavailableReason": reason,
        "dockerSocket": sock_ok,
        "job": job,
        "manualCommand": "cd /opt/library && bash scripts/update_library.sh",
    }


async def _github_tip(repo: str, branch: str) -> dict[str, Any]:
    headers = await _github_headers()
    url = f"https://api.github.com/repos/{repo}/commits/{branch}"
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code == 404:
            raise RuntimeError(f"GitHub repo/branch not found: {repo}@{branch}")
        if resp.status_code >= 400:
            raise RuntimeError(f"GitHub API HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
    commit = data.get("commit") or {}
    sha = str(data.get("sha") or "").strip()
    return {
        "sha": sha,
        "shortSha": _short_sha(sha),
        "branch": branch,
        "message": str((commit.get("message") or "").split("\n", 1)[0]) or None,
        "committedAt": str(((commit.get("committer") or {}).get("date")) or "") or None,
        "htmlUrl": data.get("html_url"),
        "source": "github_api",
    }


async def _github_compare(repo: str, base: str, head: str) -> dict[str, Any]:
    headers = await _github_headers()
    url = f"https://api.github.com/repos/{repo}/compare/{base}...{head}"
    async with httpx.AsyncClient(timeout=25.0) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code >= 400:
            raise RuntimeError(f"GitHub compare HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
    return {
        "status": str(data.get("status") or "unknown"),
        "aheadBy": int(data.get("ahead_by") or 0),
        "behindBy": int(data.get("behind_by") or 0),
        "totalCommits": int(data.get("total_commits") or 0),
        "htmlUrl": data.get("html_url"),
    }


async def check_for_updates(branch: str | None = None) -> dict[str, Any]:
    branch = (branch or DEFAULT_BRANCH).strip() or DEFAULT_BRANCH
    status = await get_status()
    local = status["local"]
    repo = status["repo"]
    try:
        remote = await _github_tip(repo, branch)
    except Exception as e:
        logger.warning("server-update check failed: %s", e)
        return {
            **status,
            "state": "check_failed",
            "error": str(e),
            "checkedAt": _now_iso(),
        }

    local_sha = (local.get("sha") or "").strip()
    remote_sha = (remote.get("sha") or "").strip()
    compare = None
    state = "unknown"
    if local_sha and remote_sha:
        if local_sha == remote_sha:
            state = "up_to_date"
            compare = {"status": "identical", "aheadBy": 0, "behindBy": 0, "totalCommits": 0}
        else:
            try:
                # base=local, head=remote → ahead_by = commits on remote not in local
                compare = await _github_compare(repo, local_sha, remote_sha)
                remote_ahead = int(compare.get("aheadBy") or 0)
                cmp_status = str(compare.get("status") or "")
                if cmp_status == "identical" or (remote_ahead == 0 and cmp_status != "diverged"):
                    state = "up_to_date"
                elif remote_ahead > 0 or cmp_status in ("ahead", "diverged"):
                    state = "update_available"
                else:
                    state = "up_to_date"
                # Expose behind count in the update-available sense for the UI.
                compare = {
                    **compare,
                    "commitsBehind": remote_ahead,
                }
            except Exception as e:
                logger.info("compare failed, falling back to sha inequality: %s", e)
                state = "update_available"
    elif remote_sha and not local_sha:
        state = "unknown"
    elif remote_sha:
        state = "update_available"

    return {
        **status,
        "local": local,
        "remote": remote,
        "compare": compare,
        "state": state,
        "branch": branch,
        "checkedAt": _now_iso(),
        "error": None,
    }


def _docker_client() -> httpx.AsyncClient:
    transport = httpx.AsyncHTTPTransport(uds=docker_control.DOCKER_SOCK)
    return httpx.AsyncClient(
        transport=transport,
        base_url="http://docker",
        timeout=120.0,
    )


async def _ensure_image(image: str) -> None:
    async with _docker_client() as client:
        insp = await client.get(f"{docker_control.API_PREFIX}/images/{image}/json")
        if insp.status_code == 200:
            return
        # Pull
        repo = image
        tag = "latest"
        if ":" in image and "/" not in image.split(":")[-1]:
            # naive split — docker:27-cli → repo docker tag 27-cli
            repo, tag = image.rsplit(":", 1)
        elif image.count(":") >= 1 and not image.endswith(":"):
            # ghcr.io/foo/bar:tag
            repo, tag = image.rsplit(":", 1)
        params = {"fromImage": repo, "tag": tag}
        resp = await client.post(
            f"{docker_control.API_PREFIX}/images/create",
            params=params,
            timeout=300.0,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"Failed to pull {image}: HTTP {resp.status_code} {resp.text[:200]}")


async def _start_update_container(host_root: str) -> str:
    await _ensure_image(UPDATE_IMAGE)
    sock = docker_control.DOCKER_SOCK
    # Sidecar runs as root; host checkout is often uid 1000 → git "dubious ownership"
    # unless safe.directory is set. Container path is always /library (bind target).
    #
    # IMPORTANT: this container must outlive the app process. ``update_library.sh``
    # recreates ``audiobook-request``; the app must not ``wait``/force-delete the
    # sidecar (that killed mid-recreate and left the stack down).
    cmd = (
        "apk add --no-cache git bash >/dev/null && "
        "git config --global --add safe.directory /library && "
        "git config --global --add safe.directory '*' && "
        "bash /library/scripts/admin_server_update.sh"
    )
    body = {
        "Image": UPDATE_IMAGE,
        "Entrypoint": ["sh", "-c"],
        "Cmd": [cmd],
        "WorkingDir": "/library",
        "Env": [
            "LIBRARY_UPDATE_YES=1",
            "GIT_TERMINAL_PROMPT=0",
            f"LIBRARY_HOST_ROOT_BIND={host_root}",
        ],
        "Labels": {
            "com.library.server-update": "1",
        },
        "HostConfig": {
            "Binds": [
                f"{host_root}:/library",
                f"{sock}:/var/run/docker.sock",
            ],
            "AutoRemove": False,
            "NetworkMode": "bridge",
            # Survive if someone stops the compose project; only explicit delete removes it.
            "RestartPolicy": {"Name": "no"},
        },
    }
    async with _docker_client() as client:
        create = await client.post(
            f"{docker_control.API_PREFIX}/containers/create",
            params={"name": f"library-server-update-{int(time.time())}"},
            json=body,
        )
        if create.status_code == 409:
            # name conflict — retry without fixed name
            create = await client.post(
                f"{docker_control.API_PREFIX}/containers/create",
                json=body,
            )
        if create.status_code >= 400:
            raise RuntimeError(
                f"Failed to create update container: HTTP {create.status_code} {create.text[:300]}"
            )
        cid = str((create.json() or {}).get("Id") or "")
        if not cid:
            raise RuntimeError("Docker create returned no container id")
        start = await client.post(f"{docker_control.API_PREFIX}/containers/{cid}/start")
        if start.status_code >= 400:
            raise RuntimeError(
                f"Failed to start update container: HTTP {start.status_code} {start.text[:300]}"
            )
        return cid


async def _cleanup_container(cid: str, *, force: bool = True) -> None:
    try:
        async with _docker_client() as client:
            await client.delete(
                f"{docker_control.API_PREFIX}/containers/{cid}",
                params={"force": "true" if force else "false"},
            )
    except Exception as e:
        logger.debug("cleanup update container failed: %s", e)


async def _list_update_sidecars(*, all_containers: bool = True) -> list[dict[str, Any]]:
    """Containers labeled com.library.server-update=1."""
    if not docker_control.socket_available():
        return []
    try:
        async with _docker_client() as client:
            resp = await client.get(
                f"{docker_control.API_PREFIX}/containers/json",
                params={
                    "all": "true" if all_containers else "false",
                    "filters": json.dumps({"label": ["com.library.server-update=1"]}),
                },
            )
            if resp.status_code >= 400:
                return []
            data = resp.json()
            return data if isinstance(data, list) else []
    except Exception as e:
        logger.debug("list update sidecars failed: %s", e)
        return []


async def _update_sidecar_running() -> bool:
    for c in await _list_update_sidecars(all_containers=True):
        state = str((c.get("State") or "")).lower()
        if state == "running":
            return True
    return False


async def _reap_exited_update_sidecars() -> None:
    """Remove finished update sidecars (never force-kill a running one)."""
    for c in await _list_update_sidecars(all_containers=True):
        state = str((c.get("State") or "")).lower()
        cid = str(c.get("Id") or "")
        if cid and state in ("exited", "dead", "created"):
            await _cleanup_container(cid, force=True)


def is_apply_running() -> bool:
    job = _job_snapshot()
    if job.get("running") or str(job.get("phase") or "") in ("updating", "validating"):
        return True
    global _task
    return _task is not None and not _task.done()


async def _launch_detached_update(host_root: str) -> str:
    """Start the update sidecar and return immediately — do not wait or delete it.

    Waiting from the app process is unsafe: compose recreates this container, and
    task cancellation used to force-delete the sidecar mid-recreate.
    """
    cid = await _start_update_container(host_root)
    job = _read_json(JOB_FILE) or {}
    job.update(
        {
            "phase": "updating",
            "running": True,
            "containerId": cid,
            "hostRoot": host_root,
            "detached": True,
            "updatedAt": _now_iso(),
        }
    )
    _write_json(JOB_FILE, job)
    try:
        with JOB_LOG.open("a", encoding="utf-8") as fh:
            fh.write(
                f"[server_update] detached sidecar id={cid[:12]} "
                f"(app may restart; sidecar continues on host)\n"
            )
    except Exception:
        pass
    return cid


def _reset_job_state(
    *,
    phase: str,
    running: bool,
    error: str | None = None,
    ok: bool | None = None,
    host_root: str | None = None,
    host_root_source: str | None = None,
    log_line: str | None = None,
) -> None:
    """Replace job JSON + log so the UI never keeps a previous failure alongside a new attempt."""
    started = _now_iso()
    payload: dict[str, Any] = {
        "phase": phase,
        "running": running,
        "ok": ok,
        "error": error,
        "startedAt": started,
        "finishedAt": None if running else started,
        "updatedAt": started,
        "containerId": None,
    }
    if host_root is not None:
        payload["hostRoot"] = host_root
    if host_root_source is not None:
        payload["hostRootSource"] = host_root_source
    _write_json(JOB_FILE, payload)
    JOB_LOG.parent.mkdir(parents=True, exist_ok=True)
    JOB_LOG.write_text(log_line or "", encoding="utf-8")


async def start_apply() -> dict[str, Any]:
    """Start detached host update sidecar; return immediately for job polling."""
    global _task
    _task = None  # legacy; apply no longer depends on an in-process waiter

    if await _update_sidecar_running() or is_apply_running():
        # Reconcile stale "running" with no sidecar before blocking the user.
        job = await _reconcile_job_with_sidecars()
        if await _update_sidecar_running() or job.get("running"):
            return {
                "ok": True,
                "started": False,
                "already_running": True,
                "message": "Server update already running",
                "job": job,
            }

    # Clear stale job/log immediately so the UI shows this attempt, not an old failure.
    _reset_job_state(
        phase="validating",
        running=True,
        log_line=f"[server_update] validating host install root {_now_iso()}\n",
    )

    if not docker_control.socket_available():
        msg = "docker.sock not available — mount it into the app container"
        _reset_job_state(phase="failed", running=False, ok=False, error=msg, log_line=f"[server_update] {msg}\n")
        raise RuntimeError(msg)

    root = await resolve_validated_host_root()
    host_root = root.get("hostRoot")
    if not host_root:
        msg = str(root.get("error") or "Host project directory unavailable")
        _reset_job_state(
            phase="failed",
            running=False,
            ok=False,
            error=msg,
            log_line=f"[server_update] {msg}\n",
        )
        raise RuntimeError(msg)

    _reset_job_state(
        phase="updating",
        running=True,
        host_root=str(host_root),
        host_root_source=root.get("source"),
        log_line=(
            f"[server_update] starting detached sidecar image={UPDATE_IMAGE} "
            f"hostRoot={host_root} (bind → /library)\n"
        ),
    )
    try:
        cid = await _launch_detached_update(str(host_root))
    except Exception as e:
        _reset_job_state(
            phase="failed",
            running=False,
            ok=False,
            error=str(e),
            host_root=str(host_root),
            log_line=f"[server_update] failed to start sidecar: {e}\n",
        )
        raise RuntimeError(str(e)) from e

    return {
        "ok": True,
        "started": True,
        "already_running": False,
        "message": (
            "Server update started — git reset --hard to origin/main and rebuild "
            "the stack. The app will restart mid-update; keep this page open and "
            "poll until the job finishes."
        ),
        "job": _job_snapshot(),
        "hostRoot": host_root,
        "hostRootSource": root.get("source"),
        "containerId": cid,
    }


async def get_job() -> dict[str, Any]:
    return await _reconcile_job_with_sidecars()