"""Admin server stack update via host git + docker compose bridge.

The app container typically has no writable .git checkout. Updates run in a
one-shot sidecar with the compose project directory and docker.sock mounted,
invoking scripts/admin_server_update.sh (same work as update_library.sh).

Version checks prefer GitHub compare against data/install_revision.json when
live git is unavailable inside the app.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
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


async def discover_host_root() -> dict[str, Any]:
    """Locate the compose project directory on the Docker host."""
    configured = _configured_host_root()
    if configured:
        return {
            "hostRoot": configured,
            "containerRoot": str(HOST_MOUNT) if HOST_MOUNT.is_dir() else None,
            "source": "env",
            "error": None,
        }

    if not docker_control.socket_available():
        return {
            "hostRoot": None,
            "containerRoot": None,
            "source": None,
            "error": "docker.sock not available",
        }

    try:
        info = await docker_control._inspect(SELF_CONTAINER)  # noqa: SLF001 — shared sock helper
    except Exception as e:
        return {
            "hostRoot": None,
            "containerRoot": None,
            "source": None,
            "error": f"inspect failed: {e}",
        }

    if not info:
        return {
            "hostRoot": None,
            "containerRoot": None,
            "source": None,
            "error": f"container {SELF_CONTAINER} not found",
        }

    labels = (info.get("Config") or {}).get("Labels") or {}
    working = (
        labels.get("com.docker.compose.project.working_dir")
        or labels.get("com.docker.compose.project.workingdir")
        or ""
    ).strip()
    if working:
        return {
            "hostRoot": working,
            "containerRoot": None,
            "source": "compose_label",
            "error": None,
        }

    for mount in info.get("Mounts") or []:
        if not isinstance(mount, dict):
            continue
        dest = str(mount.get("Destination") or "")
        src = str(mount.get("Source") or "")
        if dest in ("/app/data", "/library-host") and src:
            root = str(Path(src).parent) if dest == "/app/data" else src
            return {
                "hostRoot": root,
                "containerRoot": "/library-host" if dest == "/library-host" else None,
                "source": "mount",
                "error": None,
            }

    return {
        "hostRoot": None,
        "containerRoot": None,
        "source": None,
        "error": "could not resolve compose project directory from container labels/mounts",
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


async def get_status() -> dict[str, Any]:
    local = await get_local_version()
    root = await discover_host_root()
    job = _job_snapshot()
    apply_ready = bool(root.get("hostRoot")) and docker_control.socket_available()
    return {
        "local": local,
        "remote": None,
        "state": "unknown",
        "branch": DEFAULT_BRANCH,
        "remoteName": DEFAULT_REMOTE,
        "repo": await _github_repo(),
        "hostRoot": root.get("hostRoot"),
        "hostRootSource": root.get("source"),
        "applyAvailable": apply_ready,
        "applyUnavailableReason": None
        if apply_ready
        else (root.get("error") or "Host project directory or docker.sock unavailable"),
        "dockerSocket": docker_control.socket_available(),
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
    cmd = (
        "apk add --no-cache git bash >/dev/null && "
        "bash /library/scripts/admin_server_update.sh"
    )
    body = {
        "Image": UPDATE_IMAGE,
        "Cmd": ["sh", "-c", cmd],
        "WorkingDir": "/library",
        "Env": [
            "LIBRARY_UPDATE_YES=1",
            "GIT_TERMINAL_PROMPT=0",
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


async def _await_container(cid: str) -> int:
    async with _docker_client() as client:
        # Wait up to 45 minutes for compose build
        resp = await client.post(
            f"{docker_control.API_PREFIX}/containers/{cid}/wait",
            timeout=2700.0,
        )
        if resp.status_code >= 400:
            return 1
        data = resp.json() or {}
        return int(data.get("StatusCode") or 0)


async def _cleanup_container(cid: str) -> None:
    try:
        async with _docker_client() as client:
            await client.delete(
                f"{docker_control.API_PREFIX}/containers/{cid}",
                params={"force": "true"},
            )
    except Exception as e:
        logger.debug("cleanup update container failed: %s", e)


def is_apply_running() -> bool:
    job = _job_snapshot()
    if job.get("running"):
        return True
    global _task
    return _task is not None and not _task.done()


async def _run_apply_job(host_root: str) -> None:
    cid: str | None = None
    try:
        _write_json(
            JOB_FILE,
            {
                "phase": "updating",
                "running": True,
                "ok": None,
                "error": None,
                "startedAt": _now_iso(),
                "finishedAt": None,
                "updatedAt": _now_iso(),
                "hostRoot": host_root,
            },
        )
        JOB_LOG.write_text(
            f"[server_update] starting sidecar image={UPDATE_IMAGE} root={host_root}\n",
            encoding="utf-8",
        )
        cid = await _start_update_container(host_root)
        job = _read_json(JOB_FILE) or {}
        job.update({"containerId": cid, "updatedAt": _now_iso()})
        _write_json(JOB_FILE, job)

        code = await _await_container(cid)
        # Prefer sidecar-written status; fill gaps if needed.
        job = _read_json(JOB_FILE) or {}
        if job.get("phase") in (None, "updating", "idle"):
            job.update(
                {
                    "phase": "succeeded" if code == 0 else "failed",
                    "running": False,
                    "ok": code == 0,
                    "error": None if code == 0 else f"update container exited {code}",
                    "finishedAt": _now_iso(),
                    "updatedAt": _now_iso(),
                }
            )
            _write_json(JOB_FILE, job)
        else:
            job["running"] = False
            job["updatedAt"] = _now_iso()
            _write_json(JOB_FILE, job)
    except Exception as e:
        logger.exception("server update apply failed")
        _write_json(
            JOB_FILE,
            {
                **(_read_json(JOB_FILE) or {}),
                "phase": "failed",
                "running": False,
                "ok": False,
                "error": str(e),
                "finishedAt": _now_iso(),
                "updatedAt": _now_iso(),
            },
        )
        try:
            with JOB_LOG.open("a", encoding="utf-8") as fh:
                fh.write(f"[server_update] error: {e}\n")
        except Exception:
            pass
    finally:
        if cid:
            await _cleanup_container(cid)


async def start_apply() -> dict[str, Any]:
    """Kick async host update. Returns immediately for polling."""
    global _task
    if is_apply_running():
        return {
            "ok": True,
            "started": False,
            "already_running": True,
            "message": "Server update already running",
            "job": _job_snapshot(),
        }

    root = await discover_host_root()
    host_root = root.get("hostRoot")
    if not host_root:
        raise RuntimeError(root.get("error") or "Host project directory unavailable")
    if not docker_control.socket_available():
        raise RuntimeError("docker.sock not available — mount it into the app container")

    _write_json(
        JOB_FILE,
        {
            "phase": "updating",
            "running": True,
            "ok": None,
            "error": None,
            "startedAt": _now_iso(),
            "finishedAt": None,
            "updatedAt": _now_iso(),
            "hostRoot": host_root,
        },
    )
    _task = asyncio.create_task(_run_apply_job(str(host_root)))
    return {
        "ok": True,
        "started": True,
        "already_running": False,
        "message": (
            "Server update started — git reset --hard to origin/main and rebuild "
            "the stack. The app may restart mid-update; keep this page open."
        ),
        "job": _job_snapshot(),
    }


def get_job() -> dict[str, Any]:
    return _job_snapshot()