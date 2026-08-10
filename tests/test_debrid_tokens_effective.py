"""Server-default tokens in app_settings must count for provider availability."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.services import debrid, debrid_tokens


@pytest.fixture(autouse=True)
def _clear_debrid_context():
    debrid_tokens.clear_tokens()
    yield
    debrid_tokens.clear_tokens()


def test_apply_tokens_for_user_none_uses_server_defaults():
    async def _run():
        with patch.object(
            debrid_tokens,
            "_effective_server_tokens",
            new=AsyncMock(return_value=("rd-server", "tb-server")),
        ):
            await debrid_tokens.apply_tokens_for_user_id(None)
            assert debrid_tokens.rd_token() == "rd-server"
            assert debrid_tokens.torbox_token() == "tb-server"
            assert debrid.available_providers() == [debrid.RD, debrid.TORBOX]

    asyncio.run(_run())


def test_apply_tokens_for_user_empty_group_keeps_server_torbox():
    async def _run():
        class _Group:
            real_debrid_api_token = ""
            torbox_api_token = ""

        class _User:
            library_group_id = 1

        class _Result:
            def __init__(self, value):
                self._value = value

            def scalar_one_or_none(self):
                return self._value

        class _Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def execute(self, _stmt):
                if not getattr(self, "_seen_user", False):
                    self._seen_user = True
                    return _Result(_User())
                return _Result(_Group())

        with (
            patch.object(
                debrid_tokens,
                "_effective_server_tokens",
                new=AsyncMock(return_value=("rd-server", "tb-server")),
            ),
            patch("app.database.async_session", return_value=_Session()),
        ):
            await debrid_tokens.apply_tokens_for_user_id(1)
            assert debrid_tokens.rd_token() == "rd-server"
            assert debrid_tokens.torbox_token() == "tb-server"
            assert debrid.available_providers() == [debrid.RD, debrid.TORBOX]

    asyncio.run(_run())


def test_apply_tokens_group_rd_overrides_server_keeps_server_torbox():
    async def _run():
        class _Group:
            real_debrid_api_token = "rd-group"
            torbox_api_token = ""

        class _User:
            library_group_id = 1

        class _Result:
            def __init__(self, value):
                self._value = value

            def scalar_one_or_none(self):
                return self._value

        class _Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def execute(self, _stmt):
                if not getattr(self, "_seen_user", False):
                    self._seen_user = True
                    return _Result(_User())
                return _Result(_Group())

        with (
            patch.object(
                debrid_tokens,
                "_effective_server_tokens",
                new=AsyncMock(return_value=("rd-server", "tb-server")),
            ),
            patch("app.database.async_session", return_value=_Session()),
        ):
            await debrid_tokens.apply_tokens_for_user_id(1)
            assert debrid_tokens.rd_token() == "rd-group"
            assert debrid_tokens.torbox_token() == "tb-server"

    asyncio.run(_run())


def test_apply_server_tokens_reads_effective_settings():
    async def _run():
        with patch.object(
            debrid_tokens,
            "_effective_server_tokens",
            new=AsyncMock(return_value=("rd-eff", "tb-eff")),
        ):
            await debrid_tokens.apply_server_debrid_tokens()
            assert debrid_tokens.rd_token() == "rd-eff"
            assert debrid_tokens.torbox_token() == "tb-eff"
            assert debrid.TORBOX in debrid.available_providers()

    asyncio.run(_run())


def test_pick_uses_preferred_torbox_when_server_tokens_applied():
    async def _run():
        with (
            patch.object(
                debrid_tokens,
                "_effective_server_tokens",
                new=AsyncMock(return_value=("rd-server", "tb-server")),
            ),
            patch.object(
                debrid,
                "check_cached_all",
                new=AsyncMock(return_value={debrid.RD: set(), debrid.TORBOX: set()}),
            ),
        ):
            await debrid_tokens.apply_tokens_for_user_id(None)
            choice = await debrid.pick_provider_for_magnet(
                "magnet:?xt=urn:btih:abcdef0123456789abcdef0123456789abcdef01",
                debrid.TORBOX,
            )
            assert choice == debrid.TORBOX

    asyncio.run(_run())
