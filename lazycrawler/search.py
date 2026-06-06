# -*- coding: utf-8 -*-
"""
lazycrawler.search
==================
WebSearch: a derivation of WebCrawler that seeds itself from a search engine's
results instead of a fixed URL.

Supported engines
-----------------
duckduckgo (default)
    ddgs returns a list of URLs -> WebCrawler.crawl_many() crawls them.
    No LLM cost for the search step (the LLM is only used in smart mode for
    extraction/selection during the crawl).

gemini
    Grounded answer via LazyBridge native Google Search. The grounding source
    URLs do not surface through LazyBridge's Agent layer, so this engine runs in
    "answer mode": it returns the grounded answer as a single result. (Crawling
    seed URLs from Gemini would require a grounding passthrough in LazyBridge -
    see README.)
"""

from __future__ import annotations

from typing import List, Optional

from ._log import log
from .config import CrawlerConfig, HTTPConfig, LLMConfig, SearchConfig
from .crawler import PageResult, WebCrawler
from .db import CrawlerDB
from .http import is_blacklisted_domain, is_excluded_url, normalize_url, url_hash


# =============================================================================
# DUCKDUCKGO
# =============================================================================

def search_ddg_urls(query: str, max_results: int, blacklist: Optional[List[str]] = None) -> List[str]:
    """
    Search DuckDuckGo and return normalized URLs, filtered and with blacklisted
    domains removed. Empty list on error.

    Requires ``pip install ddgs`` (or the older ``duckduckgo_search``).
    """
    try:
        from ddgs import DDGS  # type: ignore
    except ImportError:
        try:
            from duckduckgo_search import DDGS  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "DuckDuckGo library missing. Install with: pip install ddgs"
            ) from e

    results: List[str] = []
    seen: set = set()
    try:
        with DDGS() as ddgs_client:
            for r in ddgs_client.text(query, max_results=max_results * 2):
                href = (r.get("href") or "").strip()
                if not href or not href.startswith(("http://", "https://")):
                    continue
                norm = normalize_url(href)
                if is_excluded_url(norm) or is_blacklisted_domain(norm, blacklist):
                    continue
                if norm in seen:
                    continue
                seen.add(norm)
                results.append(norm)
                if len(results) >= max_results:
                    break
    except Exception as e:
        log.warning("DuckDuckGo search failed (%s: %s)", type(e).__name__, e, exc_info=True)
    return results


# =============================================================================
# WEB SEARCH
# =============================================================================

class WebSearch:
    """
    Search a topic and crawl the results.

    Parameters
    ----------
    search_cfg : SearchConfig
        Engine, n_results, crawl_depth, same_domain_only, expand_topic.
    crawler_cfg : CrawlerConfig
        Crawler configuration. max_depth and same_domain_only are overridden by
        search_cfg (crawl_depth / same_domain_only).
    http_cfg, llm_cfg : optional configs passed to the crawler.
    db : CrawlerDB, optional
        Persistence with dedup; sessions use source="search:<engine>".
    """

    def __init__(
        self,
        search_cfg: Optional[SearchConfig] = None,
        crawler_cfg: Optional[CrawlerConfig] = None,
        http_cfg: Optional[HTTPConfig] = None,
        llm_cfg: Optional[LLMConfig] = None,
        db: Optional[CrawlerDB] = None,
    ):
        self.search_cfg = search_cfg or SearchConfig()
        self.llm_cfg = llm_cfg
        self.db = db

        # The crawler inherits the config, but search_cfg drives depth/domain.
        base = crawler_cfg or CrawlerConfig()
        base.max_depth = self.search_cfg.crawl_depth
        base.same_domain_only = self.search_cfg.same_domain_only
        self.crawler = WebCrawler(crawler_cfg=base, http_cfg=http_cfg, llm_cfg=llm_cfg, db=db)

    # -- public API -----------------------------------------------------------

    def run(
        self,
        query: str,
        *,
        mode: str = "pure",
        content: Optional[str] = None,
        links: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> dict:
        """
        Run search + crawl.

        ``mode`` sets both content and links; ``content=`` / ``links=`` override
        independently (see WebCrawler).

        Returns
        -------
        dict with: query, topic, engine, pages_found, results (List[PageResult]).
        """
        scfg = self.search_cfg
        engine = scfg.engine
        content_mode = content or mode
        link_mode = links or mode

        # Topic expansion (only useful for smart link selection, since the topic
        # drives link relevance ranking).
        topic = query
        if link_mode == "smart" and scfg.expand_topic:
            self.crawler._ensure_llm()
            topic = self.crawler._llm.expand_topic(query)

        log.info("web search: engine=%s content=%s links=%s query=%r%s",
                 engine, content_mode, link_mode, query,
                 f" topic={topic!r}" if topic != query else "")

        if engine == "gemini":
            results = self._run_gemini(query, topic, content_mode)
        else:
            results = self._run_duckduckgo(query, topic, content_mode, link_mode, session_id)

        pages_found = sum(1 for r in results if r.status == "done")
        log.info("web search done: %d pages extracted (%d entries)", pages_found, len(results))
        return {
            "query": query, "topic": topic, "engine": engine,
            "pages_found": pages_found, "results": results,
        }

    # -- DuckDuckGo: URLs -> crawl --------------------------------------------

    def _run_duckduckgo(self, query, topic, content_mode, link_mode, session_id) -> List[PageResult]:
        urls = search_ddg_urls(query, self.search_cfg.n_results, self.crawler.blacklist)
        log.info("%d URLs from DuckDuckGo", len(urls))
        for i, u in enumerate(urls, 1):
            log.debug("  %2d. %s", i, u[:90])
        if not urls:
            return []
        return self.crawler.crawl_many(
            urls, content=content_mode, links=link_mode, topic=topic,
            session_id=session_id, source="search:duckduckgo",
        )

    # -- Gemini grounded (answer mode) ----------------------------------------

    def _run_gemini(self, query, topic, content_mode) -> List[PageResult]:
        mode = content_mode
        try:
            from lazybridge import Agent, LLMEngine, NativeTool
        except ImportError as e:
            raise RuntimeError("Engine 'gemini' requires LazyBridge.") from e

        try:
            agent = Agent(engine=LLMEngine(
                self.search_cfg.gemini_model,
                native_tools=[NativeTool.GOOGLE_SEARCH],
            ))
            env = agent(
                "Use Google Search grounding. Search the public web and answer "
                "using grounded, current sources. Keep it compact.\n\n"
                f"Query: {query}\nExpanded topic: {topic}"
            )
            answer = (env.text() or "").strip() if env.ok else ""
        except Exception as e:
            log.warning("Gemini grounding failed (%s: %s)", type(e).__name__, e, exc_info=True)
            return []

        if not answer:
            return []

        url = "gemini://grounded-web-search"
        result = PageResult(
            url=url, url_hash=url_hash(url), status="done",
            mode="smart" if mode == "smart" else "pure",
            title="Gemini grounded web search",
            text=answer, summary=answer[:500], depth=0,
        )
        # optional persistence
        if self.db is not None:
            sid = self.crawler._default_session_id(topic, mode)
            self.db.create_session(sid, topic=topic, seed=query, mode=mode, source="search:gemini")
            self.db.upsert_page({
                "url": url, "url_hash": result.url_hash, "status": "done",
                "mode": result.mode, "title": result.title, "clean_text": answer,
                "summary": result.summary, "raw_text": answer,
            })
            self.db.add_edge(sid, result.url_hash, depth=0)
        return [result]
