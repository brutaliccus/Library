"""Byte-range slicing for audio proxies (Android OOM guard)."""

from app.utils.http_range import parse_range_header, resolve_range, response_for_slice


def test_parse_range_start_end():
    r = parse_range_header("bytes=0-2097151")
    assert r is not None
    assert r.start == 0
    assert r.end == 2_097_151


def test_parse_range_open_end():
    r = parse_range_header("bytes=1024-")
    assert r is not None
    assert r.start == 1024
    assert r.end is None


def test_parse_range_suffix():
    r = parse_range_header("bytes=-500")
    assert r is not None
    assert r.start is None
    assert r.end == 500


def test_parse_range_missing():
    assert parse_range_header(None) is None
    assert parse_range_header("") is None
    assert parse_range_header("not-a-range") is None


def test_resolve_open_end_known_total():
    r = parse_range_header("bytes=100-")
    skip, last, total = resolve_range(r, 1000)
    assert skip == 100
    assert last is None
    assert total == 1000
    status, headers = response_for_slice(start=skip, last=last, total=total, content_type=None)
    assert status == 206
    assert headers["content-range"] == "bytes 100-999/1000"


def test_response_slice_is_206_not_full_file_length():
    status, headers = response_for_slice(
        start=0,
        last=2_097_151,
        total=8_000_000_000,
        content_type="audio/mp4",
    )
    assert status == 206
    assert headers["content-length"] == "2097152"
    assert headers["content-range"] == "bytes 0-2097151/8000000000"
    assert headers["accept-ranges"] == "bytes"
    assert headers["x-accel-buffering"] == "no"


def test_response_full_file_keeps_200():
    status, headers = response_for_slice(
        start=0,
        last=None,
        total=12345,
        content_type="audio/mpeg",
    )
    assert status == 200
    assert headers["content-length"] == "12345"
    assert "content-range" not in headers
