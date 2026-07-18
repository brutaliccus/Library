from app.services.google_books import genre_subject_fts_expr


def test_genre_expr_single_word():
    expr = genre_subject_fts_expr("fantasy")
    assert '"fantasy"' in expr
    # multi_queries add broader phrases
    assert "fantasy fiction" in expr.lower()


def test_genre_expr_multiword_subject_is_phrase():
    expr = genre_subject_fts_expr("science-fiction")
    assert '"science fiction"' in expr
    assert " OR " in expr  # combines ol_subject + multi_queries


def test_genre_expr_unknown_slug_falls_back_to_slug_tokens():
    expr = genre_subject_fts_expr("totally-made-up-genre")
    assert expr == '"totally made up genre"'


def test_genre_expr_is_fts_safe():
    # No stray punctuation that would break FTS5 MATCH parsing.
    expr = genre_subject_fts_expr("sci-fi & fantasy!!!")
    assert "&" not in expr
    assert "!" not in expr
