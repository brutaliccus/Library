"""Provider selection: unique cache hit wins; ties/misses use preferred."""

from __future__ import annotations

from unittest.mock import patch

from app.services import debrid


HASH = "abcdef0123456789abcdef0123456789abcdef01"


def _both_configured():
    return patch.object(
        debrid,
        "available_providers",
        return_value=[debrid.RD, debrid.TORBOX],
    )


def test_unique_cache_hit_rd_wins_even_if_torbox_preferred():
    """Cached on exactly one service → that service (not preferred)."""
    cached = {debrid.RD: {HASH}, debrid.TORBOX: set()}
    with _both_configured():
        assert debrid.pick_provider(HASH, cached, debrid.TORBOX) == debrid.RD


def test_unique_cache_hit_torbox_wins_even_if_rd_preferred():
    cached = {debrid.RD: set(), debrid.TORBOX: {HASH}}
    with _both_configured():
        assert debrid.pick_provider(HASH, cached, debrid.RD) == debrid.TORBOX


def test_both_cached_uses_preferred_torbox():
    """Cached on both → preferred."""
    cached = {debrid.RD: {HASH}, debrid.TORBOX: {HASH}}
    with _both_configured():
        assert debrid.pick_provider(HASH, cached, debrid.TORBOX) == debrid.TORBOX


def test_both_cached_uses_preferred_rd():
    cached = {debrid.RD: {HASH}, debrid.TORBOX: {HASH}}
    with _both_configured():
        assert debrid.pick_provider(HASH, cached, debrid.RD) == debrid.RD


def test_neither_cached_uses_preferred_torbox():
    """Cached on neither → preferred."""
    cached = {debrid.RD: set(), debrid.TORBOX: set()}
    with _both_configured():
        assert debrid.pick_provider(HASH, cached, debrid.TORBOX) == debrid.TORBOX


def test_neither_cached_uses_preferred_rd():
    cached = {debrid.RD: set(), debrid.TORBOX: set()}
    with _both_configured():
        assert debrid.pick_provider(HASH, cached, debrid.RD) == debrid.RD


def test_unknown_hash_uses_preferred():
    with _both_configured():
        assert debrid.pick_provider(None, None, debrid.TORBOX) == debrid.TORBOX


def test_preferred_missing_falls_back_with_available():
    cached = {debrid.RD: set(), debrid.TORBOX: set()}
    with patch.object(debrid, "available_providers", return_value=[debrid.RD]):
        assert debrid.pick_provider(HASH, cached, debrid.TORBOX) == debrid.RD


def test_download_provider_order_puts_chosen_first_then_preferred_fallback():
    with _both_configured():
        assert debrid.download_provider_order(debrid.TORBOX, debrid.TORBOX) == [
            debrid.TORBOX,
            debrid.RD,
        ]
        # Unique RD cache chose RD; preferred TorBox is next fallback
        assert debrid.download_provider_order(debrid.RD, debrid.TORBOX) == [
            debrid.RD,
            debrid.TORBOX,
        ]


def test_exclude_skips_unique_cache_winner():
    """Retry after TorBox failure must not re-pick TorBox via unique-cache."""
    cached = {debrid.RD: set(), debrid.TORBOX: {HASH}}
    with _both_configured():
        assert (
            debrid.pick_provider(HASH, cached, debrid.RD, exclude=[debrid.TORBOX])
            == debrid.RD
        )
        assert debrid.download_provider_order(
            debrid.TORBOX, debrid.TORBOX, exclude=[debrid.TORBOX]
        ) == [debrid.RD]


def test_torbox_preferred_only_rd_cached_picks_rd():
    cached = {debrid.RD: {HASH}, debrid.TORBOX: set()}
    with _both_configured():
        assert debrid.pick_provider(HASH, cached, debrid.TORBOX) == debrid.RD


def test_rd_preferred_only_torbox_cached_picks_torbox():
    cached = {debrid.RD: set(), debrid.TORBOX: {HASH}}
    with _both_configured():
        assert debrid.pick_provider(HASH, cached, debrid.RD) == debrid.TORBOX


def test_rd_preferred_neither_cached_picks_rd_first_in_order():
    cached = {debrid.RD: set(), debrid.TORBOX: set()}
    with _both_configured():
        assert debrid.pick_provider(HASH, cached, debrid.RD) == debrid.RD
        assert debrid.download_provider_order(debrid.RD, debrid.RD) == [
            debrid.RD,
            debrid.TORBOX,
        ]


def test_torbox_preferred_neither_cached_order_tries_torbox_first():
    cached = {debrid.RD: set(), debrid.TORBOX: set()}
    with _both_configured():
        chosen = debrid.pick_provider(HASH, cached, debrid.TORBOX)
        assert chosen == debrid.TORBOX
        assert debrid.download_provider_order(chosen, debrid.TORBOX) == [
            debrid.TORBOX,
            debrid.RD,
        ]
