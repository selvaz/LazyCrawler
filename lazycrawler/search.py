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
    Requires: pip install ddgs

brave
    Brave Search REST API: privacy-first, own index (not a Google/Bing wrapper).
    Free tier: 2 000 queries/month (Data for Search plan).
    Requires: BRAVE_API_KEY env var or SearchConfig(brave_api_key=...).
    No additional Python dependency (uses ``requests``, already a core dep).

tavily
    Tavily Search API, optimised for LLM agents: returns pre-cleaned snippets
    alongside URLs, well-suited for the smart-content RAG pipeline.
    Free tier: 1 000 queries/month.
    Requires: TAVILY_API_KEY env var or SearchConfig(tavily_api_key=...).
    No additional Python dependency (uses ``requests``).

gemini
    Grounded answer via LazyBridge native Google Search. The grounding source
    URLs do not surface through LazyBridge's Agent layer, so this engine runs in
    "answer mode": it returns the grounded answer as a single result. (Crawling
    seed URLs from Gemini would require a grounding passthrough in LazyBridge -
    see README.)
"""

from __future__ import annotations

import os
from dataclasses import replace
from typing import List, Optional

from ._log import log
from .config import CrawlerConfig, HTTPConfig, LLMConfig, SearchConfig
from .crawler import PageResult, WebCrawler
from .db import CrawlerDB
from .http import is_blacklisted_domain, is_excluded_url, normalize_url, url_hash

# =============================================================================
# DUCKDUCKGO
# =============================================================================


def search_ddg_urls(
    query: str,
    max_results: int,
    blacklist: Optional[List[str]] = None,
    *,
    region: str = "wt-wt",
    safesearch: str = "moderate",
    timelimit: Optional[str] = None,
    backend: str = "auto",
) -> List[str]:
    """
    Search DuckDuckGo and return normalized URLs, filtered and with blacklisted
    domains removed. Empty list on error.

    ``region`` / ``safesearch`` / ``timelimit`` / ``backend`` are passed through
    to ddgs (see SearchConfig). Unsupported kwargs are ignored gracefully.

    Requires ``pip install ddgs`` (or the older ``duckduckgo_search``).
    """
    try:
        from ddgs import DDGS  # type: ignore
    except ImportError:
        try:
            from duckduckgo_search import DDGS  # type: ignore
        except ImportError as e:
            raise RuntimeError("DuckDuckGo library missing. Install with: pip install ddgs") from e

    text_kwargs = dict(
        max_results=max_results * 2,
        region=region,
        safesearch=safesearch,
        timelimit=timelimit,
        backend=backend,
    )

    results: List[str] = []
    seen: set = set()
    try:
        with DDGS() as ddgs_client:
            try:
                hits = ddgs_client.text(query, **text_kwargs)
            except TypeError:
                # older ddgs/duckduckgo_search without some kwargs
                log.debug("ddgs.text rejected extra kwargs - retrying with basics", exc_info=True)
                hits = ddgs_client.text(query, max_results=max_results * 2)
            for r in hits:
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
# BRAVE SEARCH
# =============================================================================

# Map the shared timelimit codes to Brave's freshness parameter.
_BRAVE_FRESHNESS: dict = {"d": "pd", "w": "pw", "m": "pm", "y": "py"}


def search_brave_urls(
    query: str,
    max_results: int,
    api_key: str = "",
    blacklist: Optional[List[str]] = None,
    *,
    safesearch: str = "moderate",
    timelimit: Optional[str] = None,
    region: str = "wt-wt",
) -> List[str]:
    """
    Search Brave and return normalized URLs.

    API reference: https://api.search.brave.com/res/v1/web/search
    Free tier: 2 000 queries/month (Data for Search plan).

    Parameters
    ----------
    api_key : str
        Brave Search API key. Falls back to the ``BRAVE_API_KEY`` environment
        variable when empty.
    safesearch : str
        "off" | "moderate" | "strict". Brave accepts these directly.
    timelimit : str | None
        "d"->past day, "w"->past week, "m"->past month, "y"->past year, None = any.
    region : str
        Region code (e.g. "us-en"). The first segment before "-" is used as the
        ISO-3166-1 alpha-2 country code (e.g. "us-en" -> "US"). "wt-wt" = global.
    """
    key = api_key or os.getenv("BRAVE_API_KEY", "")
    if not key:
        raise RuntimeError(
            "Brave Search requires an API key. "
            "Set SearchConfig(brave_api_key=...) or the BRAVE_API_KEY environment variable. "
            "Get a free key at https://brave.com/search/api/"
        )

    import requests as _requests

    params: dict = {
        "q": query,
        "count": min(max_results * 2, 20),  # Brave max per request = 20
        "safesearch": safesearch,
    }
    freshness = _BRAVE_FRESHNESS.get(timelimit or "")
    if freshness:
        params["freshness"] = freshness
    # Derive country from region string ("us-en" -> "US", "wt-wt" -> omit)
    if region and region != "wt-wt":
        country = region.split("-")[0].upper()
        if len(country) == 2:
            params["country"] = country

    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": key,
    }

    results: List[str] = []
    seen: set = set()
    try:
        resp = _requests.get(
            "https://api.search.brave.com/res/v1/web/search",
            params=params,
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        for r in (data.get("web") or {}).get("results") or []:
            url = (r.get("url") or "").strip()
            if not url or not url.startswith(("http://", "https://")):
                continue
            norm = normalize_url(url)
            if is_excluded_url(norm) or is_blacklisted_domain(norm, blacklist):
                continue
            if norm in seen:
                continue
            seen.add(norm)
            results.append(norm)
            if len(results) >= max_results:
                break
    except Exception as e:
        log.warning("Brave Search failed (%s: %s)", type(e).__name__, e, exc_info=True)
    return results


# =============================================================================
# TAVILY SEARCH
# =============================================================================

# Map shared timelimit codes to Tavily's ``days`` parameter.
_TAVILY_DAYS: dict = {"d": 1, "w": 7, "m": 30, "y": 365}


def search_tavily_urls(
    query: str,
    max_results: int,
    api_key: str = "",
    blacklist: Optional[List[str]] = None,
    *,
    search_depth: str = "basic",
    timelimit: Optional[str] = None,
) -> List[str]:
    """
    Search Tavily and return normalized URLs.

    API reference: https://docs.tavily.com/documentation/api-reference/endpoint/search
    Free tier: 1 000 queries/month.

    Parameters
    ----------
    api_key : str
        Tavily API key. Falls back to the ``TAVILY_API_KEY`` environment variable.
    search_depth : str
        "basic" (faster, fewer credits) or "advanced" (deeper recall, 2x credits).
    timelimit : str | None
        "d"->1 day, "w"->7 days, "m"->30 days, "y"->365 days, None = any.
    """
    key = api_key or os.getenv("TAVILY_API_KEY", "")
    if not key:
        raise RuntimeError(
            "Tavily requires an API key. "
            "Set SearchConfig(tavily_api_key=...) or the TAVILY_API_KEY environment variable. "
            "Get a free key at https://tavily.com/"
        )

    import requests as _requests

    payload: dict = {
        "query": query,
        "max_results": min(max_results * 2, 20),
        "search_depth": search_depth,
        "include_answer": False,
        "include_raw_content": False,
    }
    days = _TAVILY_DAYS.get(timelimit or "")
    if days:
        payload["days"] = days

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    results: List[str] = []
    seen: set = set()
    try:
        resp = _requests.post(
            "https://api.tavily.com/search",
            json=payload,
            headers=headers,
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        for r in data.get("results") or []:
            url = (r.get("url") or "").strip()
            if not url or not url.startswith(("http://", "https://")):
                continue
            norm = normalize_url(url)
            if is_excluded_url(norm) or is_blacklisted_domain(norm, blacklist):
                continue
            if norm in seen:
                continue
            seen.add(norm)
            results.append(norm)
            if len(results) >= max_results:
                break
    except Exception as e:
        log.warning("Tavily search failed (%s: %s)", type(e).__name__, e, exc_info=True)
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
        # Copy first so WebSearch does not mutate a caller-owned CrawlerConfig.
        base = replace(
            crawler_cfg or CrawlerConfig(),
            max_depth=self.search_cfg.crawl_depth,
            same_domain_only=self.search_cfg.same_domain_only,
        )
        self.crawler = WebCrawler(crawler_cfg=base, http_cfg=http_cfg, llm_cfg=llm_cfg, db=db)

    def close(self) -> None:
        """Release the underlying crawler's HTTP/browser resources."""
        self.crawler.close()

    def __enter__(self) -> "WebSearch":
        return self

    def __exit__(self, *exc) -> bool:
        self.close()
        return False

    # -- public API -----------------------------------------------------------

    def run(
        self,
        query: str,
        *,
        mode: str = "pure",
        content: Optional[str] = None,
        links: Optional[str] = None,
        session_id: Optional[str] = None,
        max_results: Optional[int] = None,
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

        log.info(
            "web search: engine=%s content=%s links=%s query=%r%s",
            engine,
            content_mode,
            link_mode,
            query,
            f" topic={topic!r}" if topic != query else "",
        )

        if engine == "gemini":
            results = self._run_gemini(query, topic, content_mode)
        elif engine == "brave":
            results = self._run_brave(query, topic, content_mode, link_mode, session_id, max_results)
        elif engine == "tavily":
            results = self._run_tavily(
                query, topic, content_mode, link_mode, session_id, max_results
            )
        else:
            results = self._run_duckduckgo(
                query, topic, content_mode, link_mode, session_id, max_results
            )

        pages_found = sum(1 for r in results if r.status == "done")
        log.info("web search done: %d pages extracted (%d entries)", pages_found, len(results))
        return {
            "query": query,
            "topic": topic,
            "engine": engine,
            "pages_found": pages_found,
            "results": results,
        }

    # -- DuckDuckGo: URLs -> crawl --------------------------------------------

    def _run_duckduckgo(
        self, query, topic, content_mode, link_mode, session_id, max_results
    ) -> List[PageResult]:
        n_results = self.search_cfg.n_results if max_results is None else max(1, int(max_results))
        scfg = self.search_cfg
        urls = search_ddg_urls(
            query,
            n_results,
            self.crawler.blacklist,
            region=scfg.region,
            safesearch=scfg.safesearch,
            timelimit=scfg.timelimit,
            backend=scfg.backend,
        )
        log.info("%d URLs from DuckDuckGo", len(urls))
        for i, u in enumerate(urls, 1):
            log.debug("  %2d. %s", i, u[:90])
        if not urls:
            return []
        return self.crawler.crawl_many(
            urls,
            content=content_mode,
            links=link_mode,
            topic=topic,
            session_id=session_id,
            source="search:duckduckgo",
        )

    # -- Brave Search: URLs -> crawl ------------------------------------------

    def _run_brave(
        self, query, topic, content_mode, link_mode, session_id, max_results
    ) -> List[PageResult]:
        n_results = self.search_cfg.n_results if max_results is None else max(1, int(max_results))
        scfg = self.search_cfg
        urls = search_brave_urls(
            query,
            n_results,
            scfg.brave_api_key,
            self.crawler.blacklist,
            safesearch=scfg.safesearch,
            timelimit=scfg.timelimit,
            region=scfg.region,
        )
        log.info("%d URLs from Brave Search", len(urls))
        for i, u in enumerate(urls, 1):
            log.debug("  %2d. %s", i, u[:90])
        if not urls:
            return []
        return self.crawler.crawl_many(
            urls,
            content=content_mode,
            links=link_mode,
            topic=topic,
            session_id=session_id,
            source="search:brave",
        )

    # -- Tavily Search: URLs -> crawl -----------------------------------------

    def _run_tavily(
        self, query, topic, content_mode, link_mode, session_id, max_results
    ) -> List[PageResult]:
        n_results = self.search_cfg.n_results if max_results is None else max(1, int(max_results))
        scfg = self.search_cfg
        urls = search_tavily_urls(
            query,
            n_results,
            scfg.tavily_api_key,
            self.crawler.blacklist,
            search_depth=scfg.tavily_search_depth,
            timelimit=scfg.timelimit,
        )
        log.info("%d URLs from Tavily Search", len(urls))
        for i, u in enumerate(urls, 1):
            log.debug("  %2d. %s", i, u[:90])
        if not urls:
            return []
        return self.crawler.crawl_many(
            urls,
            content=content_mode,
            links=link_mode,
            topic=topic,
            session_id=session_id,
            source="search:tavily",
        )

    # -- Gemini grounded (answer mode) ----------------------------------------

    def _run_gemini(self, query, topic, content_mode) -> List[PageResult]:
        mode = content_mode
        try:
            from lazybridge import Agent, LLMEngine, NativeTool
        except ImportError as e:
            raise RuntimeError("Engine 'gemini' requires LazyBridge.") from e

        try:
            agent = Agent(
                engine=LLMEngine(
                    self.search_cfg.gemini_model,
                    native_tools=[NativeTool.GOOGLE_SEARCH],
                )
            )
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

        # NOTE: this is a SYNTHETIC result, not a navigable source. LazyBridge's
        # Agent layer does not expose Gemini's grounding source URLs, so the
        # answer cannot be audited against citations. ``notes`` flags this so
        # callers/agents do not treat it as a real, fetchable web page.
        url = "gemini://grounded-web-search"
        result = PageResult(
            url=url,
            url_hash=url_hash(url),
            status="done",
            mode="smart" if mode == "smart" else "pure",
            title="Gemini grounded web search",
            text=answer,
            summary=answer[:500],
            depth=0,
            notes="synthetic: grounded answer, no verifiable source URLs",
        )
        # optional persistence
        if self.db is not None:
            sid = self.crawler._default_session_id(topic, mode)
            self.db.create_session(sid, topic=topic, seed=query, mode=mode, source="search:gemini")
            self.db.upsert_page(
                {
                    "url": url,
                    "url_hash": result.url_hash,
                    "status": "done",
                    "mode": result.mode,
                    "title": result.title,
                    "clean_text": answer,
                    "summary": result.summary,
                    "raw_text": answer,
                    "notes": result.notes,
                }
            )
            self.db.add_edge(sid, result.url_hash, depth=0)
        return [result]
