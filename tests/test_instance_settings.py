"""Tests for instance settings registry helpers."""

from app.services import instance_settings as inst


def test_registry_has_core_groups():
    groups = {g["id"] for g in inst.GROUPS}
    assert "libraries" in groups
    assert "indexers" in groups
    assert "catalog" in groups
    assert "scraper" in groups
    assert "debrid" in groups


def test_registry_includes_api_keys():
    keys = {d.key for d in inst.REGISTRY}
    assert "config.kavita_api_key" in keys
    assert "config.abs_api_key" in keys
    assert "config.prowlarr_api_key" in keys
    assert "integrations.hardcover_api_key" in keys
    assert "config.google_books_api_key" in keys
    assert "config.aa_account_id" in keys
    assert "config.real_debrid_api_token" in keys
    assert "scraper.abb_rss_only" in keys


def test_mask_hides_secret():
    assert inst._mask("abcdefghij") == "******ghij"
    assert inst._mask("") == ""
