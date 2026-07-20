"""Tests for EPUB HTML prep that lets reader first-line indent win."""

from __future__ import annotations

from app.routers.library import _prepare_reader_html


def test_strips_style_blocks():
    html = (
        "<div><style>p { text-indent: 0; margin: 1em 0; }</style>"
        "<p>Hello dialogue.</p></div>"
    )
    out = _prepare_reader_html(html)
    assert "<style" not in out.lower()
    assert "Hello dialogue." in out


def test_strips_stylesheet_links():
    html = (
        '<link rel="stylesheet" href="styles.css">'
        '<link rel="icon" href="x.png">'
        "<p>Keep me</p>"
    )
    out = _prepare_reader_html(html)
    assert "stylesheet" not in out.lower()
    assert 'rel="icon"' in out
    assert "Keep me" in out


def test_strips_inline_text_indent():
    html = '<p style="color: red; text-indent: 0em; font-size: 12pt">Said she.</p>'
    out = _prepare_reader_html(html)
    assert "text-indent" not in out.lower()
    assert "color: red" in out
    assert "font-size: 12pt" in out
    assert "Said she." in out


def test_empty_passthrough():
    assert _prepare_reader_html("") == ""
