# -*- coding: utf-8 -*-
"""search.py: DDG/Brave/Tavily param passthrough and WebSearch -> crawl wiring (offline)."""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

import pytest

import lazycrawler.search as search_mod
from lazycrawler import CrawlerConfig, HTTPConfig, SearchConfig
from lazycrawler.search import WebSearch, search_brave_urls, search_ddg_urls, search_tavily_urls


def test_ddg_params_passthrough(monkeypatch):
    captured = {}

    class FakeDDGS:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def text(self, query, **kw):
            captured.update(kw)
            captured["query"] = query
            return [{"href": "https://e.org/r1"}, {"href": "https://e.org/r2"}]

    fake = types.ModuleType("ddgs")
    fake.DDGS = FakeDDGS
    monkeypatch.setitem(sys.modules, "ddgs", fake)

    urls = search_ddg_urls("q", 2, region="us-en", safesearch="off", timelimit="w", backend="lite")
    assert urls == ["https://e.org/r1", "https://e.org/r2"]
    assert captured["region"] == "us-en"
    assert captured["safesearch"] == "off"
    assert captured["timelimit"] == "w"
    assert captured["backend"] == "lite"


def test_ddg_falls_back_when_kwargs_unsupported(monkeypatch):
    seen = {"with_kwargs": 0, "basic": 0}

    class OldDDGS:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def text(self, query, **kw):
            # old client only accepts max_results
            if set(kw) - {"max_results"}:
                seen["with_kwargs"] += 1
                raise TypeError("unexpected keyword")
            seen["basic"] += 1
            return [{"href": "https://e.org/ok"}]

    fake = types.ModuleType("ddgs")
    fake.DDGS = OldDDGS
    monkeypatch.setitem(sys.modules, "ddgs", fake)

    urls = search_ddg_urls("q", 1, region="us-en")
    assert urls == ["https://e.org/ok"]
    assert seen["with_kwargs"] == 1 and seen["basic"] == 1


def test_websearch_crawls_results(monkeypatch, stub_fetch, tmp_db):
    stub_fetch()
    monkeypatch.setattr(
        search_mod, "search_ddg_urls", lambda *a, **k: ["https://e.org/r1", "https://e.org/r2"]
    )
    ws = WebSearch(
        SearchConfig(engine="duckduckgo", n_results=2),
        crawler_cfg=CrawlerConfig(respect_robots=False),
        http_cfg=HTTPConfig(verify_ssl=False, link_delay=0),
        db=tmp_db,
    )
    out = ws.run("anything", mode="pure")
    ws.crawler.close()
    assert out["engine"] == "duckduckgo"
    assert out["pages_found"] == 2
    assert {r.url for r in out["results"]} == {"https://e.org/r1", "https://e.org/r2"}


# =============================================================================
# BRAVE SEARCH
# =============================================================================


def _brave_response(urls):
    """Build a minimal Brave Search API response dict."""
    return {"web": {"results": [{"url": u, "title": f"T {u}", "description": "desc"} for u in urls]}}


def test_brave_no_api_key_raises():
    with pytest.raises(RuntimeError, match="API key"):
        search_brave_urls("q", 3, api_key="")


def test_brave_params_and_url_extraction(monkeypatch):
    captured = {}

    def fake_get(url, *, params, headers, timeout):
        captured["url"] = url
        captured["params"] = dict(params)
        captured["token"] = headers.get("X-Subscription-Token")
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json.return_value = _brave_response(
            ["https://brave.org/a", "https://brave.org/b", "https://bad"]
        )
        return resp

    monkeypatch.setattr(search_mod, "os", MagicMock(getenv=lambda k, d="": "TESTKEY" if k == "BRAVE_API_KEY" else d))

    with patch("lazycrawler.search._requests" if hasattr(search_mod, "_requests") else "requests.get", fake_get, create=True):
        import requests as _req
        monkeypatch.setattr(_req, "get", fake_get)
        urls = search_brave_urls("test query", 2, api_key="TESTKEY")

    assert len(urls) == 2
    assert all(u.startswith("https://brave.org/") for u in urls)


def test_brave_timelimit_mapped():
    """timelimit 'd'/'w'/'m'/'y' maps to Brave freshness pd/pw/pm/py."""
    captured_params = {}

    def fake_get(url, *, params, headers, timeout):
        captured_params.update(params)
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json.return_value = _brave_response([])
        return resp

    import requests as _req
    with patch.object(_req, "get", fake_get):
        search_brave_urls("q", 5, api_key="KEY", timelimit="w")
    assert captured_params.get("freshness") == "pw"


def _brave_fake_get_region(store):
    def fake_get(url, *, params, headers, timeout):
        store.update(params)
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json.return_value = _brave_response([])
        return resp

    return fake_get


