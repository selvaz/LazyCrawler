# -*- coding: utf-8 -*-
"""text.py: preprocessing, link extraction (+ exclude), canonical, date parsing."""

from __future__ import annotations

from lazycrawler.http import compile_exclude
from lazycrawler.text import (
    extract_candidate_links,
    extract_canonical_url,
    extract_published_datetime,
    preprocess_text,
)


def test_preprocess_strips_noise():
    raw = "Real sentence one.\nWe use cookies to improve your experience\nReal sentence two."
    out = preprocess_text(raw)
    assert "Real sentence one." in out
    assert "Real sentence two." in out
    assert "cookies" not in out.lower()


def test_extract_links_dedup_and_absolute():
    html = (
        '<a href="/a">A</a><a href="/a">dup</a>'
        '<a href="https://e.org/b">B</a><a href="#frag">skip</a>'
    )
    links = extract_candidate_links(html, "https://e.org/", "e.org")
    urls = [u for _, u in links]
    assert "https://e.org/a" in urls
    assert "https://e.org/b" in urls
    assert urls.count("https://e.org/a") == 1


def test_extract_links_honors_custom_exclude():
    html = '<a href="/keep">keep</a><a href="/skipme">skip</a>'
    pat = compile_exclude([r"/skipme"])
    links = extract_candidate_links(html, "https://e.org/", "e.org", exclude_pattern=pat)
    urls = [u for _, u in links]
    assert "https://e.org/keep" in urls
    assert "https://e.org/skipme" not in urls


def test_extract_links_default_allows_about():
    html = '<a href="/about">About</a>'
    links = extract_candidate_links(html, "https://e.org/", "e.org")
    assert any(u.endswith("/about") for _, u in links)


def test_canonical_url():
    html = '<link rel="canonical" href="https://e.org/canon"/>'
    assert extract_canonical_url(html, "https://e.org/page") == "https://e.org/canon"


def test_canonical_url_absent():
    assert extract_canonical_url("<html></html>", "https://e.org/p") is None


def test_published_datetime_from_meta():
    html = '<meta property="article:published_time" content="2026-01-15T14:30:00Z">'
    iso = extract_published_datetime(html, "https://e.org/x")
    assert iso and iso.startswith("2026-01-15")


def test_published_datetime_none_when_missing():
    assert extract_published_datetime("<html></html>", "https://e.org/x") is None
