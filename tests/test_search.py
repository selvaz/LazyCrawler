# -*- coding: utf-8 -*-
"""search.py: DDG param passthrough and WebSearch -> crawl wiring (offline)."""

from __future__ import annotations

import sys
import types

import lazycrawler.search as search_mod
from lazycrawler import CrawlerConfig, HTTPConfig, SearchConfig
from lazycrawler.search import WebSearch, search_ddg_urls


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
