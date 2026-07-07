"""Regression tests for torrent title matching / ranking.

Focus areas:
- Dungeon Crawler Carl book 1 (volume title == series name) must NOT be filtered.
- Later volumes without numbers ("Series - Other Title") must be filtered.
- ABB "Series - NN - Title" naming must resolve to the right volume.
- Box sets / complete collections must not rank as book 1 exact matches.
"""

import pytest

from app.services.download_discovery import (
    is_relevant_torrent,
    rank_indexer_results,
    resolve_book_search_context,
    score_torrent_title,
)
from app.utils.book_series import (
    extract_book_numbers_from_text,
    looks_like_later_series_volume,
)


def dcc_ctx():
    """Dungeon Crawler Carl book 1: catalog title equals series name."""
    return resolve_book_search_context(
        title="Dungeon Crawler Carl",
        author="Matt Dinniman",
        series_name="Dungeon Crawler Carl",
        series_index="1",
    )


def dcc_book2_ctx():
    return resolve_book_search_context(
        title="Carl's Doomsday Scenario",
        author="Matt Dinniman",
        series_name="Dungeon Crawler Carl",
        series_index="2",
    )


# ---------------- extract_book_numbers_from_text ----------------

@pytest.mark.parametrize("title,expected", [
    ("Dungeon Crawler Carl - 01 - Dungeon Crawler Carl", {1.0}),
    ("Dungeon Crawler Carl Book 1", {1.0}),
    ("Dungeon Crawler Carl #4 - The Gate of the Feral Gods", {4.0}),
    ("Matt Dinniman - Dungeon Crawler Carl (2020) [M4B]", set()),
    ("Dungeon Crawler Carl - 03", {3.0}),
    ("The Martian by Andy Weir (2014)", set()),
])
def test_extract_book_numbers(title, expected):
    assert extract_book_numbers_from_text(title) == expected


# ---------------- looks_like_later_series_volume ----------------

BOOK1_RELEASES = [
    # THE original bug: ABB "Series - NN - Title" naming for book 1
    "Dungeon Crawler Carl - 01 - Dungeon Crawler Carl",
    # Series title repeated without a number
    "Dungeon Crawler Carl - Dungeon Crawler Carl",
    # Author-first naming
    "Matt Dinniman - Dungeon Crawler Carl [M4B]",
    # Explicit book 1 marker
    "Dungeon Crawler Carl: Book 1 - Matt Dinniman",
    "Dungeon Crawler Carl (Dungeon Crawler Carl #1)",
    # Bare title
    "Dungeon Crawler Carl (2020) 64k",
]


@pytest.mark.parametrize("release", BOOK1_RELEASES)
def test_book1_releases_not_flagged_as_later_volume(release):
    assert not looks_like_later_series_volume(
        release,
        target_index="1",
        series_name="Dungeon Crawler Carl",
        base_title="Dungeon Crawler Carl",
        volume_title="Dungeon Crawler Carl",
    ), f"Book 1 release wrongly filtered: {release}"


LATER_VOLUME_RELEASES = [
    "Dungeon Crawler Carl - Carl's Doomsday Scenario",
    "Dungeon Crawler Carl - 02 - Carl's Doomsday Scenario",
    "Dungeon Crawler Carl Book 4 - The Gate of the Feral Gods",
    "Dungeon Crawler Carl: The Butcher's Masquerade (2022)",
]


@pytest.mark.parametrize("release", LATER_VOLUME_RELEASES)
def test_later_volumes_flagged(release):
    assert looks_like_later_series_volume(
        release,
        target_index="1",
        series_name="Dungeon Crawler Carl",
        base_title="Dungeon Crawler Carl",
        volume_title="Dungeon Crawler Carl",
    ), f"Later volume NOT filtered: {release}"


# ---------------- is_relevant_torrent ----------------

@pytest.mark.parametrize("release", BOOK1_RELEASES)
def test_book1_releases_relevant(release):
    assert is_relevant_torrent(release, dcc_ctx()), f"Book 1 release dropped: {release}"


@pytest.mark.parametrize("release", [
    "Minecraft Survival Guide for Kids",
    "Python Crash Course 3rd Edition",
    "The Great Gatsby - F Scott Fitzgerald",
])
def test_unrelated_torrents_dropped(release):
    assert not is_relevant_torrent(release, dcc_ctx()), f"Noise kept: {release}"


def test_book2_release_relevant_for_book2():
    assert is_relevant_torrent(
        "Dungeon Crawler Carl - 02 - Carl's Doomsday Scenario", dcc_book2_ctx()
    )


# ---------------- score_torrent_title tiers ----------------

def test_abb_numbered_book1_scores_exact():
    score, tier = score_torrent_title(
        "Dungeon Crawler Carl - 01 - Dungeon Crawler Carl", dcc_ctx()
    )
    assert tier == "exact", f"tier={tier} score={score}"


def test_unnumbered_series_title_scores_high_for_book1():
    score, tier = score_torrent_title(
        "Matt Dinniman - Dungeon Crawler Carl [M4B]", dcc_ctx()
    )
    assert tier in ("exact", "likely"), f"tier={tier} score={score}"


def test_wrong_volume_scores_low_for_book1():
    s1, t1 = score_torrent_title(
        "Dungeon Crawler Carl - 04 - The Gate of the Feral Gods", dcc_ctx()
    )
    s2, _ = score_torrent_title(
        "Dungeon Crawler Carl - 01 - Dungeon Crawler Carl", dcc_ctx()
    )
    assert s2 > s1
    assert t1 == "weak"


def test_book2_numbered_release_scores_exact_for_book2():
    _, tier = score_torrent_title(
        "Dungeon Crawler Carl - 02 - Carl's Doomsday Scenario", dcc_book2_ctx()
    )
    assert tier == "exact"


# ---------------- ranking ----------------

def _mk(title, seeders=10):
    return {"title": title, "seeders": seeders, "indexer": "AudioBook Bay"}


def test_book1_ranks_above_later_volumes_despite_seeders():
    results = [
        _mk("Dungeon Crawler Carl - 04 - The Gate of the Feral Gods", seeders=500),
        _mk("Dungeon Crawler Carl - 02 - Carl's Doomsday Scenario", seeders=300),
        _mk("Dungeon Crawler Carl - 01 - Dungeon Crawler Carl", seeders=5),
        _mk("Matt Dinniman - Dungeon Crawler Carl", seeders=2),
    ]
    ranked = rank_indexer_results(results, dcc_ctx())
    top_titles = [r["title"] for r in ranked[:2]]
    assert "Dungeon Crawler Carl - 01 - Dungeon Crawler Carl" in top_titles
    assert "Matt Dinniman - Dungeon Crawler Carl" in top_titles


def test_fuzzy_handles_punctuation_variants():
    ctx = resolve_book_search_context(
        title="Carl's Doomsday Scenario",
        author="Matt Dinniman",
        series_name="Dungeon Crawler Carl",
        series_index="2",
    )
    assert is_relevant_torrent(
        "Dungeon Crawler Carl 2 - Carls Doomsday Scenario [MP3]", ctx
    )


def test_generic_series_book3():
    ctx = resolve_book_search_context(
        title="The Dragon Reborn",
        author="Robert Jordan",
        series_name="The Wheel of Time",
        series_index="3",
    )
    _, tier = score_torrent_title(
        "The Wheel of Time - 03 - The Dragon Reborn (Robert Jordan)", ctx
    )
    assert tier == "exact"
    assert is_relevant_torrent("Robert Jordan - The Dragon Reborn [M4B]", ctx)