def test_brave_region_us_maps_to_country():
    """'us-en' -> country='US'."""
    import requests as _req

    store: dict = {}
    with patch.object(_req, "get", _brave_fake_get_region(store)):
        search_brave_urls("q", 3, api_key="KEY", region="us-en")
    assert store.get("country") == "US"


def test_brave_region_global_omits_country():
    """'wt-wt' -> no country param."""
    import requests as _req

    store: dict = {}
    with patch.object(_req, "get", _brave_fake_get_region(store)):
        search_brave_urls("q", 3, api_key="KEY", region="wt-wt")
    assert store.get("country") is None


def test_brave_blacklist_filtered():
    import requests as _req

    def fake_get(url, *, params, headers, timeout):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json.return_value = _brave_response(
            ["https://good.org/page", "https://blocked.com/page"]
        )
        return resp

    with patch.object(_req, "get", fake_get):
        urls = search_brave_urls("q", 5, api_key="K", blacklist=["blocked.com"])
    assert urls == ["https://good.org/page"]


def test_websearch_brave_wires_correctly(monkeypatch, stub_fetch, tmp_db):
    stub_fetch()
    monkeypatch.setattr(
        search_mod,
        "search_brave_urls",
        lambda *a, **k: ["https://e.org/b1", "https://e.org/b2"],
    )
    ws = WebSearch(
        SearchConfig(engine="brave", n_results=2, brave_api_key="K"),
        crawler_cfg=CrawlerConfig(respect_robots=False),
        http_cfg=HTTPConfig(verify_ssl=False, link_delay=0),
        db=tmp_db,
    )
    out = ws.run("anything", mode="pure")
    ws.crawler.close()
    assert out["engine"] == "brave"
    assert out["pages_found"] == 2
    assert {r.url for r in out["results"]} == {"https://e.org/b1", "https://e.org/b2"}


# =============================================================================
# TAVILY SEARCH
# =============================================================================


def _tavily_response(urls):
    return {"results": [{"url": u, "title": f"T {u}", "content": "content"} for u in urls]}


def test_tavily_no_api_key_raises():
    with pytest.raises(RuntimeError, match="API key"):
        search_tavily_urls("q", 3, api_key="")


def test_tavily_params_and_url_extraction():
    import requests as _req
    captured = {}

    def fake_post(url, *, json, headers, timeout):
        captured["url"] = url
        captured["payload"] = dict(json)
        captured["auth"] = headers.get("Authorization")
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json.return_value = _tavily_response(
            ["https://tavily.io/a", "https://tavily.io/b"]
        )
        return resp

    with patch.object(_req, "post", fake_post):
        urls = search_tavily_urls("test query", 2, api_key="TKEY")

    assert urls == ["https://tavily.io/a", "https://tavily.io/b"]
    assert captured["auth"] == "Bearer TKEY"
    assert captured["payload"]["query"] == "test query"
    assert captured["payload"]["search_depth"] == "basic"


def test_tavily_timelimit_mapped():
    import requests as _req
    captured = {}

    def fake_post(url, *, json, headers, timeout):
        captured.update(json)
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json.return_value = _tavily_response([])
        return resp

    with patch.object(_req, "post", fake_post):
        search_tavily_urls("q", 3, api_key="K", timelimit="w")
    assert captured.get("days") == 7


def test_tavily_advanced_depth():
    import requests as _req
    captured = {}

    def fake_post(url, *, json, headers, timeout):
        captured.update(json)
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json.return_value = _tavily_response([])
        return resp

    with patch.object(_req, "post", fake_post):
        search_tavily_urls("q", 3, api_key="K", search_depth="advanced")
    assert captured.get("search_depth") == "advanced"


def test_tavily_blacklist_filtered():
    import requests as _req

    def fake_post(url, *, json, headers, timeout):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json.return_value = _tavily_response(
            ["https://good.org/x", "https://evil.com/x"]
        )
        return resp

    with patch.object(_req, "post", fake_post):
        urls = search_tavily_urls("q", 5, api_key="K", blacklist=["evil.com"])
    assert urls == ["https://good.org/x"]


def test_websearch_tavily_wires_correctly(monkeypatch, stub_fetch, tmp_db):
    stub_fetch()
    monkeypatch.setattr(
        search_mod,
        "search_tavily_urls",
        lambda *a, **k: ["https://e.org/t1", "https://e.org/t2"],
    )
    ws = WebSearch(
        SearchConfig(engine="tavily", n_results=2, tavily_api_key="K"),
        crawler_cfg=CrawlerConfig(respect_robots=False),
        http_cfg=HTTPConfig(verify_ssl=False, link_delay=0),
        db=tmp_db,
    )
    out = ws.run("anything", mode="pure")
    ws.crawler.close()
    assert out["engine"] == "tavily"
    assert out["pages_found"] == 2
    assert {r.url for r in out["results"]} == {"https://e.org/t1", "https://e.org/t2"}
