# -*- coding: utf-8 -*-
"""Named crawl presets: catalog, resolution, and per-call application via tools."""

from __future__ import annotations

import json

import pytest

from lazycrawler import CrawlerConfig, HTTPConfig
from lazycrawler.presets import DEFAULT_PRESETS, CrawlPreset, resolve_presets
from lazycrawler.tools import CrawlerTools

# -- catalog / resolution --------------------------------------------------


def test_default_catalog_has_expected_presets():
    assert set(DEFAULT_PRESETS) == {
        "quick_lookup",
        "deep_research",
        "news_scan",
        "extract_data",
        "rag_ingest",
        "research_ml",
    }


def test_crawl_overrides_only_exposes_config_fields():
    ov = DEFAULT_PRESETS["rag_ingest"].crawl_overrides()
    assert ov["emit_markdown"] is True
    assert ov["markdown_artifact_anchors"] is True
    assert ov["extract_artifacts"] is True
    # content/links/depth/recency are NOT CrawlerConfig fields -> not in overrides
    for k in ("content", "links", "max_depth", "timelimit", "max_results"):
        assert k not in ov


def test_crawl_overrides_includes_branching_only_when_set():
    # quick_lookup leaves branching to the crawler default (max_depth 0 anyway)
    assert "max_links_per_level" not in DEFAULT_PRESETS["quick_lookup"].crawl_overrides()
    # deep_research widens the fan-out explicitly
    assert DEFAULT_PRESETS["deep_research"].crawl_overrides()["max_links_per_level"] == 25


def test_resolve_presets_merges_and_overrides():
    custom = {
        "tiny": CrawlPreset(name="tiny", description="x", max_pages=1),
        # override a built-in
        "deep_research": CrawlPreset(name="deep_research", description="y", max_pages=99),
    }
    merged = resolve_presets(custom)
    assert "tiny" in merged and "quick_lookup" in merged  # custom + defaults
    assert merged["deep_research"].max_pages == 99  # built-in overridden
    assert merged["tiny"].max_pages == 1


def test_resolve_presets_keeps_name_in_sync_with_key():
    # key is the source of truth even if the preset's own name disagrees
    custom = {"my_preset": CrawlPreset(name="WRONG", description="x")}
    merged = resolve_presets(custom)
    assert merged["my_preset"].name == "my_preset"


def test_resolve_presets_without_defaults():
    merged = resolve_presets(
        {"only": CrawlPreset(name="only", description="x")}, include_defaults=False
    )
    assert set(merged) == {"only"}


# -- application through CrawlerTools --------------------------------------


@pytest.fixture
def tools(tmp_db):
    ct = CrawlerTools(
        db=tmp_db,
        crawler_cfg=CrawlerConfig(max_depth=5, max_pages=20, respect_robots=False),
        http_cfg=HTTPConfig(verify_ssl=False, link_delay=0),
        content="pure",
    )
    yield ct
    ct.close()


def test_list_presets_includes_defaults(tools):
    out = json.loads(tools.list_presets())
    names = {p["name"] for p in out["presets"]}
    assert {"quick_lookup", "deep_research", "extract_data"} <= names
    # each brief carries the LLM-facing fields
    one = out["presets"][0]
    for key in ("name", "intent", "cost", "content", "follows_links", "depth"):
        assert key in one


def test_unknown_preset_returns_error_json(stub_fetch, tools):
    stub_fetch()
    out = json.loads(tools.web_crawl("https://e.org/p", preset="does_not_exist"))
    assert "error" in out
    assert "quick_lookup" in out["available"]


def test_preset_applies_artifacts_override_without_mutating_config(stub_fetch, tools):
    # A page with a real table so extract_data has something to pull.
    table_html = (
        "<table><tr><th>City</th><th>Pop</th></tr><tr><td>Rome</td><td>3M</td></tr></table>"
    )
    stub_fetch(links_map={"https://e.org/report": table_html})

    # baseline: instance cfg has artifacts OFF
    assert tools._crawler.cfg.extract_artifacts is False

    out = json.loads(tools.web_crawl("https://e.org/report", preset="extract_data"))
    assert out["found"] >= 1

    # the preset enabled artifacts for THIS call only — shared cfg untouched
    assert tools._crawler.cfg.extract_artifacts is False
    arts = json.loads(tools.get_artifacts("https://e.org/report"))
    assert any(a["type"] == "table" for a in arts["artifacts"])


def test_preset_drives_depth_and_explicit_depth_wins(stub_fetch, tools):
    # quick_lookup pins depth 0; even though instance cfg allows depth 5.
    links = '<a href="https://e.org/child">child</a>'
    stub_fetch(links_map={"https://e.org/root": links})

    out0 = json.loads(tools.web_crawl("https://e.org/root", preset="quick_lookup"))
    urls0 = {p["url"] for p in out0["pages"]}
    assert not any("child" in u for u in urls0)  # depth 0 -> no link following

    # explicit depth overrides the preset's depth
    out1 = json.loads(tools.web_crawl("https://e.org/root2", preset="quick_lookup", depth=1))
    stub2 = {p["url"] for p in out1["pages"]}
    assert len(stub2) >= 1


def test_preset_max_pages_override_is_per_call(stub_fetch, tools):
    # extract_data caps max_pages at 5; the instance allows 20. Crawl a fan-out
    # and confirm the per-call cap applied without touching shared cfg.
    fanout = "".join(f'<a href="https://e.org/p{i}">p{i}</a>' for i in range(12))
    stub_fetch(links_map={"https://e.org/seed": fanout})
    out = json.loads(tools.web_crawl("https://e.org/seed", preset="extract_data", depth=1))
    assert out["found"] <= 5
    assert tools._crawler.cfg.max_pages == 20  # shared cfg untouched


def test_branching_override_is_per_call(stub_fetch, make_crawler):
    # 10 children on the seed; an overrides branching of 2 must cap how many are
    # followed, per call, without mutating shared config.
    fanout = "".join(f'<a href="https://e.org/c{i}">c{i}</a>' for i in range(10))
    stub_fetch(links_map={"https://e.org/seed": fanout})
    c = make_crawler(max_depth=1, max_pages=50, max_links_per_level=15)
    res = c.crawl("https://e.org/seed", overrides={"max_links_per_level": 2})
    done = [r for r in res if r.status == "done"]
    assert len(done) <= 3  # seed + at most 2 followed children
    assert c.cfg.max_links_per_level == 15  # shared cfg untouched
