# -*- coding: utf-8 -*-
"""Coverage for the critical+medium audit remediations:

#1 async redirect SSRF re-check, #2 async streamed body cap, #3 sync redirect
adoption, #4 registrable-domain scope, #5 per-call config in traversal,
#7 async provenance, #8 agent search cap, #10 untrusted-content labeling.
"""

from __future__ import annotations

import asyncio
import importlib.util

import pytest

from lazycrawler import HTTPConfig
from lazycrawler.http import FetchResult, get_hostname, registrable_domain, same_site

_HAS_AIOHTTP = importlib.util.find_spec("aiohttp") is not None
requires_aiohttp = pytest.mark.skipif(not _HAS_AIOHTTP, reason="requires aiohttp (the async extra)")

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


def test_redirect_to_shared_target_is_not_emitted_twice(monkeypatch, make_crawler):
    """Two distinct source URLs that redirect to the SAME final URL must produce a
    single page (no duplicate emission, no double count toward max_pages) —
    mirroring the canonical-adoption dedup guard."""

    def fetch(self, url, extra_headers=None):
        body = "Shared canonical target body, long enough to be real content. " * 4
        html = f"<html><body><p>{body}</p></body></html>"
        return FetchResult(
            html=html, text=body, status=200, final_url="https://target.example/final"
        )

    monkeypatch.setattr("lazycrawler.http.HTTPClient.fetch", fetch)
    c = make_crawler(max_depth=0, respect_robots=False)
    r = c.crawl_many(["https://a.example/one", "https://b.example/two"], mode="pure")

    finals = [p for p in r if p.url == "https://target.example/final"]
    assert len(finals) == 1  # emitted once, not once per source URL


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


@requires_aiohttp
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


# =============================================================================
# Deep-audit round 2 — F1/F2/F3/F4/F5/F6/F7
# =============================================================================


def test_normalize_url_strips_default_ports():
    """F6: http://h:80 and http://h dedup to one key (likewise https/:443)."""
    from lazycrawler.http import normalize_url, url_hash

    assert normalize_url("http://Example.com:80/a/") == "http://example.com/a"
    assert normalize_url("https://example.com:443/a") == "https://example.com/a"
    assert url_hash("http://example.com:80/a") == url_hash("http://example.com/a")
    assert url_hash("https://example.com:443/a") == url_hash("https://example.com/a")
    # A non-default port is preserved.
    assert normalize_url("http://example.com:8080/a") == "http://example.com:8080/a"


def test_pdf_import_basexception_degrades(monkeypatch):
    """F1: a native-dep panic (BaseException, not Exception) on importing the PDF
    parsers must degrade gracefully, not crash extraction."""
    import builtins

    from lazycrawler import pdf as pdfmod

    class FakePanic(BaseException):  # mimics pyo3_runtime.PanicException
        pass

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "fitz" or name == "pypdf" or name.startswith("pypdf."):
            raise FakePanic("simulated broken native dependency")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    # Must not raise despite BaseException being raised on import.
    text, title, pub = pdfmod.extract_pdf_bytes(b"%PDF-1.4 not a real pdf")
    assert text == ""
    assert title == ""


def test_parallel_worker_state_is_per_call(make_crawler, stub_fetch):
    """F2: worker resources live on the run state, not the crawler instance."""
    stub_fetch()
    c = make_crawler(max_workers=4, max_depth=0)
    # The per-worker bookkeeping no longer hangs off the shared instance.
    assert not hasattr(c, "_created_res")
    assert not hasattr(c, "_tls")
    results = c.crawl("https://e.org/a", mode="pure")
    assert results and results[0].status == "done"


def test_content_dedup_copy_keys_to_new_url(tmp_db, make_crawler, stub_fetch):
    """F5: a level-2 (content_hash) dedup copy is keyed to the NEW url and stays a
    valid independent 'done' row (the copy path, incl. fresh crawled_at)."""
    from lazycrawler.db import _parse_iso
    from lazycrawler.http import url_hash

    stub_fetch()  # identical DEFAULT_BODY for every URL -> same content_hash
    c = make_crawler(db=tmp_db, max_depth=0)
    c.crawl("https://a.org/x", mode="pure")
    c.crawl("https://b.org/y", mode="pure")  # triggers content-hash dedup copy
    row_b = tmp_db.get_page(url_hash("https://b.org/y"))
    assert row_b is not None
    assert row_b["status"] == "done"
    assert row_b["url"] == "https://b.org/y"
    assert _parse_iso(row_b["crawled_at"]) is not None


@requires_aiohttp
def test_async_http_ssl_param_and_min_text():
    """F3 + F4: async client honors ca_bundle/verify_ssl and min_text_chars."""
    import ssl

    from lazycrawler.async_crawler import _AsyncHTTPClient

    assert _AsyncHTTPClient(HTTPConfig(verify_ssl=False))._ssl_param() is False
    assert _AsyncHTTPClient(HTTPConfig(verify_ssl=True))._ssl_param() is True

    certifi = pytest.importorskip("certifi")
    ctx = _AsyncHTTPClient(HTTPConfig(ca_bundle=certifi.where()))._ssl_param()
    assert isinstance(ctx, ssl.SSLContext)

    short_html = "<html><body><p>tiny</p></body></html>"
    assert _AsyncHTTPClient(HTTPConfig(min_text_chars=10000))._extract(short_html) is None
    long_cfg = _AsyncHTTPClient(HTTPConfig(min_text_chars=4))
    assert long_cfg._extract("<html><body><p>enough words here</p></body></html>")


