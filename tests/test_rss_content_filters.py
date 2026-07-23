"""Unit tests for RSS / indexer content filters (adult, music, movie, tiny audio)."""

from __future__ import annotations

from app.services.rss_content_filters import (
    SIZE_AUDIO_MUSIC_MAX,
    is_too_small_for_audiobook,
    title_is_non_book,
    title_looks_adult,
    title_looks_like_movie_or_tv,
    title_looks_like_music,
)
from app.services.knaben import _knaben_hit_is_book, _hit_to_result
from app.services.prowlarr import is_book_related


def test_rejects_clear_adult_titles():
    assert title_looks_adult("Brazzers - Hot Scene XXX")
    assert title_looks_adult("OnlyFans Leak Collection")
    assert title_looks_adult("[XXX] Amateur Camgirl Night")
    assert title_looks_adult("Pornhub Premium Pack 2024")
    assert title_is_non_book("Hentai Uncensored Complete")


def test_keeps_young_adult_and_legit_audiobooks():
    assert not title_looks_adult("Young Adult Fantasy Bundle")
    assert not title_is_non_book("Project Hail Mary - Andy Weir [m4b] Unabridged")
    assert not title_is_non_book("The Name of the Wind narrated by Rupert Degas")
    assert not title_looks_like_music("Mistborn Audiobook Unabridged M4B")


def test_rejects_music_and_movies():
    assert title_looks_like_music("Artist Discography 1980-2020 FLAC")
    assert title_looks_like_music("Greatest Hits 320kbps MP3")
    assert title_looks_like_movie_or_tv("Some Film 1080p BluRay x264")
    assert title_looks_like_movie_or_tv("Show.Name.S01E02.720p.WEB-DL")
    assert title_is_non_book("Album Collection FLAC")


def test_tiny_audio_rejected_as_music():
    assert is_too_small_for_audiobook(5 * 1024 * 1024, "audiobook")
    assert is_too_small_for_audiobook(SIZE_AUDIO_MUSIC_MAX - 1, "audiobook")
    assert not is_too_small_for_audiobook(SIZE_AUDIO_MUSIC_MAX, "audiobook")
    assert not is_too_small_for_audiobook(200 * 1024 * 1024, "audiobook")
    # Unknown size allowed through
    assert not is_too_small_for_audiobook(0, "audiobook")
    assert not is_too_small_for_audiobook(None, "audiobook")
    # Ebooks not gated by audio size
    assert not is_too_small_for_audiobook(2 * 1024 * 1024, "ebook")


def test_is_book_related_uses_filters():
    assert not is_book_related([], title="Pornhub Mega Pack XXX", indexer="Knaben")
    assert not is_book_related(
        [], title="Artist Discography FLAC", indexer="Knaben", media_type="audiobook",
        size_bytes=500_000_000,
    )
    assert not is_book_related(
        [], title="Short Track.mp3", indexer="Knaben", media_type="audiobook",
        size_bytes=3_000_000,
    )
    assert is_book_related(
        [{"id": 3030, "name": "Audiobook"}],
        title="Dune Unabridged M4B",
        indexer="Knaben",
        media_type="audiobook",
        size_bytes=400_000_000,
    )
    # ABB is trusted but still rejects adult spam
    assert not is_book_related([], title="OnlyFans Audio Dump", indexer="AudioBookBay")
    assert is_book_related(
        [], title="Legit Title Unabridged", indexer="AudioBookBay", media_type="audiobook",
        size_bytes=300_000_000,
    )


def test_knaben_hit_rejects_adult_music_movie_categories():
    assert _knaben_hit_is_book({
        "title": "Something",
        "categoryId": 1_003_000,
        "category": "Audiobook",
    })
    assert not _knaben_hit_is_book({
        "title": "Something",
        "categoryId": 1_001_000,
        "category": "MP3",
    })
    assert not _knaben_hit_is_book({
        "title": "Something",
        "categoryId": 3_000_000,
        "category": "Movies",
    })
    assert not _knaben_hit_is_book({
        "title": "Something",
        "categoryId": 5_000_000,
        "category": "XXX",
    })
    assert not _knaben_hit_is_book({
        "title": "Brazzers Hot Audio XXX",
        "categoryId": 1_003_000,
        "category": "Audiobook",
    })


def test_knaben_hit_to_result_drops_tiny_audio():
    row = _hit_to_result({
        "title": "Tiny Clip supposedly audiobook",
        "bytes": 4_000_000,
        "seeders": 5,
        "peers": 5,
        "categoryId": 1_003_000,
        "category": "Audiobook",
        "magnetUrl": "magnet:?xt=urn:btih:" + ("a" * 40),
        "hash": "a" * 40,
    })
    assert row is None

    ok = _hit_to_result({
        "title": "Real Audiobook Unabridged M4B",
        "bytes": 350_000_000,
        "seeders": 5,
        "peers": 5,
        "categoryId": 1_003_000,
        "category": "Audiobook",
        "magnetUrl": "magnet:?xt=urn:btih:" + ("b" * 40),
        "hash": "b" * 40,
    })
    assert ok is not None
    assert ok["mediaType"] == "audiobook"
