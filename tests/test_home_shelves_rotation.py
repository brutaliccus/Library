"""Deterministic daily shuffle of Hardcover home curated shelves."""

from app.services import hardcover


def test_rotated_home_shelves_same_seed_same_order():
    a = hardcover.rotated_home_curated_shelves(day="2026-07-24", salt="library.example.com")
    b = hardcover.rotated_home_curated_shelves(day="2026-07-24", salt="library.example.com")
    assert [e["slug"] for e in a] == [e["slug"] for e in b]
    assert {e["slug"] for e in a} == {e["slug"] for e in hardcover.HOME_CURATED_SHELVES}


def test_rotated_home_shelves_new_day_changes_order():
    day1 = hardcover.rotated_home_curated_shelves(day="2026-07-24", salt="")
    day2 = hardcover.rotated_home_curated_shelves(day="2026-07-25", salt="")
    assert [e["slug"] for e in day1] != [e["slug"] for e in day2]
    assert {e["slug"] for e in day1} == {e["slug"] for e in day2}


def test_rotated_home_shelves_salt_diverges_instances():
    a = hardcover.rotated_home_curated_shelves(day="2026-07-24", salt="alpha.example")
    b = hardcover.rotated_home_curated_shelves(day="2026-07-24", salt="beta.example")
    assert [e["slug"] for e in a] != [e["slug"] for e in b]
