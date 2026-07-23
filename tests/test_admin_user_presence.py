"""Unit tests for admin user presence / online threshold."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.routers.admin import ONLINE_THRESHOLD, user_is_online


def test_user_is_online_within_threshold():
    now = datetime(2026, 7, 23, 14, 0, 0, tzinfo=timezone.utc)
    seen = now - ONLINE_THRESHOLD + timedelta(seconds=1)
    assert user_is_online(seen, now=now) is True


def test_user_is_online_at_exact_threshold():
    now = datetime(2026, 7, 23, 14, 0, 0, tzinfo=timezone.utc)
    seen = now - ONLINE_THRESHOLD
    assert user_is_online(seen, now=now) is True


def test_user_is_offline_past_threshold():
    now = datetime(2026, 7, 23, 14, 0, 0, tzinfo=timezone.utc)
    seen = now - ONLINE_THRESHOLD - timedelta(seconds=1)
    assert user_is_online(seen, now=now) is False


def test_user_is_offline_when_never_seen():
    now = datetime(2026, 7, 23, 14, 0, 0, tzinfo=timezone.utc)
    assert user_is_online(None, now=now) is False


def test_user_is_online_naive_timestamp_treated_as_utc():
    now = datetime(2026, 7, 23, 14, 0, 0, tzinfo=timezone.utc)
    seen = datetime(2026, 7, 23, 13, 59, 0)  # naive
    assert user_is_online(seen, now=now) is True
