"""Unit tests for LibraForge Audible auth client helpers (no live network)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from app.services.libraforge import (
    LibraForgeError,
    audible_auth_summary,
    public_accounts_url,
)


def test_public_accounts_url(monkeypatch):
    from app.services import libraforge as lf

    monkeypatch.setattr(lf.settings, "libraforge_url", "http://127.0.0.1:5056")
    assert public_accounts_url() == "http://127.0.0.1:5056/settings#accounts"


def test_audible_auth_summary_configured():
    with (
        patch(
            "app.services.libraforge.auth_status",
            new=AsyncMock(
                return_value={
                    "auth_ok": True,
                    "auth_file": "/auth/audible-metadata.json",
                    "active_name": "Metadata",
                    "activation_bytes_set": False,
                }
            ),
        ),
        patch(
            "app.services.libraforge.auth_accounts",
            new=AsyncMock(
                return_value={
                    "accounts": [
                        {
                            "user_id": "u1",
                            "flavor_name": "Metadata",
                            "active": True,
                        }
                    ]
                }
            ),
        ),
        patch(
            "app.services.libraforge.auth_locales",
            new=AsyncMock(return_value={"locales": {"us": "United States"}}),
        ),
        patch(
            "app.services.libraforge.public_accounts_url",
            return_value="http://lf/settings#accounts",
        ),
    ):
        summary = asyncio.run(audible_auth_summary())
    assert summary["configured"] is True
    assert summary["reachable"] is True
    assert summary["status"] == "configured"
    assert summary["active_name"] == "Metadata"
    assert summary["locales"]["us"] == "United States"
    assert summary["libraforge_accounts_url"] == "http://lf/settings#accounts"


def test_audible_auth_summary_unreachable():
    with patch(
        "app.services.libraforge.auth_status",
        new=AsyncMock(side_effect=LibraForgeError("LibraForge unreachable (/api/auth/status): boom")),
    ):
        summary = asyncio.run(audible_auth_summary())
    assert summary["configured"] is False
    assert summary["reachable"] is False
    assert summary["status"] == "unreachable"
    assert "unreachable" in summary["error"].lower()
