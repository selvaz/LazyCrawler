# -*- coding: utf-8 -*-
"""CrawlerTools: response shapes, session ids, thread-safety, get_session_pages."""

from __future__ import annotations

import json
import threading

import pytest

from lazycrawler import CrawlerConfig, HTTPConfig
from lazycrawler.tools import CrawlerTools


@pytest.fixture
def tools(tmp_db):
    ct = CrawlerTools(
        db=tmp_db,
        crawler_cfg=CrawlerConfig(max_depth=5, respect_robots=False),
        http_cfg=HTTPConfig(verify_ssl=False, link_delay=0),
        content="pure",
    )
    yield ct
    ct.close()


def test_web_crawl_returns_enriched_json(stub_fetch, tools):
    stub_fetch()
    out = json.loads(tools.web_crawl("https://e.org/page", depth=0))
    assert out["found"] >= 1
    assert "session_id" in out and out["session_id"]
    page = out["pages"][0]
    for key in ("url", "title", "snippet", "from_cache", "depth", "source_url"):
        assert key in page


def test_web_crawl_does_not_mutate_config(stub_fetch, tools):
    stub_fetch()
    tools.web_crawl("https://e.org/a", depth=0)
    assert tools._crawler.cfg.max_depth == 5  # config untouched by the call


def test_web_crawl_concurrent_depths_isolated(stub_fetch, tools):
    stub_fetch()
    results = {}

    def run(name, depth):
        results[name] = json.loads(tools.web_crawl(f"https://e.org/{name}", depth=depth))

    threads = [threading.Thread(target=run, args=(n, d)) for n, d in (("a", 0), ("b", 0), ("c", 0))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # shared config never clobbered by concurrent calls
    assert tools._crawler.cfg.max_depth == 5
    assert all(r["found"] >= 1 for r in results.values())
    # each call reports its own distinct session
    sids = {r["session_id"] for r in results.values()}
    assert len(sids) == 3


def test_get_page_and_session_pages(stub_fetch, tools):
    stub_fetch()
    out = json.loads(tools.web_crawl("https://e.org/sess", depth=0))
    sid = out["session_id"]

    sp = json.loads(tools.get_session_pages(sid))
    assert sp["session_id"] == sid and sp["found"] >= 1

    gp = json.loads(tools.get_page("https://e.org/sess"))
    assert gp.get("text")


def test_get_session_pages_unknown(tools):
    sp = json.loads(tools.get_session_pages("does-not-exist"))
    assert sp["found"] == 0


@pytest.mark.integration
def test_as_tools_exposes_expected_tools(tools):
    pytest.importorskip("lazybridge")
    names = {getattr(t, "name", None) for t in tools.as_tools()}
    assert {"web_search", "web_crawl", "get_page", "search_cached", "get_session_pages"} <= names
    # lifecycle methods must NEVER be exposed to the agent.
    assert "close" not in names


# -- SSRF guard enforcement (audit #2) -------------------------------------


def test_enforce_ssrf_guard_default_on():
    ct = CrawlerTools(
        http_cfg=HTTPConfig(block_private_addresses=False, verify_ssl=False), content="pure"
    )
    assert ct._crawler.http_cfg.block_private_addresses is True  # forced on
    ct.close()


def test_enforce_ssrf_guard_opt_out():
    ct = CrawlerTools(
        http_cfg=HTTPConfig(block_private_addresses=False, verify_ssl=False),
        enforce_ssrf_guard=False,
        content="pure",
    )
    assert ct._crawler.http_cfg.block_private_addresses is False  # honored
    ct.close()
