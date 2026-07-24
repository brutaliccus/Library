"""Unit tests for publish-year sanitization used by New Releases shelves."""
from __future__ import annotations

from datetime import datetime, timezone

from app.services.ol_catalog import sane_publish_year


def test_sane_publish_year_rejects_ol_dump_garbage():
    assert sane_publish_year(9999) == 0
    assert sane_publish_year(9881) == 0
    assert sane_publish_year(0) == 0
    assert sane_publish_year(None) == 0
    assert sane_publish_year("not-a-year") == 0


def test_sane_publish_year_accepts_plausible_years():
    now = datetime.now(timezone.utc).year
    assert sane_publish_year(2010) == 2010
    assert sane_publish_year(str(now)) == now
    assert sane_publish_year(now + 1) == now + 1
    assert sane_publish_year(now + 2) == 0
    assert sane_publish_year(999) == 0
