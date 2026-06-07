# -*- coding: utf-8 -*-
"""Coverage for the critical+medium audit remediations:

#1 async redirect SSRF re-check, #2 async streamed body cap, #3 sync redirect
adoption, #4 registrable-domain scope, #5 per-call config in traversal,
#7 async provenance, #8 agent search cap, #10 untrusted-content labeling.
"""

from __future__ import annotations

import asyncio

from lazycrawler import HTTPConfig
from lazycrawler.http import FetchResult, get_hostname, registrable_domain, same_site

# =============================================================================
# #4 — registrable-domain semantics
# =============================================================================


def test_registrable_domain_basic():
    assert registrable_domain("https://news.example.com/x") == "example.com"
    assert registrable_domain("blog.example.com") == "example.com"
    assert registrable_domain("example.com") == "example.com"


def test_registrable_domain_multipart_tld():
    # tldextract (installed via the 'domains' extra) handles these accurately.
    assert registrable_domain("https://x.bbc.co.uk/news") == "bbc.co.uk"


def test_registrable_domain_strips_port_and_userinfo():
    assert get_hostname("https://user:pw@example.com:8443/p") == "example.com"
    assert registrable_domain("https://user@news.example.com:8443/p") == "example.com"


def test_same_site_parent_sibling_and_cross():
    # The documented contract: parent + sibling subdomains are "same site".
    assert same_site("news.example.com", "blog.example.com") is True
    assert same_site("news.example.com", "example.com") is True
    assert same_site("example.com", "example.org") is False


def test_extract_links_follows_sibling_subdomain():
    from lazycrawler.text import extract_candidate_links

    html = (
        '<a href="https://blog.example.com/post">sibling</a><a href="https://other.org/x">cross</a>'
    )
    # start scope is the seed hostname
    links = extract_candidate_links(html, "https://news.example.com/", "news.example.com")
    urls = [u for _, u in links]
    assert "https://blog.example.com/post" in urls
    assert "https://other.org/x" not in urls


# =============================================================================
# #3 — sync pipeline adopts the post-redirect final URL
# =============================================================================


def test_redirect_final_url_is_adopted(monkeypatch, make_crawler):
    def fetch(self, url, extra_headers=None):
        if "a.example" in url:
            body = "Original landing page body, long enough to be real text. " * 4
            html = f'<html><body><p>{body}</p><a href="/article">go</a></body></html>'
            return FetchResult(
                html=html, text=body, status=200, final_url="https://b.example/landing"
            )
        # the adopted-origin child
        body = "Distinct article body on the real origin, long enough to count. " * 4
        return FetchResult(html=f"<html><body><p>{body}</p></body></html>", text=body, status=200)

    monkeypatch.setattr("lazycrawler.http.HTTPClient.fetch", fetch)
    c = make_crawler(max_depth=1, respect_robots=False)
    # same_domain_only off so the cross-origin relative link is followed, proving
    # it resolved against the ADOPTED base (b.example), not the seed (a.example).
    r = c.crawl("https://a.example/page", mode="pure", overrides={"same_domain_only": False})

    seed_page = r[0]
    # identity adopted to the real origin; original preserved as requested_url
    assert seed_page.url == "https://b.example/landing"
    assert seed_page.requested_url == "https://a.example/page"
    assert any("https://b.example/article" == p.url for p in r)
    assert not any("a.example/article" in p.url for p in r)


# =============================================================================
# #5 — per-call overrides apply inside traversal (mode selection)
# =============================================================================


def test_override_max_workers_triggers_parallel(stub_fetch, make_crawler, monkeypatch):
    stub_fetch()
    c = make_crawler(max_workers=1)  # constructed sequential
    seen = {}
    orig = c._crawl_parallel

    def spy(st, seeds):
        seen["parallel"] = True
        return orig(st, seeds)

    monkeypatch.setattr(c, "_crawl_parallel", spy)
    c.crawl("https://e.org/p", mode="pure", overrides={"max_workers": 3})
    assert seen.get("parallel") is True


# =============================================================================
# #8 — agent web_search result count is capped
# =============================================================================


def test_web_search_result_count_capped(tmp_db, monkeypatch):
    from lazycrawler import CrawlerConfig
    from lazycrawler.tools import _MAX_AGENT_SEARCH_RESULTS, CrawlerTools

    tools = CrawlerTools(
        db=tmp_db,
        crawler_cfg=CrawlerConfig(max_depth=1, respect_robots=False),
        http_cfg=HTTPConfig(verify_ssl=False, link_delay=0),
        content="pure",
    )
    captured = {}

    def fake_run(query, **kwargs):
        captured["max_results"] = kwargs.get("max_results")
        return {"results": [], "pages_found": 0}

    monkeypatch.setattr(tools._search, "run", fake_run)
    try:
        tools.web_search("anything", max_results=10_000)
    finally:
        tools.close()
    assert captured["max_results"] == _MAX_AGENT_SEARCH_RESULTS


