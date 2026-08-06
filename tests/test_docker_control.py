"""Docker control allowlist and self-container guards."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from app.services import docker_control


def test_managed_services_cover_health_containers():
    ids = set(docker_control.MANAGED_SERVICES)
    assert ids >= {
        "app",
        "prowlarr",
        "jackett",
        "flaresolverr",
        "gluetun",
        "audiobookshelf",
        "kavita",
        "libraforge",
    }
    assert docker_control.HEALTH_KEY_TO_SERVICE["mullvad_proxy"] == "gluetun"
    assert docker_control.HEALTH_KEY_TO_SERVICE["prowlarr"] == "prowlarr"


def test_app_self_actions_are_restart_only():
    app = docker_control.MANAGED_SERVICES["app"]
    assert app.is_self
    assert docker_control.allowed_actions(app) == ["restart"]


def test_control_rejects_app_stop():
    async def _run():
        with patch.object(docker_control, "socket_available", return_value=True):
            with pytest.raises(PermissionError, match="Cannot stop"):
                await docker_control.control_service("app", "stop")

    asyncio.run(_run())


def test_control_schedules_deferred_app_restart():
    async def _run():
        created: list = []

        async def fake_deferred(container: str) -> None:
            created.append(container)

        with (
            patch.object(docker_control, "socket_available", return_value=True),
            patch.object(docker_control, "_deferred_restart", side_effect=fake_deferred),
            patch.object(docker_control, "SELF_RESTART_DELAY_SEC", 0),
        ):
            out = await docker_control.control_service("app", "restart")
            assert out["ok"] is True
            assert out["deferred"] is True
            await asyncio.sleep(0.05)
        assert created == ["audiobook-request"]

    asyncio.run(_run())


def test_control_unknown_service():
    async def _run():
        with pytest.raises(KeyError):
            await docker_control.control_service("nope", "restart")

    asyncio.run(_run())


def test_list_services_without_socket():
    async def _run():
        with patch.object(docker_control, "socket_available", return_value=False):
            out = await docker_control.list_services()
        assert out["available"] is False
        assert len(out["services"]) == len(docker_control.MANAGED_SERVICES)
        assert out["appServiceId"] == "app"
        abs_svc = next(s for s in out["services"] if s["id"] == "audiobookshelf")
        assert abs_svc["container"] == "audiobookshelf-server"
        assert "openUrl" in abs_svc
        assert abs_svc.get("stats") is None

    asyncio.run(_run())
