"""Unit tests for LibraForge Audible auth client helpers (no live network)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from app.routers.admin import (
    _diagnose_audible_redirect_url,
    _looks_like_audible_oauth_start_url,
    _looks_like_audible_oauth_url,
)
from app.services.libraforge import (
    LibraForgeError,
    audible_auth_summary,
    public_accounts_url,
)


_SAMPLE_OAUTH = (
    "https://www.amazon.com/ap/signin?openid.oa2.response_type=code"
    "&openid.oa2.code_challenge_method=S256"
    "&openid.oa2.code_challenge=abc123XYZ"
    "&openid.return_to=https%3A%2F%2Fwww.amazon.com%2Fap%2Fmaplanding"
    "&openid.assoc_handle=amzn_audible_ios_us"
    "&openid.identity=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0%2Fidentifier_select"
    "&pageId=amzn_audible_ios"
)

_SAMPLE_REDIRECT = (
    "https://www.amazon.com/ap/maplanding?openid.oa2.authorization_code=Atza%7Cexample"
    "&openid.assoc_handle=amzn_audible_ios_us"
)


def test_looks_like_audible_oauth_url_accepts_full_pkce():
    assert _looks_like_audible_oauth_url(_SAMPLE_OAUTH) is True


def test_looks_like_audible_oauth_url_rejects_truncated():
    # Truncation at first & is what produces Amazon's dog / "not a functioning page" 404.
    assert _looks_like_audible_oauth_url(
        "https://www.amazon.com/ap/signin?openid.oa2.response_type=code"
    ) is False
    assert _looks_like_audible_oauth_url("https://www.amazon.com/ap/maplanding") is False
    assert _looks_like_audible_oauth_url("") is False


def test_diagnose_rejects_oauth_start_url_as_complete_paste():
    assert _looks_like_audible_oauth_start_url(_SAMPLE_OAUTH) is True
    msg = _diagnose_audible_redirect_url(_SAMPLE_OAUTH)
    assert msg is not None
    assert "login page URL" in msg
    assert "authorization_code" in msg


def test_diagnose_accepts_maplanding_with_auth_code():
    assert _diagnose_audible_redirect_url(_SAMPLE_REDIRECT) is None
    assert _looks_like_audible_oauth_start_url(_SAMPLE_REDIRECT) is False


def test_diagnose_maplanding_without_code():
    msg = _diagnose_audible_redirect_url("https://www.amazon.com/ap/maplanding")
    assert msg is not None
    assert "maplanding" in msg.lower()
    assert "authorization_code" in msg


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
