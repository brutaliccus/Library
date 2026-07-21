"""LibraForge health probe — fail-open when sibling stack is down."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from app.services import health_checks


def test_probe_libraforge_ok_on_health():
    async def _run():
        settings = MagicMock()
        settings.libraforge_url = "https://forge.library.freiverse.com"
        settings.libraforge_internal_url = "http://172.17.0.1:5056"

        resp = MagicMock()
        resp.status_code = 200

        client = AsyncMock()
        client.get = AsyncMock(return_value=resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch.object(health_checks, "get_settings", return_value=settings),
            patch.object(health_checks.httpx, "AsyncClient", return_value=client),
        ):
            out = await health_checks._probe_libraforge()

        assert out["configured"] is True
        assert out["connected"] is True
        assert out["url"] == "https://forge.library.freiverse.com"
        assert out["internal_url"] == "http://172.17.0.1:5056"
        client.get.assert_awaited_with("http://172.17.0.1:5056/health")

    asyncio.run(_run())


def test_probe_libraforge_falls_back_to_root():
    async def _run():
        settings = MagicMock()
        settings.libraforge_url = "https://forge.library.freiverse.com"
        settings.libraforge_internal_url = "http://172.17.0.1:5056"

        health_resp = MagicMock()
        health_resp.status_code = 404
        root_resp = MagicMock()
        root_resp.status_code = 200

        client = AsyncMock()
        client.get = AsyncMock(side_effect=[health_resp, root_resp])
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch.object(health_checks, "get_settings", return_value=settings),
            patch.object(health_checks.httpx, "AsyncClient", return_value=client),
        ):
            out = await health_checks._probe_libraforge()

        assert out["connected"] is True
        assert client.get.await_count == 2

    asyncio.run(_run())


def test_probe_libraforge_fail_open_when_down():
    async def _run():
        settings = MagicMock()
        settings.libraforge_url = "https://forge.library.freiverse.com"
        settings.libraforge_internal_url = "http://172.17.0.1:5056"

        client = AsyncMock()
        client.get = AsyncMock(side_effect=ConnectionError("refused"))
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch.object(health_checks, "get_settings", return_value=settings),
            patch.object(health_checks.httpx, "AsyncClient", return_value=client),
        ):
            out = await health_checks._probe_libraforge()

        assert out["configured"] is True
        assert out["connected"] is False
        assert "error" in out

    asyncio.run(_run())


def test_probe_libraforge_not_configured_without_url():
    async def _run():
        settings = MagicMock()
        settings.libraforge_url = ""
        settings.libraforge_internal_url = ""

        with patch.object(health_checks, "get_settings", return_value=settings):
            out = await health_checks._probe_libraforge()

        assert out["configured"] is False
        assert out["connected"] is False

    asyncio.run(_run())
