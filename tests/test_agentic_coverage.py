"""Regression coverage for the agent-facing parity additions."""

from __future__ import annotations

import json

from pydantic import BaseModel

from lazycrawler import CrawlerConfig, HTTPConfig
from lazycrawler.tools import CrawlerTools


class FinancialArticle(BaseModel):
    headline: str


def _tools(**kwargs):
    return CrawlerTools(
        crawler_cfg=CrawlerConfig(max_depth=1, respect_robots=False),
        http_cfg=HTTPConfig(verify_ssl=False, link_delay=0),
        content="pure",
        enforce_ssrf_guard=False,
        **kwargs,
    )


def test_default_memory_db_keeps_retrievable_text(stub_fetch):
    body = "long retained content " * 100
    stub_fetch(body=body)
    tools = _tools()
    try:
        out = json.loads(tools.web_crawl("https://e.org/retained", depth=0))
        assert out["pages"][0]["full_text_available"] is True
        page = json.loads(tools.get_page("https://e.org/retained"))
        assert "long retained content" in page["untrusted_page_text"]
    finally:
        tools.close()


def test_refresh_is_per_call_and_cache_metadata_is_visible(stub_fetch):
    state = stub_fetch()
    tools = _tools()
    try:
        first = json.loads(tools.web_crawl("https://e.org/fresh", depth=0))
        second = json.loads(tools.web_crawl("https://e.org/fresh", depth=0))
        refreshed = json.loads(tools.web_crawl("https://e.org/fresh", depth=0, refresh=True))
        assert state["n"] == 2
        assert first["pages"][0]["crawled_at"]
        assert second["pages"][0]["from_cache"] is True
        assert refreshed["pages"][0]["cache_age_seconds"] is not None
    finally:
        tools.close()


def test_many_deduplicates_seeds_and_exposes_session_graph(stub_fetch):
    state = stub_fetch()
    tools = _tools()
    try:
        out = json.loads(tools.web_crawl_many(["https://e.org/a", "https://e.org/a"], depth=0))
        assert out["urls"] == ["https://e.org/a"]
        assert state["n"] == 1
        graph = json.loads(tools.get_crawl_graph(out["session_id"]))
        assert graph["nodes"]
    finally:
        tools.close()


def test_schema_registry_rejects_unknown_and_non_smart():
    tools = _tools(schemas={"financial_article": FinancialArticle})
    try:
        assert json.loads(tools.list_schemas())["schemas"] == [{"name": "financial_article"}]
        assert (
            json.loads(tools.web_crawl("https://e.org/a", schema="missing"))["error"]["code"]
            == "UNKNOWN_SCHEMA"
        )
        assert (
            json.loads(tools.web_crawl("https://e.org/a", schema="financial_article"))["error"][
                "code"
            ]
            == "SCHEMA_REQUIRES_SMART"
        )
    finally:
        tools.close()
