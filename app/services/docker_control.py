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

import httpx

logger = logging.getLogger(__name__)

Action = Literal["start", "stop", "restart"]
DOCKER_SOCK = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")
API_PREFIX = "/v1.41"
SELF_RESTART_DELAY_SEC = 2.0


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
        container="audiobookshelf",
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


async def list_services() -> dict[str, Any]:
    """Return managed services and live Docker state (best-effort)."""
    available = socket_available()
    services: list[dict[str, Any]] = []
    socket_error: str | None = None

    if not available:
        socket_error = f"Docker socket not mounted ({DOCKER_SOCK})"
        for svc in MANAGED_SERVICES.values():
            services.append(_service_payload(svc, state=None, available=False))
        return {
            "available": False,
            "socket": DOCKER_SOCK,
            "error": socket_error,
            "services": services,
            "byHealthKey": {k: v for k, v in HEALTH_KEY_TO_SERVICE.items()},
            "appServiceId": "app",
        }

    for svc in MANAGED_SERVICES.values():
        state: dict[str, Any] | None = None
        err: str | None = None
        try:
            info = await _inspect(svc.container)
            state = _state_from_inspect(info)
        except PermissionError as e:
            socket_error = str(e)
            err = str(e)
        except Exception as e:
            err = str(e)[:200]
            logger.debug("docker inspect %s failed: %s", svc.container, e)
        payload = _service_payload(svc, state=state, available=err is None and available)
        if err:
            payload["error"] = err
        services.append(payload)

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
) -> dict[str, Any]:
    return {
        "id": svc.id,
        "label": svc.label,
        "container": svc.container,
        "composeService": svc.compose_service,
        "healthKey": svc.health_key,
        "isSelf": svc.is_self,
        "actions": allowed_actions(svc),
        "available": available,
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

    if svc.is_self and action == "restart":
        asyncio.create_task(_deferred_restart(svc.container))
        return {
            "ok": True,
            "serviceId": svc.id,
            "action": action,
            "deferred": True,
            "message": (
                f"{svc.label} restart scheduled in {int(SELF_RESTART_DELAY_SEC)}s "
                "(API will briefly drop while the container restarts)"
            ),
            "container": svc.container,
        }

    await _engine_action(svc.container, action)
    # Best-effort fresh state
    state = None
    try:
        state = _state_from_inspect(await _inspect(svc.container))
    except Exception:
        pass

    verb = {"start": "started", "stop": "stopped", "restart": "restarted"}[action]
    return {
        "ok": True,
        "serviceId": svc.id,
        "action": action,
        "deferred": False,
        "message": f"{svc.label} {verb}",
        "container": svc.container,
        "state": state,
    }
