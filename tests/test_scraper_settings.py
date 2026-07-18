"""Unit tests for the admin-tunable scraper settings (pure logic, no DB)."""

from dataclasses import fields

from app.services import scraper_settings as ss
from app.services.indexer_scraper import _extra_queries


def test_env_defaults_cover_all_config_fields():
    defaults = ss.env_defaults()
    cfg = ss.ScraperConfig(**defaults)
    assert cfg.interval_seconds > 0
    assert cfg.queries_per_job > 0


def test_field_definitions_match_config_dataclass():
    def_keys = {f.key for f in ss.FIELDS}
    cfg_keys = {f.name for f in fields(ss.ScraperConfig)}
    assert def_keys == cfg_keys


def test_coerce_clamps_int_bounds():
    field = ss._FIELD_BY_KEY["interval_seconds"]
    assert ss._coerce(field, 1) == field.min
    assert ss._coerce(field, 999999) == field.max
    assert ss._coerce(field, "60") == 60


def test_coerce_bool():
    field = ss._FIELD_BY_KEY["foreign_title_prune"]
    assert ss._coerce(field, True) is True
    assert ss._coerce(field, "false") is False
    assert ss._coerce(field, "1") is True
    assert ss._coerce(field, 0) is False


def test_mode_toggle_defaults_are_on():
    defaults = ss.env_defaults()
    assert defaults["abb_rss_only"] is True
    assert defaults["knaben_rss_only"] is True
    assert defaults["foreign_title_prune"] is True



def test_extra_queries_parsing():
    cfg = ss.ScraperConfig(**{**ss.env_defaults(), "extra_queries": "one query\n two , x \n\none query"})
    parsed = _extra_queries(cfg)
    assert parsed == ["one query", "two"]  # "x" too short, dupes removed
