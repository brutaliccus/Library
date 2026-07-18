from app.services.ol_catalog import _fts_query


def test_fts_query_quotes_tokens():
    q = _fts_query("Dungeon Crawler Carl")
    assert '"dungeon"' in q
    assert '"crawler"' in q
    assert '"carl"' in q


def test_fts_query_skips_stopwords():
    q = _fts_query("The Way of Kings")
    assert '"the"' not in q
    assert '"of"' not in q
    assert '"way"' in q
    assert '"kings"' in q
