"""Ebook size cap for indexer cache."""

from app.services.indexer_cache import ebook_size_acceptable, MAX_EBOOK_SIZE_BYTES


def test_ebook_size_cap_rejects_huge():
    assert not ebook_size_acceptable("ebook", MAX_EBOOK_SIZE_BYTES + 1)
    assert ebook_size_acceptable("ebook", MAX_EBOOK_SIZE_BYTES)
    assert ebook_size_acceptable("ebook", 15 * 1024 * 1024)
    assert ebook_size_acceptable("audiobook", MAX_EBOOK_SIZE_BYTES + 1)


def test_ebook_unknown_size_allowed():
    assert ebook_size_acceptable("ebook", None)
    assert ebook_size_acceptable("ebook", 0)
