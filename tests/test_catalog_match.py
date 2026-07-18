from app.services.catalog_match import _torrent_search_queries


def test_torrent_search_queries_strips_extensions():
    qs = _torrent_search_queries("Brandon Sanderson - The Way of Kings [m4b]")
    joined = " | ".join(qs).lower()
    assert "m4b" not in joined
    assert "way of kings" in joined


def test_torrent_search_queries_includes_title_segment():
    qs = _torrent_search_queries("Joe Abercrombie - The Blade Itself Audiobook")
    joined = " | ".join(qs).lower()
    assert "blade itself" in joined
