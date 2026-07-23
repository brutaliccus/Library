"""Unit tests for admin user presence / listening activity helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.routers.admin import ONLINE_THRESHOLD, later_datetime, user_is_online


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


def test_later_datetime_prefers_abs_over_stale_rd():
    rd = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
    abs_played = datetime(2026, 7, 23, 14, 5, 0, tzinfo=timezone.utc)
    assert later_datetime(rd, abs_played) == abs_played


def test_later_datetime_handles_none_and_naive():
    only_abs = datetime(2026, 7, 23, 10, 0, 0)  # naive
    assert later_datetime(None, only_abs) == only_abs.replace(tzinfo=timezone.utc)
    assert later_datetime(None, None) is None
