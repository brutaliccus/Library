"""Unit tests for web-push helpers (expired subscription detection)."""
from __future__ import annotations

from types import SimpleNamespace

from app.services.push import _is_gone_subscription, _response_status


class _FalsyGoneResponse:
    """Mimic requests.Response: falsy when status >= 400, but status_code set."""

    def __init__(self, status_code: int):
        self.status_code = status_code

    def __bool__(self) -> bool:
        return self.status_code < 400


def test_response_status_reads_410_even_when_response_is_falsy():
    exc = SimpleNamespace(response=_FalsyGoneResponse(410))
    assert _response_status(exc) == 410
    assert _is_gone_subscription(exc) is True


def test_response_status_none_when_missing():
    assert _response_status(SimpleNamespace()) is None
    assert _response_status(SimpleNamespace(response=None)) is None


def test_is_gone_from_message_fallback():
    assert _is_gone_subscription(RuntimeError("Push failed: 410 Gone")) is True
    assert _is_gone_subscription(RuntimeError("network timeout")) is False