# =============================================================================
# #10 — untrusted-content labeling on tool output
# =============================================================================


def test_brief_marks_content_untrusted():
    from lazycrawler.tools import _brief

    out = _brief({"url": "https://x/y", "clean_text": "some page text"})
    assert out["content_is_untrusted"] is True


# =============================================================================
# #1 / #2 / #7 — async crawler hardening
# =============================================================================


class _FakeContent:
    def __init__(self, body: bytes):
        self._body = body

    async def iter_chunked(self, n: int):
        for i in range(0, len(self._body), n):
            yield self._body[i : i + n]


class _FakeResp:
    def __init__(self, status, headers, body=b""):
        self.status = status
        self.headers = headers
        self.content = _FakeContent(body)
        self.request_info = None
        self.history = ()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, responses):
        self._responses = responses  # url -> _FakeResp factory

    def get(self, url, allow_redirects=False):
        spec = self._responses[url]
        return spec()


def _make_async_client(monkeypatch, responses):
    from lazycrawler.async_crawler import _AsyncHTTPClient

    client = _AsyncHTTPClient(HTTPConfig(block_private_addresses=True, max_html_bytes=1000))

    async def _ensure():
        return _FakeSession(responses)

    monkeypatch.setattr(client, "_ensure", _ensure)
    return client


def test_async_redirect_to_private_is_blocked(monkeypatch):
    import lazycrawler.async_crawler as ac

    async def fake_guard(url: str) -> bool:
        return "127.0.0.1" in url  # only the private hop is blocked

    monkeypatch.setattr(ac, "_is_blocked_async", fake_guard)

    def public():
        return _FakeResp(302, {"Location": "http://127.0.0.1/admin"})

    def private():
        raise AssertionError("private hop must never be requested")

    client = _make_async_client(
        monkeypatch,
        {"https://public.example/": public, "http://127.0.0.1/admin": private},
    )
    fr = asyncio.run(client._fetch_once("https://public.example/"))
    assert fr.html is None  # blocked before any body was read
    assert "127.0.0.1" in (fr.final_url or "")


def test_async_body_capped_while_streaming(monkeypatch):
    import lazycrawler.async_crawler as ac

    async def no_block(url: str) -> bool:
        return False

    monkeypatch.setattr(ac, "_is_blocked_async", no_block)

    big = b"<html><body>" + (b"x" * 100_000) + b"</body></html>"

    def ok():
        return _FakeResp(200, {"Content-Type": "text/html"}, body=big)

    client = _make_async_client(monkeypatch, {"https://big.example/": ok})
    fr = asyncio.run(client._fetch_once("https://big.example/"))
    # html decoded from at most max_html_bytes (1000), not the full 100KB
    assert fr.html is not None
    assert len(fr.html) <= 1000


def test_async_preserves_provenance(monkeypatch):
    import lazycrawler.async_crawler as ac
    from lazycrawler import CrawlerConfig
    from lazycrawler.async_crawler import AsyncWebCrawler, _AsyncFetchResult

    async def no_block(url: str) -> bool:
        return False

    monkeypatch.setattr(ac, "_is_blocked_async", no_block)

    async def fake_fetch(self, url):
        if url.rstrip("/") == "https://parent.example":
            body = "Parent page body long enough to be real content here. " * 4
            html = (
                f"<html><body><p>{body}</p>"
                '<a href="https://parent.example/child">c</a></body></html>'
            )
            return _AsyncFetchResult(html=html, text=body, status=200, final_url=url)
        body = "Child page body long enough to be treated as real text now. " * 4
        return _AsyncFetchResult(
            html=f"<html><body><p>{body}</p></body></html>", text=body, status=200, final_url=url
        )

    monkeypatch.setattr(ac._AsyncHTTPClient, "fetch", fake_fetch)

    async def run():
        crawler = AsyncWebCrawler(CrawlerConfig(max_depth=1, max_pages=10, respect_robots=False))
        try:
            return await crawler.crawl("https://parent.example/")
        finally:
            await crawler.close()

    results = asyncio.run(run())
    child = next((r for r in results if r.url.endswith("/child")), None)
    assert child is not None
    assert child.source_url and "parent.example" in child.source_url
