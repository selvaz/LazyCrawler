"""Regression coverage for the agent-facing parity additions."""

from __future__ import annotations

import json

from pydantic import BaseModel

from lazycrawler import CrawlerConfig, HTTPConfig
from lazycrawler.tools import CrawlerTools


class FinancialArticle(BaseModel):
    headline: str


def _tools(db_factory=None, **kwargs):
    # `db` must be explicit: CrawlerTools() falls back to
    # LAZYCRAWLER_NEWS_DB when neither `db` nor a `db_path` override is
    # given, and that env var is set at User level on the machines these
    # tests run on, pointing at the real production news DB. Without this,
    # the test writes/reads rows against production, poisoning a later run
    # of itself within the source TTL (D1 in
    # ecosystem-cleanup/docs/deferred-fixes.md). `db_factory` (tests/conftest.py)
    # builds one on a tmp_path file and closes it in fixture teardown.
    #
    # `db_factory=None` is only for the one test that means to exercise the
    # real no-db default (the in-memory fallback) instead of routing around
    # it -- that test deletes the env var itself rather than calling this
    # with a factory.
    if db_factory is not None:
        kwargs.setdefault("db", db_factory())
    return CrawlerTools(
        crawler_cfg=CrawlerConfig(max_depth=1, respect_robots=False),
        http_cfg=HTTPConfig(verify_ssl=False, link_delay=0),
        content="pure",
        enforce_ssrf_guard=False,
        **kwargs,
    )


def test_default_memory_db_keeps_retrievable_text(stub_fetch, monkeypatch):
    # This test's whole point is the no-db, no-env-var default (CrawlerTools
    # falls back to an in-memory :memory: cache), so it must NOT go through
    # db_factory like the others below -- doing so would make this test pass
    # even if that in-memory fallback were broken. Deleting the env var
    # (rather than routing through _tools()/db_factory) is what keeps this on
    # the real default while still not touching production if the var
    # happens to be set on the machine running it. Flagged by Codex review on
    # the PR that first introduced db_factory here.
    monkeypatch.delenv("LAZYCRAWLER_NEWS_DB", raising=False)
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


def test_refresh_is_per_call_and_cache_metadata_is_visible(stub_fetch, db_factory):
    state = stub_fetch()
    tools = _tools(db_factory)
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


def test_many_deduplicates_seeds_and_exposes_session_graph(stub_fetch, db_factory):
    state = stub_fetch()
    tools = _tools(db_factory)
    try:
        out = json.loads(tools.web_crawl_many(["https://e.org/a", "https://e.org/a"], depth=0))
        assert out["urls"] == ["https://e.org/a"]
        assert state["n"] == 1
        graph = json.loads(tools.get_crawl_graph(out["session_id"]))
        assert graph["nodes"]
    finally:
        tools.close()


def test_schema_registry_rejects_unknown_and_non_smart(db_factory):
    tools = _tools(db_factory, schemas={"financial_article": FinancialArticle})
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