@requires_aiohttp
def test_async_render_js_is_disabled():
    """F7: render_js is ignored (not fatal) on the async path."""
    from lazycrawler.async_crawler import AsyncWebCrawler

    crawler = AsyncWebCrawler(http_cfg=HTTPConfig(render_js=True, block_private_addresses=True))
    assert crawler.http_cfg.render_js is False
    assert crawler.http_cfg.block_private_addresses is True


# =============================================================================
# Deep-audit round 3 — async parity: robots SSRF, strict isolation
# =============================================================================


@requires_aiohttp
def test_async_get_robots_respects_ssrf_guard_and_scheme(monkeypatch):
    """H1: robots.txt is a live GET too — a blocked host (or a non-http scheme)
    must not be reached just because the guard refused the main fetch."""
    import lazycrawler.async_crawler as ac
    from lazycrawler.async_crawler import _AsyncHTTPClient

    client = _AsyncHTTPClient(HTTPConfig(block_private_addresses=True))

    async def always_blocked(url: str) -> bool:
        return True

    # If the guard is honored, _ensure()/the network is never touched.
    def explode(self):
        raise AssertionError("must not open a session for a blocked robots.txt")

    monkeypatch.setattr(ac, "_is_blocked_async", always_blocked)
    monkeypatch.setattr(_AsyncHTTPClient, "_ensure", explode)

    assert asyncio.run(client.get_robots("http://10.0.0.5:8080/page")) is None
    # Non-http scheme is rejected outright.
    assert asyncio.run(client.get_robots("file:///etc/passwd")) is None


@requires_aiohttp
def test_async_process_skips_robots_on_blocked_redirect(monkeypatch):
    """H1: when a fetch produced no content (SSRF-refused / never-validated hop),
    _process must not issue a robots.txt check against that hop."""
    import lazycrawler.async_crawler as ac
    from lazycrawler import CrawlerConfig
    from lazycrawler.async_crawler import AsyncWebCrawler, _AsyncFetchResult

    async def no_block(url: str) -> bool:
        return False

    monkeypatch.setattr(ac, "_is_blocked_async", no_block)

    async def fake_fetch(self, url):
        # Simulate a fetch refused after a redirect: no status, final_url is the
        # blocked private hop (this is what _fetch_once returns on SSRF refusal).
        return _AsyncFetchResult(status=None, final_url="http://10.0.0.5:8080/x")

    monkeypatch.setattr(ac._AsyncHTTPClient, "fetch", fake_fetch)

    checked = []

    async def spy_allowed(self, url):
        checked.append(url)
        return True

    monkeypatch.setattr(ac._AsyncRobotsChecker, "allowed", spy_allowed)

    async def run():
        c = AsyncWebCrawler(CrawlerConfig(max_depth=0, max_pages=5, respect_robots=True))
        try:
            return await c.crawl("https://public.example/seed")
        finally:
            await c.close()

    asyncio.run(run())
    # The seed itself is checked, but never the blocked redirect target.
    assert not any("10.0.0.5" in u for u in checked)


@requires_aiohttp
def test_async_rate_limiter_honors_robots_crawl_delay():
    """M-C2: the async rate limiter takes max(per_host_delay, robots Crawl-delay),
    matching the sync HostRateLimiter."""
    from lazycrawler.async_crawler import _AsyncRateLimiter

    class _FakeRobots:
        async def crawl_delay(self, url):
            return 5.0

    rl = _AsyncRateLimiter(0.0, _FakeRobots())
    # per_host_delay is 0 but robots says 5s -> effective delay is 5s.
    assert asyncio.run(rl._delay_for("https://slow.example/x")) == 5.0
    # Without a robots checker it stays at the configured default.
    assert asyncio.run(_AsyncRateLimiter(0.0)._delay_for("https://x/y")) == 0.0


@requires_aiohttp
def test_async_strict_false_isolates_worker_failure(monkeypatch):
    """H2: a single page raising in the executor pipeline must not discard the
    whole crawl under strict=False; under strict=True it propagates."""
    import lazycrawler.async_crawler as ac
    from lazycrawler import CrawlerConfig
    from lazycrawler.async_crawler import AsyncWebCrawler, _AsyncFetchResult

    async def no_block(url: str) -> bool:
        return False

    monkeypatch.setattr(ac, "_is_blocked_async", no_block)

    seed = "https://e.org/seed"
    body = "Body long enough to be treated as real article text now. " * 4

    async def fake_fetch(self, url):
        extra = (
            '<a href="https://e.org/good">good</a><a href="https://e.org/bad">bad</a>'
            if url.rstrip("/") == seed
            else ""
        )
        html = f"<html><body><p>{body}</p>{extra}</body></html>"
        return _AsyncFetchResult(html=html, text=body, status=200, final_url=url)

    monkeypatch.setattr(ac._AsyncHTTPClient, "fetch", fake_fetch)

    orig = AsyncWebCrawler._run_pipeline

    def boom(self, st, url, depth, source_url, start_domain, fr):
        if url.endswith("/bad"):
            raise RuntimeError("worker blew up")
        return orig(self, st, url, depth, source_url, start_domain, fr)

    monkeypatch.setattr(AsyncWebCrawler, "_run_pipeline", boom)

    async def run(strict):
        c = AsyncWebCrawler(
            CrawlerConfig(max_depth=1, max_pages=10, respect_robots=False, strict=strict)
        )
        try:
            return await c.crawl(seed)
        finally:
            await c.close()

    # strict=False (default): the good page + seed survive; the bad one is dropped.
    results = asyncio.run(run(False))
    urls = {r.url for r in results}
    assert any(u.endswith("/seed") for u in urls)
    assert any(u.endswith("/good") for u in urls)

    # strict=True: the failure surfaces to the caller.
    with pytest.raises(RuntimeError):
        asyncio.run(run(True))
