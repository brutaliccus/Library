"""ABB / indexer release title ↔ author parsing."""

from __future__ import annotations

import pytest

from app.services.downloader import parse_torrent_name
from app.utils.release_title import (
    looks_like_person_name,
    parse_torrent_name_parts,
    split_release_title_author,
)


@pytest.mark.parametrize(
    "raw,indexer,title,author",
    [
        # Classic ABB: Title - Author (short title must NOT swap)
        ("Dune - Frank Herbert", "AudioBookBay", "Dune", "Frank Herbert"),
        ("It - Stephen King", "AudioBookBay", "It", "Stephen King"),
        (
            "The Gunslinger - Stephen King",
            "AudioBookBay",
            "The Gunslinger",
            "Stephen King",
        ),
        (
            "Project Hail Mary - Andy Weir [M4B]",
            "AudioBookBay",
            "Project Hail Mary",
            "Andy Weir",
        ),
        # Series / multi-segment ABB
        (
            "Dungeon Crawler Carl #1 - Matt Dinniman",
            "AudioBookBay",
            "Dungeon Crawler Carl #1",
            "Matt Dinniman",
        ),
        (
            "The Name of the Wind - Patrick Rothfuss - Nick Podehl",
            "AudioBookBay",
            "The Name of the Wind",
            "Patrick Rothfuss",
        ),
        # Multi-author
        (
            "Good Omens - Neil Gaiman & Terry Pratchett",
            "AudioBookBay",
            "Good Omens",
            "Neil Gaiman & Terry Pratchett",
        ),
        (
            "The Talisman - Stephen King, Peter Straub",
            "abb",
            "The Talisman",
            "Stephen King, Peter Straub",
        ),
        # Title by Author
        (
            "Hyperion by Dan Simmons",
            "AudioBookBay",
            "Hyperion",
            "Dan Simmons",
        ),
        (
            "Leviathan Wakes by James S.A. Corey (Unabridged)",
            None,
            "Leviathan Wakes",
            "James S.A. Corey",
        ),
        # Noise segments stripped
        (
            "Children of Time - Adrian Tchaikovsky - Unabridged - 2015",
            "AudioBookBay",
            "Children of Time",
            "Adrian Tchaikovsky",
        ),
        (
            "Wool - Hugh Howey - Narrated by Dion Graham",
            "AudioBookBay",
            "Wool",
            "Hugh Howey",
        ),
        # Knaben-style Author - Title
        (
            "Brandon Sanderson - Mistborn The Final Empire",
            "Knaben",
            "Mistborn The Final Empire",
            "Brandon Sanderson",
        ),
        (
            "King, Stephen - The Stand",
            "Knaben",
            "The Stand",
            "King, Stephen",
        ),
    ],
)
def test_split_release_title_author(raw, indexer, title, author):
    got_title, got_author = split_release_title_author(raw, indexer=indexer)
    assert got_title == title
    assert got_author == author


def test_short_title_no_longer_swaps_with_length_heuristic():
    # Regression: len("Dune") < len("Frank Herbert") used to swap.
    title, author = split_release_title_author(
        "Dune - Frank Herbert", indexer="AudioBookBay"
    )
    assert title == "Dune"
    assert author == "Frank Herbert"
    assert not (title == "Frank Herbert" and author == "Dune")


def test_parse_torrent_name_returns_author_first():
    author, book = parse_torrent_name(
        "Dune - Frank Herbert [M4B]", indexer="AudioBookBay"
    )
    assert author == "Frank Herbert"
    assert book == "Dune"


def test_parse_torrent_name_parts_unknown():
    author, book = parse_torrent_name_parts("SomeRandomReleaseName")
    assert author == "Unknown Author"
    assert book == "SomeRandomReleaseName"


def test_looks_like_person_name():
    assert looks_like_person_name("Frank Herbert")
    assert looks_like_person_name("King, Stephen")
    assert looks_like_person_name("Neil Gaiman & Terry Pratchett")
    assert not looks_like_person_name("The Martian")
    assert not looks_like_person_name("Dungeon Crawler Carl #1")
    assert not looks_like_person_name("Unabridged")
