# -*- coding: utf-8 -*-
"""HTML -> Markdown rendering (optional, markdownify-backed)."""

from __future__ import annotations

import pytest

from lazycrawler.markdown import html_to_markdown

pytest.importorskip("markdownify")


def test_empty_html_returns_empty():
    assert html_to_markdown("") == ""
    assert html_to_markdown("   ") == ""


def test_headings_and_lists():
    md = html_to_markdown("<h1>Title</h1><ul><li>alpha</li><li>beta</li></ul>")
    assert "# Title" in md
    assert "alpha" in md and "beta" in md
    # markdownify uses '*' or '-' for bullets depending on version
    assert any(marker in md for marker in ("- alpha", "* alpha"))


def test_relative_links_resolved_against_base():
    md = html_to_markdown('<a href="/docs/x">Docs</a>', "https://e.org/page")
    assert "https://e.org/docs/x" in md
    assert "Docs" in md


def test_script_and_style_bodies_not_leaked():
    # Regression: markdownify's strip= keeps tag *text*, which leaked raw JS/CSS
    # into the RAG corpus. We now decompose script/style/noscript outright.
    html = (
        "<p>Hello</p>"
        "<script>var secret=1;function pwn(){}</script>"
        "<style>.a{color:red}</style>"
        "<noscript>enable js</noscript>"
        "<p>World</p>"
    )
    md = html_to_markdown(html)
    assert "Hello" in md and "World" in md
    assert "var secret" not in md
    assert "color:red" not in md
    assert "pwn" not in md
    # base_url present exercises the link-resolution branch too.
    md2 = html_to_markdown(html, "https://e.org/page")
    assert "var secret" not in md2 and "color:red" not in md2


def test_table_rendered():
    html = "<table><tr><th>H</th></tr><tr><td>V</td></tr></table>"
    md = html_to_markdown(html)
    assert "H" in md and "V" in md
