"""Unit tests for Real-Debrid info-hash extraction and instantAvailability parsing."""

from app.services.real_debrid import (
    extract_info_hash,
    parse_instant_availability_response,
)


def test_extract_hex_from_info_hash_field():
    h = "A1B2C3D4E5F6789012345678ABCDEF1234567890"
    assert extract_info_hash(None, h) == h.lower()


def test_extract_hex_from_magnet():
    magnet = "magnet:?xt=urn:btih:a1b2c3d4e5f6789012345678abcdef1234567890&dn=test"
    assert extract_info_hash(magnet, None) == "a1b2c3d4e5f6789012345678abcdef1234567890"


def test_extract_base32_from_magnet():
    b32 = "A" * 32
    magnet = f"magnet:?xt=urn:btih:{b32}&dn=test"
    assert extract_info_hash(magnet, None) == "0" * 40


def test_parse_instant_availability_rd_array():
    data = {
        "abc123": {
            "rd": [
                {"1": {"filename": "book.m4b", "filesize": 100}},
            ],
        },
    }
    assert parse_instant_availability_response(data) == {"abc123"}


def test_parse_instant_availability_empty_rd():
    data = {"abc123": {"rd": []}}
    assert parse_instant_availability_response(data) == set()
