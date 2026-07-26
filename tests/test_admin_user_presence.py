"""Unit tests for admin user presence / listening activity helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.routers.admin import (
    FINISHED_NEAR_END_SECONDS,
    ONLINE_THRESHOLD,
    later_datetime,
    stream_counts_as_finished,
    user_is_online,
)


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


def test_stream_finished_by_status():
    assert stream_counts_as_finished("finished", 0, 0) is True
    assert stream_counts_as_finished("playing", 100, 10_000) is False


def test_stream_finished_near_end_of_book():
    total = 3600.0
    # Exactly 5 minutes remaining → finished; more than 5 minutes → not.
    assert stream_counts_as_finished("playing", total - FINISHED_NEAR_END_SECONDS, total) is True
    assert stream_counts_as_finished("paused", total - FINISHED_NEAR_END_SECONDS - 1, total) is False
    assert stream_counts_as_finished("playing", total - 1, total) is True


def test_stream_not_finished_without_known_total():
    assert stream_counts_as_finished("playing", 9999, 0) is False
    assert stream_counts_as_finished("playing", 9999, None) is False
