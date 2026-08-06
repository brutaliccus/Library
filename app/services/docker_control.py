"""Admin Docker container control via the Docker Engine API (unix socket).

Requires `/var/run/docker.sock` mounted into the app container and membership
in the host `docker` group (`DOCKER_GID` + compose `group_add`).
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

Action = Literal["start", "stop", "restart"]
DOCKER_SOCK = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")
API_PREFIX = "/v1.41"
SELF_RESTART_DELAY_SEC = 2.0

# Alternate container_name values seen across installs (Pi external stack vs bundled compose).
CONTAINER_ALIASES: dict[str, tuple[str, ...]] = {
    "audiobookshelf-server": ("audiobookshelf-server", "audiobookshelf"),
}

# Preferred container-side port when picking a published HostPort for Open links.
PREFERRED_CONTAINER_PORTS: dict[str, int] = {
    "app": 8080,
    "prowlarr": 9696,
    "jackett": 9117,
    "flaresolverr": 8191,
    "audiobookshelf": 80,
    "kavita": 5000,
    "libraforge": 5056,
}

# Fallback host ports when inspect has no binding (compose defaults).
DEFAULT_HOST_PORTS: dict[str, int] = {
    "app": 8085,
    "prowlarr": 9696,
    "jackett": 9117,
    "flaresolverr": 8191,
    "audiobookshelf": 13378,
    "kavita": 5000,
    "libraforge": 5056,
}


@dataclass(frozen=True)
class ManagedService:
    id: str
    label: str
    container: str
    compose_service: str
    health_key: str | None  # Admin HealthTab probe key, if any
    is_self: bool = False
    allow_start: bool = True
    allow_stop: bool = True
    allow_restart: bool = True


# Container names match docker-compose.yml `container_name` values.
MANAGED_SERVICES: dict[str, ManagedService] = {
    "app": ManagedService(
        id="app",
        label="Library App",
        container="audiobook-request",
        compose_service="app",
        health_key=None,  # Uptime card
        is_self=True,
        allow_start=False,
        allow_stop=False,
        allow_restart=True,
    ),
    "prowlarr": ManagedService(
        id="prowlarr",
        label="Prowlarr",
        container="audiobook-prowlarr",
        compose_service="prowlarr",
        health_key="prowlarr",
    ),
    "jackett": ManagedService(
        id="jackett",
        label="Jackett",
        container="audiobook-jackett",
        compose_service="jackett",
        health_key="jackett",
    ),
    "flaresolverr": ManagedService(
        id="flaresolverr",
        label="FlareSolverr",
        container="audiobook-flaresolverr",
        compose_service="flaresolverr",
        health_key="flaresolverr",
    ),
    "gluetun": ManagedService(
        id="gluetun",
        label="Mullvad (gluetun)",
        container="audiobook-gluetun",
        compose_service="gluetun",
        health_key="mullvad_proxy",
    ),
    "audiobookshelf": ManagedService(
        id="audiobookshelf",
        label="Audiobookshelf",
        # Pi stack (/opt/stacks/audiobookshelf) uses container_name audiobookshelf-server
        container="audiobookshelf-server",
        compose_service="audiobookshelf",
        health_key="audiobookshelf",
    ),
    "kavita": ManagedService(
        id="kavita",
        label="Kavita",
        container="kavita",
        compose_service="kavita",
        health_key="kavita",
    ),
    "libraforge": ManagedService(
        id="libraforge",
        label="LibraForge",
        container="libraforge",
        compose_service="libraforge",
        health_key="libraforge",
    ),
}

HEALTH_KEY_TO_SERVICE: dict[str, str] = {
    svc.health_key: svc.id for svc in MANAGED_SERVICES.values() if svc.health_key
}


def socket_available() -> bool:
    return os.path.exists(DOCKER_SOCK)


def allowed_actions(svc: ManagedService) -> list[Action]:
    out: list[Action] = []
    if svc.allow_start:
        out.append("start")
    if svc.allow_stop:
        out.append("stop")
    if svc.allow_restart:
        out.append("restart")
    return out


def get_service(service_id: str) -> ManagedService | None:
    return MANAGED_SERVICES.get(service_id)


def _container_candidates(svc: ManagedService) -> tuple[str, ...]:
    aliases = CONTAINER_ALIASES.get(svc.container)
    if aliases:
        return aliases
    return (svc.container,)


def _client() -> httpx.AsyncClient:
    transport = httpx.AsyncHTTPTransport(uds=DOCKER_SOCK)
    return httpx.AsyncClient(
        transport=transport,
        base_url="http://docker",
        timeout=60.0,
    )


async def _inspect(container: str) -> dict[str, Any] | None:
    async with _client() as client:
        resp = await client.get(f"{API_PREFIX}/containers/{container}/json")
        if resp.status_code == 404:
            return None
        if resp.status_code == 403 or resp.status_code == 401:
            raise PermissionError(
                "Docker socket permission denied — set DOCKER_GID to the host "
                "docker group id and recreate the app container"
            )
        if resp.status_code >= 400:
            raise RuntimeError(f"Docker inspect failed: HTTP {resp.status_code} {resp.text[:200]}")
        return resp.json()


async def _inspect_service(svc: ManagedService) -> tuple[str, dict[str, Any] | None]:
    """Inspect the first matching container name (supports ABS aliases)."""
    last_name = svc.container
    for name in _container_candidates(svc):
        last_name = name
        info = await _inspect(name)
        if info is not None:
            return name, info
    return last_name, None


def _state_from_inspect(info: dict[str, Any] | None) -> dict[str, Any]:
    if not info:
        return {
            "exists": False,
            "running": False,
            "status": "not_found",
            "startedAt": None,
        }
    state = info.get("State") or {}
    return {
        "exists": True,
        "running": bool(state.get("Running")),
        "status": str(state.get("Status") or "unknown"),
        "startedAt": state.get("StartedAt"),
        "exitCode": state.get("ExitCode"),
        "error": state.get("Error") or None,
    }


def _cpu_percent(stats: dict[str, Any]) -> float | None:
    """Docker CLI-compatible CPU percentage from a one-shot stats payload."""
    try:
        cpu = stats.get("cpu_stats") or {}
        precpu = stats.get("precpu_stats") or {}
        cpu_usage = (cpu.get("cpu_usage") or {}).get("total_usage")
        precpu_usage = (precpu.get("cpu_usage") or {}).get("total_usage")
        system = cpu.get("system_cpu_usage")
        presystem = precpu.get("system_cpu_usage")
        if None in (cpu_usage, precpu_usage, system, presystem):
            return None
        cpu_delta = float(cpu_usage) - float(precpu_usage)
        system_delta = float(system) - float(presystem)
        if system_delta <= 0 or cpu_delta < 0:
            return None
        online = cpu.get("online_cpus")
        if not online:
            percpu = (cpu.get("cpu_usage") or {}).get("percpu_usage") or []
            online = len(percpu) or 1
        return round((cpu_delta / system_delta) * float(online) * 100.0, 1)
    except Exception:
        return None


def _memory_stats(stats: dict[str, Any]) -> dict[str, Any]:
    mem = stats.get("memory_stats") or {}
    usage = int(mem.get("usage") or 0)
    limit = int(mem.get("limit") or 0)
    detail = mem.get("stats") or {}
    # Prefer working set (usage minus cache) when cgroup reports cache.
    cache = int(detail.get("cache") or detail.get("total_cache") or 0)
    used = max(0, usage - cache) if cache and usage >= cache else usage
    percent = round((used / limit) * 100.0, 1) if limit > 0 else None
    return {
        "usageBytes": used or None,
        "limitBytes": limit or None,
        "percent": percent,
    }


async def _container_stats(container: str) -> dict[str, Any] | None:
    """One-shot Docker stats (CPU / memory). Returns None if unavailable."""
    try:
        async with _client() as client:
            resp = await client.get(
                f"{API_PREFIX}/containers/{container}/stats",
                params={"stream": "false", "one-shot": "true"},
                timeout=8.0,
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
    except Exception as e:
        logger.debug("docker stats %s failed: %s", container, e)
        return None

    mem = _memory_stats(data)
    return {
        "cpuPercent": _cpu_percent(data),
        "memoryUsageBytes": mem["usageBytes"],
        "memoryLimitBytes": mem["limitBytes"],
        "memoryPercent": mem["percent"],
    }


def _host_ports_from_inspect(info: dict[str, Any] | None) -> list[tuple[int, int]]:
    """Return (container_port, host_port) pairs from NetworkSettings.Ports."""
    if not info:
        return []
    ports = ((info.get("NetworkSettings") or {}).get("Ports")) or {}
    out: list[tuple[int, int]] = []
    for key, bindings in ports.items():
        if not bindings:
            continue
        try:
            container_port = int(str(key).split("/")[0])
        except ValueError:
            continue
        for binding in bindings:
            if not isinstance(binding, dict):
                continue
            hp = binding.get("HostPort")
            if not hp:
                continue
            try:
                out.append((container_port, int(hp)))
            except (TypeError, ValueError):
                continue
    return out


def _is_browser_reachable_url(url: str | None) -> bool:
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    # Docker DNS / bridge hosts are not reachable from the admin browser.
    if host in {
        "audiobookshelf",
        "audiobookshelf-server",
        "kavita",
        "libraforge",
        "audiobook-prowlarr",
        "prowlarr",
        "audiobook-jackett",
        "jackett",
        "flaresolverr",
        "audiobook-flaresolverr",
        "gluetun",
        "audiobook-request",
        "app",
    }:
        return False
    if host.endswith(".internal") or host.startswith("172.17."):
        return False
    return True


def _public_url_for_service(svc: ManagedService) -> str | None:
    """Prefer configured public / NPM URLs for Open links."""
    if svc.id == "libraforge":
        try:
            from app.config import get_settings

            url = (get_settings().libraforge_url or "").strip().rstrip("/")
            if _is_browser_reachable_url(url):
                return url
        except Exception:
            pass
        return None

    if svc.id == "audiobookshelf":
        domain = (os.environ.get("NPM_ABS_DOMAIN") or "").strip()
        if domain:
            return f"https://{domain}"
        try:
            from app.config import get_settings

            url = (get_settings().abs_url or "").strip().rstrip("/")
            if _is_browser_reachable_url(url):
                return url
        except Exception:
            pass
        return None

    if svc.id == "kavita":
        domain = (os.environ.get("NPM_KAVITA_DOMAIN") or "").strip()
        if domain:
            return f"https://{domain}"
        try:
            from app.config import get_settings

            url = (get_settings().kavita_url or "").strip().rstrip("/")
            if _is_browser_reachable_url(url):
                return url
        except Exception:
            pass
        return None

    if svc.id == "app":
        try:
            from app.config import get_settings

            url = (get_settings().app_url or "").strip().rstrip("/")
            if _is_browser_reachable_url(url):
                return url
        except Exception:
            pass
        return None

    return None


def _open_url_for_service(
    svc: ManagedService,
    *,
    info: dict[str, Any] | None,
) -> str | None:
    public = _public_url_for_service(svc)
    if public:
        return public

    # gluetun has no admin UI worth opening
    if svc.id == "gluetun":
        return None

    preferred = PREFERRED_CONTAINER_PORTS.get(svc.id)
    host_port: int | None = None
    for cport, hport in _host_ports_from_inspect(info):
        if preferred is not None and cport == preferred:
            host_port = hport
            break
        if host_port is None:
            host_port = hport
    if host_port is None:
        host_port = DEFAULT_HOST_PORTS.get(svc.id)
    if host_port is None:
        return None
    # Frontend rewrites 127.0.0.1 → window.location.hostname for LAN browsers.
    return f"http://127.0.0.1:{host_port}"


async def list_services() -> dict[str, Any]:
    """Return managed services and live Docker state (best-effort)."""
    available = socket_available()
    services: list[dict[str, Any]] = []
    socket_error: str | None = None

    if not available:
        socket_error = f"Docker socket not mounted ({DOCKER_SOCK})"
        for svc in MANAGED_SERVICES.values():
            services.append(
                _service_payload(
                    svc,
                    state=None,
                    available=False,
                    container_name=svc.container,
                    stats=None,
                    open_url=_open_url_for_service(svc, info=None),
                )
            )
        return {
            "available": False,
            "socket": DOCKER_SOCK,
            "error": socket_error,
            "services": services,
            "byHealthKey": {k: v for k, v in HEALTH_KEY_TO_SERVICE.items()},
            "appServiceId": "app",
        }

    async def _one(svc: ManagedService) -> dict[str, Any]:
        nonlocal socket_error
        state: dict[str, Any] | None = None
        err: str | None = None
        stats: dict[str, Any] | None = None
        container_name = svc.container
        info: dict[str, Any] | None = None
        try:
            container_name, info = await _inspect_service(svc)
            state = _state_from_inspect(info)
            if state.get("running"):
                stats = await _container_stats(container_name)
        except PermissionError as e:
            socket_error = str(e)
            err = str(e)
        except Exception as e:
            err = str(e)[:200]
            logger.debug("docker inspect %s failed: %s", svc.container, e)
        payload = _service_payload(
            svc,
            state=state,
            available=err is None and available,
            container_name=container_name,
            stats=stats,
            open_url=_open_url_for_service(svc, info=info),
        )
        if err:
            payload["error"] = err
        return payload

    services = list(await asyncio.gather(*[_one(svc) for svc in MANAGED_SERVICES.values()]))

    return {
        "available": available and socket_error is None,
        "socket": DOCKER_SOCK,
        "error": socket_error,
        "services": services,
        "byHealthKey": {k: v for k, v in HEALTH_KEY_TO_SERVICE.items()},
        "appServiceId": "app",
    }


def _service_payload(
    svc: ManagedService,
    *,
    state: dict[str, Any] | None,
    available: bool,
    container_name: str | None = None,
    stats: dict[str, Any] | None = None,
    open_url: str | None = None,
) -> dict[str, Any]:
    return {
        "id": svc.id,
        "label": svc.label,
        "container": container_name or svc.container,
        "composeService": svc.compose_service,
        "healthKey": svc.health_key,
        "isSelf": svc.is_self,
        "actions": allowed_actions(svc),
        "available": available,
        "stats": stats,
        "openUrl": open_url,
        "state": state
        or {
            "exists": False,
            "running": False,
            "status": "unknown",
            "startedAt": None,
        },
    }


async def _engine_action(container: str, action: Action) -> None:
    path = f"{API_PREFIX}/containers/{container}/{action}"
    # stop/restart accept optional t= timeout query
    params = {"t": "20"} if action in ("stop", "restart") else None
    async with _client() as client:
        resp = await client.post(path, params=params)
        if resp.status_code == 404:
            raise FileNotFoundError(
                f"Container '{container}' not found — create it with "
                f"docker compose up -d (enable the matching compose profile if needed)"
            )
        if resp.status_code in (401, 403):
            raise PermissionError(
                "Docker socket permission denied — set DOCKER_GID to the host "
                "docker group id and recreate the app container"
            )
        # 204 No Content = success; 304 = already in desired state for start/stop
        if resp.status_code in (204, 304):
            return
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Docker {action} failed: HTTP {resp.status_code} {resp.text[:240]}"
            )


async def _deferred_restart(container: str) -> None:
    try:
        await asyncio.sleep(SELF_RESTART_DELAY_SEC)
        await _engine_action(container, "restart")
    except Exception:
        logger.exception("Deferred restart of %s failed", container)


async def _resolve_container_name(svc: ManagedService) -> str:
    """Pick the live container name (ABS may be audiobookshelf-server or audiobookshelf)."""
    try:
        name, info = await _inspect_service(svc)
        if info is not None:
            return name
        return name or svc.container
    except Exception as e:
        logger.debug("resolve container name for %s failed: %s", svc.id, e)
        return svc.container


async def control_service(service_id: str, action: Action) -> dict[str, Any]:
    """Run start/stop/restart for a managed service. Admin-only caller required."""
    if action not in ("start", "stop", "restart"):
        raise ValueError(f"Unsupported action: {action}")

    svc = get_service(service_id)
    if not svc:
        raise KeyError(f"Unknown service: {service_id}")

    allowed = allowed_actions(svc)
    if action not in allowed:
        if svc.is_self and action == "stop":
            raise PermissionError(
                "Cannot stop the Library app from itself (would kill this request). "
                "Use SSH: docker compose stop app"
            )
        if svc.is_self and action == "start":
            raise PermissionError(
                "Cannot start the Library app from itself — it is already running"
            )
        raise PermissionError(f"Action '{action}' is not allowed for {svc.label}")

    if not socket_available():
        raise RuntimeError(f"Docker socket not available ({DOCKER_SOCK})")

    # Self-restart: use the known container name without inspecting first so the
    # HTTP response can return before the process exits (and tests stay offline).
    if svc.is_self and action == "restart":
        container = svc.container
        asyncio.create_task(_deferred_restart(container))
        return {
            "ok": True,
            "serviceId": svc.id,
            "action": action,
            "deferred": True,
            "message": (
                f"{svc.label} restart scheduled in {int(SELF_RESTART_DELAY_SEC)}s "
                "(API will briefly drop while the container restarts)"
            ),
            "container": container,
        }

    container = await _resolve_container_name(svc)
    await _engine_action(container, action)
    # Best-effort fresh state
    state = None
    try:
        state = _state_from_inspect(await _inspect(container))
    except Exception:
        pass

    verb = {"start": "started", "stop": "stopped", "restart": "restarted"}[action]
    return {
        "ok": True,
        "serviceId": svc.id,
        "action": action,
        "deferred": False,
        "message": f"{svc.label} {verb}",
        "container": container,
        "state": state,
    }
