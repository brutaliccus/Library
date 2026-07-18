"""Tests for RD disabled instantAvailability fallback."""

import asyncio

from app.services import real_debrid


def test_parse_instant_availability_empty_rd():
    data = {"abc123": {"rd": []}}
    assert real_debrid.parse_instant_availability_response(data) == set()


def test_mark_instant_availability_disabled():
    real_debrid._instant_availability_disabled = None
    real_debrid._mark_instant_availability_disabled()
    assert real_debrid.instant_availability_disabled() is True
    real_debrid._instant_availability_disabled = None


async def _fake_account(hashes):
    return {h.lower() for h in hashes if h}


async def _fake_probe(magnet, **kw):
    return True


def test_probe_magnets_cached(monkeypatch):
    real_debrid._instant_availability_disabled = None
    monkeypatch.setattr(real_debrid.debrid_tokens, "rd_token", lambda: "test-token")
    monkeypatch.setattr(real_debrid, "probe_magnet_cached", _fake_probe)

    items = [("a" * 40, "magnet:?xt=urn:btih:" + "a" * 40)]
    hits = asyncio.run(real_debrid.probe_magnets_cached(items, max_items=1, delay=0))
    assert "a" * 40 in hits


def test_check_instant_availability_uses_account_when_disabled(monkeypatch):
    real_debrid._instant_availability_disabled = True
    monkeypatch.setattr(real_debrid, "check_account_availability", _fake_account)
    monkeypatch.setattr(real_debrid.debrid_tokens, "rd_token", lambda: "test-token")

    hits = asyncio.run(real_debrid.check_instant_availability(["a" * 40]))
    assert "a" * 40 in hits
    real_debrid._instant_availability_disabled = None
