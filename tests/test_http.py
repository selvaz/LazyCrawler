# -*- coding: utf-8 -*-
"""HTTP utilities, text extraction, exclusion, FetchResult, retry, robots."""

from __future__ import annotations

import requests

from lazycrawler.config import HTTPConfig
from lazycrawler.http import (
    DEFAULT_EXCLUDE_PATTERNS,
    FetchResult,
    HTTPClient,
    RobotsChecker,
    compile_exclude,
    content_hash,
    is_excluded_url,
    url_hash,
)

# -- URL normalization / hashing ---------------------------------------------


def test_url_hash_strips_tracking_params():
    assert url_hash("https://x.example/a?utm_source=z") == url_hash("https://x.example/a")


def test_url_hash_lowercases_host_and_trims_slash():
    assert url_hash("https://X.EXAMPLE/a/") == url_hash("https://x.example/a")


def test_normalize_keeps_non_tracking_query():
    # x=1 is not a tracking param -> stays, so it's a different URL
    assert url_hash("https://x.example/a?x=1") != url_hash("https://x.example/a")


def test_content_hash_normalizes_whitespace():
    assert content_hash("a  b\n\n\nc") == content_hash("a b\n\nc")


# -- exclusion (configurable, more permissive default) -----------------------


def test_default_exclude_blocks_auth_and_commerce():
    pat = compile_exclude()
    for u in (
        "https://e.org/login",
        "https://e.org/cart",
        "https://e.org/checkout",
        "https://e.org/account",
        "https://facebook.com/x",
    ):
        assert is_excluded_url(u, pattern=pat), u


def test_default_exclude_allows_content_paths():
    # /about, /contact, /tag/, /category/, /author/ are no longer excluded
    pat = compile_exclude()
    for u in (
        "https://e.org/about",
        "https://e.org/contact",
        "https://e.org/tag/x",
        "https://e.org/category/y",
        "https://e.org/author/jane",
    ):
        assert not is_excluded_url(u, pattern=pat), u


def test_custom_exclude_overrides_default():
    pat = compile_exclude([r"/secret"])
    assert is_excluded_url("https://e.org/secret/page", pattern=pat)
    assert not is_excluded_url("https://e.org/login", pattern=pat)  # default frag not present


def test_empty_exclude_matches_nothing():
    pat = compile_exclude([])
    assert not is_excluded_url("https://e.org/login", pattern=pat)


def test_dedicated_user_agent_default():
    assert "LazyCrawler" in HTTPConfig().user_agent
    assert "Chrome" not in HTTPConfig().user_agent


# -- text extraction threshold (configurable, was hardcoded 200) -------------


def test_short_page_accepted_with_low_threshold():
    client = HTTPClient(HTTPConfig(min_text_chars=10))
    html = "<html><body><p>Short but valid documentation note.</p></body></html>"
    text = client._extract_text(html)
    assert text and "Short but valid" in text


def test_short_page_rejected_with_high_threshold():
    client = HTTPClient(HTTPConfig(min_text_chars=10_000))
    html = "<html><body><p>Tiny.</p></body></html>"
    assert client._extract_text(html) is None


def test_threshold_is_inclusive():
    # exactly min_text_chars characters should be accepted (>=, not >)
    client = HTTPClient(HTTPConfig(min_text_chars=20))
    body = "x" * 20
    html = f"<html><body><p>{body}</p></body></html>"
    assert client._extract_text(html) == body


# -- FetchResult --------------------------------------------------------------


def test_fetchresult_unpacks_as_triple():
    fr = FetchResult(html="<p>h</p>", text="h", status=200)
    html, text, status = fr
    assert (html, text, status) == ("<p>h</p>", "h", 200)


def test_fetch_retries_then_succeeds(monkeypatch):
    client = HTTPClient(HTTPConfig(max_retries=3, backoff_base_sec=0, verify_ssl=False))
    calls = {"n": 0}

    class _Resp:
        status_code = 200
        headers = {"Content-Type": "text/html"}
        content = b"<html><body><p>" + b"ok " * 50 + b"</p></body></html>"
        text = "<html><body><p>" + "ok " * 50 + "</p></body></html>"

        def raise_for_status(self):
            pass

    def flaky_get(url, **kw):
        calls["n"] += 1
        if calls["n"] < 2:
            raise requests.ConnectionError("boom")
        return _Resp()

    monkeypatch.setattr(client._session, "get", flaky_get)
    fr = client.fetch("https://e.org/x")
    assert calls["n"] == 2
    assert fr.status == 200 and fr.text


def test_fetch_gives_up_after_max_retries(monkeypatch):
    client = HTTPClient(HTTPConfig(max_retries=2, backoff_base_sec=0, verify_ssl=False))

    def always_fail(url, **kw):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(client._session, "get", always_fail)
    fr = client.fetch("https://e.org/x")
    assert fr.html is None and fr.text is None and fr.status is None


def test_fetch_returns_pdf_bytes_by_content_type(monkeypatch):
    client = HTTPClient(HTTPConfig(verify_ssl=False))

    class _Resp:
        status_code = 200
        headers = {"Content-Type": "application/pdf"}
        content = b"%PDF-1.7 minimal"
        text = "garbage"

        def raise_for_status(self):
            pass

    monkeypatch.setattr(client._session, "get", lambda url, **kw: _Resp())
    fr = client.fetch("https://e.org/doc")
    assert fr.content == b"%PDF-1.7 minimal"
    assert fr.html is None  # text extraction skipped for PDFs


# -- robots --------------------------------------------------------------------


class _FakeHTTP:
    """Minimal HTTPClient stand-in for RobotsChecker that counts robots fetches."""

    def __init__(self, body):
        self.body = body
        self.calls = 0

    def get_text(self, url):
        self.calls += 1
        return self.body


def test_robots_disallow_and_allow():
    body = "User-agent: *\nDisallow: /private\n"
    rc = RobotsChecker(_FakeHTTP(body), "LazyCrawler")
    assert rc.allowed("https://e.org/public")
    assert not rc.allowed("https://e.org/private/x")


def test_robots_fetched_once_per_host():
    fake = _FakeHTTP("User-agent: *\nDisallow:\n")
    rc = RobotsChecker(fake, "LazyCrawler")
    for _ in range(5):
        rc.allowed("https://e.org/page")
    assert fake.calls == 1  # cached after the first fetch (no TOCTOU double-fetch)


def test_robots_crawl_delay_parsed():
    fake = _FakeHTTP("User-agent: *\nCrawl-delay: 3\n")
    rc = RobotsChecker(fake, "LazyCrawler")
    assert rc.crawl_delay("https://e.org/x") == 3.0


def test_default_exclude_patterns_dropped_content_paths():
    joined = " ".join(DEFAULT_EXCLUDE_PATTERNS)
    for frag in ("/about", "/contact", "/tag/", "/category/", "/author/"):
        assert frag not in joined
