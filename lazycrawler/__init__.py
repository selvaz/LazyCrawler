# -*- coding: utf-8 -*-
"""
LazyCrawler — a generic web crawler + search with two modes (pure / smart).

  pure   = no LLM, no cost. trafilatura/regex + heuristic link selection.
  smart  = LLM via LazyBridge for structured extraction and link selection.

WebSearch is a derivation of WebCrawler: it seeds itself from a search engine's
results (DuckDuckGo or Gemini grounded) and then crawls.

To switch LLM provider/model just change ``LLMConfig.model`` — the provider is
inferred by LazyBridge.

Quick start
-----------
    from lazycrawler import WebCrawler, CrawlerConfig

    # pure mode, no LLM
    crawler = WebCrawler(CrawlerConfig(max_depth=1, max_pages=10))
    results = crawler.crawl("https://example.com/", mode="pure")
    for r in results:
        print(r.status, r.title, len(r.text or ""))

    # smart mode + DB persistence
    from lazycrawler import CrawlerDB, DBConfig, LLMConfig
    db = CrawlerDB(DBConfig(db_path="crawl.db", ttl_hours=12))
    crawler = WebCrawler(
        CrawlerConfig(max_depth=2, max_pages=20),
        llm_cfg=LLMConfig(model="claude-haiku-4-5"),
        db=db,
    )
    results = crawler.crawl("https://example.com/", mode="smart",
                            topic="your topic here")

    # web search
    from lazycrawler import WebSearch, SearchConfig
    search = WebSearch(SearchConfig(engine="duckduckgo", n_results=8))
    out = search.run("your query here", mode="pure")
"""

from __future__ import annotations

from ._log import log, set_log_level
from .artifacts import Artifact, extract_html_artifacts, extract_html_artifacts_anchored
from .config import (
    CrawlerConfig,
    DBConfig,
    HTTPConfig,
    LLMConfig,
    SearchConfig,
)
from .crawler import PageResult, WebCrawler
from .db import CrawlerDB
from .markdown import html_to_markdown, render_for_rag
from .presets import DEFAULT_PRESETS, CrawlPreset, resolve_presets
from .search import WebSearch, search_ddg_urls
from .tools import CrawlerTools

__version__ = "0.9.0"

__all__ = [
    "WebCrawler",
    "WebSearch",
    "CrawlerDB",
    "CrawlerTools",
    "PageResult",
    "Artifact",
    "extract_html_artifacts",
    "extract_html_artifacts_anchored",
    "html_to_markdown",
    "render_for_rag",
    "CrawlPreset",
    "DEFAULT_PRESETS",
    "resolve_presets",
    "CrawlerConfig",
    "HTTPConfig",
    "LLMConfig",
    "SearchConfig",
    "DBConfig",
    "search_ddg_urls",
    "set_log_level",
    "log",
    "__version__",
]
