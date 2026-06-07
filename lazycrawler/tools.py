# -*- coding: utf-8 -*-
"""
lazycrawler.tools
=================
LazyCrawler operations exposed as **LazyBridge tools**.

LazyCrawler is the *tool* — you build the agent. These wrappers make the crawler
immediately usable from any LazyBridge agent::

    from lazybridge import Agent, LLMEngine
    from lazycrawler import CrawlerDB, DBConfig, LLMConfig
    from lazycrawler.tools import CrawlerTools

    db = CrawlerDB(DBConfig(db_path="research.db"))
    crawler_tools = CrawlerTools(db=db, llm_cfg=LLMConfig(model="claude-haiku-4-5"))

    agent = Agent(
        engine=LLMEngine("claude-haiku-4-5"),
        tools=crawler_tools.as_tools(),       # <- drop the crawler into YOUR agent
    )
    print(agent("Research the latest on solid-state batteries; cite sources.").text())

``CrawlerTools`` is a LazyBridge ``ToolProvider`` (it implements ``as_tools()``),
mirroring ``lazytools`` connectors. The pure/smart modes are fixed at
construction so the LLM-facing tool schema stays simple (the LLM never has to
reason about cost knobs).

Design notes:
- LazyBridge is imported lazily (only inside ``as_tools()``), so importing this
  module — and the crawler's pure mode — never requires LazyBridge.
- Tools return compact JSON strings (the model parses them). Page text is
  truncated and a ``get_page(url)`` hint is included, so the agent pulls full
  text only when it decides to — keeping token usage low.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from typing import Any, Dict, Optional

from .config import CrawlerConfig, HTTPConfig, LLMConfig
from .crawler import WebCrawler
from .db import CrawlerDB
from .http import url_hash
from .presets import CrawlPreset, resolve_presets
from .search import WebSearch

# Per-page snippet length in tool results (full text via get_page()).
_SNIPPET_CHARS = 500


def _brief(row: Dict[str, Any]) -> Dict[str, Any]:
    """A compact, LLM-friendly view of a page (dict form). Full text via get_page()."""
    text = row.get("summary") or row.get("clean_text") or row.get("text") or ""
    truncated = len(text) > _SNIPPET_CHARS
    return {
        "url": row.get("url"),
        "title": row.get("title"),
        "snippet": (text[:_SNIPPET_CHARS] + " …") if truncated else text,
        "sentiment": row.get("sentiment"),
        "published": row.get("published_iso"),
        "status": row.get("status"),
        "source_url": row.get("source_url"),
        "from_cache": bool(row.get("from_cache")),
        "depth": row.get("depth"),
        "full_text_available": bool(row.get("clean_text") or row.get("text")) and truncated,
    }


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


def _artifact_brief(row: Dict[str, Any]) -> Dict[str, Any]:
    """Compact, LLM-friendly view of one artifact (no raw bytes)."""
    content = row.get("content") or ""
    return {
        "type": row.get("artifact_type"),
        "position": row.get("position"),
        "caption": row.get("caption") or row.get("alt"),
        "summary": row.get("summary"),
        "src_url": row.get("src_url"),
        "content": (content[:800] + " …") if len(content) > 800 else (content or None),
        "data": row.get("data"),
        "mime": row.get("mime"),
        "width": row.get("width"),
        "height": row.get("height"),
        "stored_bytes": bool(row.get("bytes_hash")),
    }


class CrawlerTools:
    """
    Crawler / search / cache operations as LazyBridge tools for an agent.

    Parameters
    ----------
    db : CrawlerDB, optional
        Shared cache + store. Required for ``search_cached`` and ``get_page``;
        also lets ``web_search`` / ``web_crawl`` reuse cached pages (no re-fetch).
    llm_cfg : LLMConfig, optional
        LLM used when content="smart" (structured extraction + sentiment).
    crawler_cfg / http_cfg : optional
        Crawler / HTTP configuration (depth, limits, robots, SSL, ...).
    content : "pure" | "smart"
        How page content is produced (default "smart": title/summary/entities/
        topics/sentiment). "pure" = cheap clean text, no LLM.
    links : "pure" | "smart"
        How links are chosen during multi-page crawls (default "pure").
    topic : str
        Optional topic hint used for smart link selection.
    presets : dict[str, CrawlPreset], optional
        Extra/override named presets the agent can select via ``preset=`` on
        ``web_search`` / ``web_crawl`` (and discover through ``list_presets``).
        Merged on top of the built-in catalog (``lazycrawler.presets``); a key
        matching a built-in name overrides it.
    verbose : bool
        If True, print concise progress messages for interactive use.
    """

    def __init__(
        self,
        db: Optional[CrawlerDB] = None,
        llm_cfg: Optional[LLMConfig] = None,
        crawler_cfg: Optional[CrawlerConfig] = None,
        http_cfg: Optional[HTTPConfig] = None,
        content: str = "smart",
        links: str = "pure",
        topic: str = "",
        presets: Optional[Dict[str, CrawlPreset]] = None,
        verbose: bool = False,
    ):
        self.db = db
        self.content = content
        self.links = links
        self.topic = topic
        self.presets = resolve_presets(presets)
        self.verbose = verbose
        # The agent can pass arbitrary URLs, so turn the SSRF guard ON by default
        # for the tool path (callers wanting internal hosts can pass an explicit
        # HTTPConfig(block_private_addresses=False)).
        http_cfg = self._with_ssrf_guard(http_cfg)
        self._crawler = WebCrawler(crawler_cfg, http_cfg, llm_cfg, db)
        self._search = WebSearch(crawler_cfg=crawler_cfg, http_cfg=http_cfg, llm_cfg=llm_cfg, db=db)

    @staticmethod
    def _with_ssrf_guard(http_cfg: Optional[HTTPConfig]) -> HTTPConfig:
        if http_cfg is None:
            return HTTPConfig(block_private_addresses=True)
        if not http_cfg.block_private_addresses:
            return dataclasses.replace(http_cfg, block_private_addresses=True)
        return http_cfg

    def _say(self, message: str) -> None:
        if self.verbose:
            print(f"[LazyCrawler] {message}", flush=True)

    def _resolve_preset(self, name: str) -> "tuple[Optional[CrawlPreset], Optional[str]]":
        """Look up a preset by name. Returns (preset, error_json). Empty name -> (None, None)."""
        if not name:
            return None, None
        preset = self.presets.get(name)
        if preset is None:
            return None, _dumps(
                {
                    "error": f"unknown preset '{name}'",
                    "available": list(self.presets),
                    "hint": "call list_presets() to see each preset's intent and cost",
                }
            )
        return preset, None

    # -- ToolProvider ---------------------------------------------------------

    def as_tools(self) -> list:
        """
        Return the crawler operations as LazyBridge ``Tool`` objects, ready for
        ``Agent(tools=...)``. (Imports LazyBridge lazily.)
        """
        from lazybridge import Tool

        tools = [
            Tool.wrap(self.list_presets, name="list_presets"),
            Tool.wrap(self.web_search, name="web_search"),
            Tool.wrap(self.web_crawl, name="web_crawl"),
            Tool.wrap(self.get_page, name="get_page"),
        ]
        if self.db is not None:
            tools.append(Tool.wrap(self.search_cached, name="search_cached"))
            tools.append(Tool.wrap(self.get_session_pages, name="get_session_pages"))
            tools.append(Tool.wrap(self.get_artifacts, name="get_artifacts"))
        return tools

    # -- tools (LLM-facing; docstrings are the schema the model sees) ---------

    def web_search(self, query: str, max_results: Optional[int] = None, preset: str = "") -> str:
        """Search the web for a query and return clean, summarized results.

        Use this to find current information on a topic when you don't already
        have a specific URL. It runs a web search, fetches each result, and
        returns a compact list of pages (title, snippet, sentiment, date).
        Snippets are truncated — call ``get_page(url)`` for a page's full text.

        Args:
            query: What to search for, e.g. "EU AI Act enforcement 2026".
            max_results: How many result pages to fetch. Omit to use the preset's
                default (or 15 when no preset).
            preset: Optional named configuration tuned for one intent (e.g.
                "quick_lookup", "deep_research", "news_scan"). Call
                ``list_presets()`` to see the options and their cost. Omit for the
                default behavior.

        Returns:
            A JSON string: {"query", "found", "pages": [{url, title, snippet,
            sentiment, published, status, full_text_available}]}.

        Example:
            web_search("solid-state battery breakthroughs", preset="deep_research")
        """
        p, err = self._resolve_preset(preset)
        if err:
            return err
        content = p.content if p else self.content
        links = p.links if p else self.links
        n = int(max_results) if max_results is not None else (p.max_results if p else 15)
        n = max(1, n)
        overrides = p.crawl_overrides() if p else None
        timelimit = p.timelimit if p else None
        sid = f"search_{uuid.uuid4().hex[:12]}"
        self._say(
            f"search query={query!r} preset={preset or '-'} max_results={n} "
            f"content={content} links={links}"
        )
        out = self._search.run(
            query,
            content=content,
            links=links,
            max_results=n,
            session_id=sid,
            overrides=overrides,
            timelimit=timelimit,
        )
        pages = [_brief(r.model_dump()) for r in out["results"]]
        self._say(f"search done: extracted={out['pages_found']} entries={len(pages)}")
        return _dumps(
            {"query": query, "found": out["pages_found"], "session_id": sid, "pages": pages}
        )

    def web_crawl(self, url: str, depth: Optional[int] = None, preset: str = "") -> str:
        """Crawl a specific URL (and optionally its links) and return clean content.

        Use this when you already have a URL to read. With depth>0 it also
        follows relevant links on the page. Returns a compact list of pages;
        call ``get_page(url)`` for any page's full text.

        Args:
            url: The page to crawl, e.g. "https://example.com/report".
            depth: Link-following depth. 0 = just this page; 1 = also its links.
                Omit to use the preset's depth (or 1 when no preset). Keep small
                to control cost.
            preset: Optional named configuration tuned for one intent (e.g.
                "quick_lookup", "extract_data", "rag_ingest"). Call
                ``list_presets()`` to see the options and their cost. Omit for the
                default behavior. An explicit ``depth`` still overrides the preset.

        Returns:
            A JSON string: {"url", "found", "pages": [{url, title, snippet,
            sentiment, published, status, full_text_available}]}.

        Example:
            web_crawl("https://example.com/report", preset="extract_data")
        """
        p, err = self._resolve_preset(preset)
        if err:
            return err
        content = p.content if p else self.content
        links = p.links if p else self.links
        eff_depth = depth if depth is not None else (p.max_depth if p else 1)
        eff_depth = max(0, int(eff_depth))
        overrides = p.crawl_overrides() if p else None
        sid = f"crawl_{uuid.uuid4().hex[:12]}"
        self._say(
            f"crawl url={url!r} preset={preset or '-'} depth={eff_depth} "
            f"content={content} links={links}"
        )
        # Pass depth/overrides as per-call overrides instead of mutating shared
        # config, so concurrent tool calls never clobber each other.
        results = self._crawler.crawl(
            url,
            content=content,
            links=links,
            topic=self.topic,
            session_id=sid,
            max_depth=eff_depth,
            overrides=overrides,
        )
        pages = [_brief(r.model_dump()) for r in results]
        found = sum(1 for r in results if r.status == "done")
        self._say(f"crawl done: extracted={found} entries={len(pages)}")
        return _dumps({"url": url, "found": found, "session_id": sid, "pages": pages})

    def list_presets(self) -> str:
        """List the named crawl presets you can pass as ``preset=`` to web_search / web_crawl.

        Each preset bundles a ready-made configuration tuned for one intent — how
        page content is extracted (cheap clean text vs LLM summary+sentiment),
        whether links are followed, crawl depth, table/image extraction, Markdown
        output and search recency — together with a coarse ``cost`` hint. Call
        this first when unsure which preset fits, then pass the chosen name. With
        no preset the tools use their default behavior.

        Returns:
            A JSON string: {"presets": [{name, intent, cost, content,
            follows_links, link_mode, depth, artifacts, markdown, recency}]}.

        Example:
            list_presets()   # then: web_search("...", preset="deep_research")
        """
        self._say("list_presets")
        return _dumps({"presets": [p.brief() for p in self.presets.values()]})

    def search_cached(self, query: str, limit: int = 10) -> str:
        """Full-text search over already-crawled pages in the local cache (free).

        Use this FIRST when researching — it searches pages already stored in the
        database with no network calls and no cost. If it returns nothing useful,
        fall back to ``web_search``. Snippets are truncated; use ``get_page(url)``
        for full text.

        Args:
            query: Keywords to search the cache for, e.g. "interest rates".
            limit: Max pages to return (default 10).

        Returns:
            A JSON string: {"query", "found", "pages": [{url, title, snippet,
            sentiment, published, status, full_text_available}]}.

        Example:
            search_cached("lithium supply chain")
        """
        if self.db is None:
            return _dumps({"error": "no database configured; use web_search instead"})
        self._say(f"cache search query={query!r} limit={limit}")
        rows = self.db.search_text(query, limit=limit)
        self._say(f"cache search done: found={len(rows)}")
        return _dumps({"query": query, "found": len(rows), "pages": [_brief(r) for r in rows]})

    def get_page(self, url: str) -> str:
        """Return the FULL stored content of a single already-crawled page.

        Use this after ``web_search`` / ``web_crawl`` / ``search_cached`` to read
        a page's complete text (those return only truncated snippets). Reads from
        the local cache — no network call.

        Args:
            url: The exact page URL to retrieve.

        Returns:
            A JSON string with the full page: {url, title, text, summary,
            entities, topics, sentiment, notes, published, status}. If the page
            isn't cached yet: {"error": ..., "hint": "crawl it first"}.

        Example:
            get_page("https://example.com/report")
        """
        if self.db is None:
            return _dumps({"error": "no database configured; cannot retrieve cached pages"})
        self._say(f"cache get_page url={url!r}")
        row = self.db.get_page(url_hash(url))
        if not row:
            self._say("cache get_page miss")
            return _dumps(
                {
                    "error": f"page not in cache: {url}",
                    "hint": "call web_crawl(url) or web_search(...) first",
                }
            )
        self._say("cache get_page hit")
        return _dumps(
            {
                "url": row.get("url"),
                "title": row.get("title"),
                "text": row.get("clean_text"),
                "summary": row.get("summary"),
                "entities": row.get("entities") or [],
                "topics": row.get("topics") or [],
                "sentiment": row.get("sentiment"),
                "notes": row.get("notes"),
                "published": row.get("published_iso"),
                "status": row.get("status"),
            }
        )

    def get_session_pages(self, session_id: str) -> str:
        """List the pages collected in a previous ``web_search`` / ``web_crawl`` run.

        Each ``web_search`` / ``web_crawl`` result includes a ``session_id``; pass
        it here to get the compact list of pages reached in that run (from the
        local cache, no network call). Snippets are truncated — use
        ``get_page(url)`` for full text.

        Args:
            session_id: The session id returned by web_search / web_crawl.

        Returns:
            A JSON string: {"session_id", "found", "pages": [{url, title, snippet,
            sentiment, published, status, source_url, from_cache, depth,
            full_text_available}]}.
        """
        if self.db is None:
            return _dumps({"error": "no database configured; cannot list session pages"})
        self._say(f"session pages session_id={session_id!r}")
        rows = self.db.get_pages(session_id=session_id, status="done")
        self._say(f"session pages done: found={len(rows)}")
        return _dumps(
            {"session_id": session_id, "found": len(rows), "pages": [_brief(r) for r in rows]}
        )

    def get_artifacts(self, url: str, artifact_type: str = "") -> str:
        """Return the non-textual artifacts extracted from an already-crawled page.

        Use this to inspect the tables, images, figures and charts found on a page
        (after web_crawl/web_search with artifact extraction enabled). Tables come
        as Markdown + structured rows; images/charts as URL + caption (+ a vision
        summary when enrichment is on). Reads the local cache — no network call.

        Args:
            url: The exact page URL whose artifacts to retrieve.
            artifact_type: Optional filter — "table", "image", "figure", "chart"
                or "svg". Empty returns all types.

        Returns:
            A JSON string: {"url", "found", "artifacts": [{type, caption, summary,
            src_url, content, data, mime, width, height, stored_bytes}]}.
        """
        if self.db is None:
            return _dumps({"error": "no database configured; cannot retrieve artifacts"})
        rows = self.db.get_artifacts(url_hash=url_hash(url), artifact_type=(artifact_type or None))
        arts = [_artifact_brief(r) for r in rows]
        return _dumps({"url": url, "found": len(arts), "artifacts": arts})

    def close(self) -> None:
        self._crawler.close()
        self._search.close()

    def __enter__(self) -> "CrawlerTools":
        return self

    def __exit__(self, *exc) -> bool:
        self.close()
        return False
