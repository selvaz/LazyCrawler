# -*- coding: utf-8 -*-
"""
lazycrawler.crawler
===================
WebCrawler: recursive crawl with two INDEPENDENT LLM knobs and a native parallel
mode.

LLM knobs (toggled separately):
  - content : "pure" (trafilatura/regex) | "smart" (LLM structured extraction)
  - links   : "pure" (heuristic first-N)  | "smart" (LLM relevance ranking)

``mode`` sets both; ``content=`` / ``links=`` override either:
  crawl(url, mode="smart")                      # content=smart, links=smart
  crawl(url, content="smart", links="pure")     # LLM summary, heuristic links
  crawl(url, content="pure",  links="smart")    # no summary, LLM picks links

Custom output schema (smart content):
  crawl(url, content="smart", schema=MyPydanticModel)
  -> PageResult.data holds the full structured object; known fields
     (title/summary/clean_text/entities/topics) are mapped when present.

Parallel mode:
  CrawlerConfig(max_workers=N) with N>1 -> bounded thread pool, level-by-level
  BFS, thread-safe shared state, thread-local HTTP/LLM resources. N=1 keeps the
  original sequential DFS. (link_delay is not applied in parallel mode.)

JavaScript rendering:
  HTTPConfig(render_js=True) routes fetches through a headless browser.

Output:
  - always: List[PageResult]
  - optional: persistence to CrawlerDB (3-level dedup, TTL cache, FTS5)
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, List, Literal, Optional, Set, Tuple

from pydantic import BaseModel, Field

from ._log import log
from .artifacts import (
    Artifact,
    bytes_sha256,
    extract_html_artifacts,
    extract_html_artifacts_anchored,
    sniff_image,
)
from .config import CrawlerConfig, HTTPConfig, LLMConfig
from .db import CrawlerDB
from .http import (
    HTTPClient,
    RobotsChecker,
    compile_exclude,
    get_base_domain,
    is_blacklisted_domain,
    is_blocked_address,
    load_blacklist_from_excel,
    normalize_url,
)
from .http import (
    content_hash as _content_hash,
)
from .http import (
    url_hash as _url_hash,
)
from .pdf import extract_pdf, extract_pdf_bytes, looks_like_pdf, title_from_pdf_text, title_from_url
from .ratelimit import HostRateLimiter
from .text import (
    extract_candidate_links,
    extract_canonical_url,
    extract_page_title,
    extract_published_datetime,
    preprocess_text,
)

Mode = Literal["pure", "smart"]
Status = Literal["done", "fetch_error", "no_text", "llm_error", "blacklisted", "robots_blocked"]


# =============================================================================
# OUTPUT MODEL
# =============================================================================


class PageResult(BaseModel):
    """Result of crawling a single page."""

    url: str
    url_hash: str = ""
    status: Status = "done"
    mode: Mode = "pure"  # content mode that produced this result
    title: Optional[str] = None
    text: Optional[str] = None  # clean text (pure: cleaned; smart: LLM clean_text)
    summary: Optional[str] = None  # smart content only
    entities: List[str] = Field(default_factory=list)
    topics: List[str] = Field(default_factory=list)
    sentiment: Optional[str] = None  # smart: negative|neutral|positive
    notes: Optional[str] = None  # smart: reserved research tags/notes
    data: Optional[dict] = None  # full structured object (custom schema)
    published_iso: Optional[str] = None
    is_pdf: bool = False
    depth: int = 0
    source_url: Optional[str] = None
    error: Optional[str] = None
    from_cache: bool = False
    markdown: Optional[str] = None  # optional HTML->Markdown render (emit_markdown)
    artifacts: List[Artifact] = Field(default_factory=list)  # tables/images/charts


# =============================================================================
# PER-RUN STATE  +  PER-WORKER RESOURCES
# =============================================================================


@dataclass
class _State:
    content_mode: Mode
    link_mode: Mode
    topic: str
    session_id: Optional[str]
    schema: Optional[type] = None
    max_depth: int = 0  # effective depth for this run (cfg or override)
    link_selector: Any = None  # sequential link-selection agent
    visited: Set[str] = field(default_factory=set)
    results: List[PageResult] = field(default_factory=list)
    pages_done: int = 0
    lock: Any = field(default_factory=threading.Lock)


@dataclass
class _Res:
    """Resources used to process a page (shared in sequential, per-thread in parallel)."""

    http: HTTPClient
    llm: Any = None
    link_selector: Any = None


# =============================================================================
# WEB CRAWLER
# =============================================================================


class WebCrawler:
    """
    Recursive crawler with independent content/link LLM modes, optional DB
    persistence, native parallel mode, and optional JS rendering.
    """

    def __init__(
        self,
        crawler_cfg: Optional[CrawlerConfig] = None,
        http_cfg: Optional[HTTPConfig] = None,
        llm_cfg: Optional[LLMConfig] = None,
        db: Optional[CrawlerDB] = None,
    ):
        self.cfg = crawler_cfg or CrawlerConfig()
        self.http_cfg = http_cfg or HTTPConfig()
        self.llm_cfg = llm_cfg
        self.db = db

        self.blacklist = list(self.cfg.blacklist)
        if self.cfg.blacklist_excel:
            self.blacklist += load_blacklist_from_excel(
                self.cfg.blacklist_excel,
                self.cfg.blacklist_excel_sheet,
                self.cfg.blacklist_excel_column,
            )

        self._http = HTTPClient(self.http_cfg)  # shared client for sequential mode
        self._llm = None  # lazy CrawlerLLM for sequential mode
        self._tls = threading.local()  # per-thread resources (parallel)
        self._created_res: List[_Res] = []  # thread-local resources to close
        # robots.txt gate (shared, thread-safe, own HTTP client honoring verify)
        self._robots = (
            RobotsChecker(HTTPClient(self.http_cfg), self.http_cfg.user_agent)
            if self.cfg.respect_robots
            else None
        )
        # compiled link-exclusion regex (configurable) and per-host rate limiter
        self._exclude_re = compile_exclude(self.cfg.exclude_patterns)
        self._rate = HostRateLimiter(self.http_cfg.per_host_delay, self._robots)

    # -- public API -----------------------------------------------------------

    def crawl(
        self,
        url: str,
        *,
        mode: Mode = "pure",
        content: Optional[Mode] = None,
        links: Optional[Mode] = None,
        topic: str = "",
        schema: Optional[type] = None,
        session_id: Optional[str] = None,
        max_depth: Optional[int] = None,
    ) -> List[PageResult]:
        """Crawl a single URL (and its links up to max_depth).

        ``max_depth`` overrides ``CrawlerConfig.max_depth`` for this call only,
        without mutating shared config (safe for concurrent calls).
        """
        return self.crawl_many(
            [url],
            mode=mode,
            content=content,
            links=links,
            topic=topic,
            schema=schema,
            session_id=session_id,
            max_depth=max_depth,
        )

    def crawl_many(
        self,
        urls: List[str],
        *,
        mode: Mode = "pure",
        content: Optional[Mode] = None,
        links: Optional[Mode] = None,
        topic: str = "",
        schema: Optional[type] = None,
        session_id: Optional[str] = None,
        source: str = "crawl",
        max_depth: Optional[int] = None,
    ) -> List[PageResult]:
        """Crawl a list of URLs sharing state (visited set, page counter).

        ``max_depth`` overrides ``CrawlerConfig.max_depth`` for this call only.
        """
        content_mode: Mode = content or mode
        link_mode: Mode = links or mode
        eff_depth = self.cfg.max_depth if max_depth is None else max(0, int(max_depth))
        st = _State(
            content_mode=content_mode,
            link_mode=link_mode,
            topic=topic,
            session_id=session_id,
            schema=schema,
            max_depth=eff_depth,
        )

        if self.db is not None:
            st.session_id = session_id or self._default_session_id(topic, content_mode)
            self.db.create_session(
                st.session_id,
                topic=topic,
                seed=urls[0] if urls else "",
                mode=content_mode,
                source=source,
            )

        seeds = [
            (u, get_base_domain(u)) for u in urls if not is_blacklisted_domain(u, self.blacklist)
        ]

        log.info(
            "crawl: content=%s links=%s workers=%d depth=%d max_pages=%d robots=%s strict=%s",
            content_mode,
            link_mode,
            self.cfg.max_workers,
            eff_depth,
            self.cfg.max_pages,
            self.cfg.respect_robots,
            self.cfg.strict,
        )

        log.debug("seeds: %d URL(s), start_domain(s): %s", len(seeds), [d for _, d in seeds])

        if self.cfg.max_workers > 1:
            self._crawl_parallel(st, seeds)
        else:
            res = self._sequential_res(st)
            for i, (url, dom) in enumerate(seeds):
                if self._reached_cap(st):
                    break
                if i > 0 and self.http_cfg.link_delay:
                    time.sleep(self.http_cfg.link_delay)
                try:
                    self._crawl_seq(st, url, 0, None, dom, res)
                except Exception:
                    if self.cfg.strict:
                        raise
                    log.exception("error crawling seed %s", url[:80])

        log.info("crawl done: %d pages collected", len(st.results))
        return st.results

    # -- resource construction ------------------------------------------------

    def _sequential_res(self, st: _State) -> _Res:
        """Shared resources for sequential mode (reuses self._http / self._llm)."""
        selector = None
        if st.content_mode == "smart" or st.link_mode == "smart":
            self._ensure_llm()
            if st.link_mode == "smart":
                selector = self._llm.build_link_selector(
                    st.topic or "general relevant content", self.cfg.max_links_per_level
                )
        st.link_selector = selector
        return _Res(self._http, self._llm, selector)

    def _build_res(self, st: _State) -> _Res:
        """Fresh resources for a parallel worker (own HTTP client + LLM agents)."""
        http = HTTPClient(self.http_cfg)
        llm = None
        selector = None
        if st.content_mode == "smart" or st.link_mode == "smart":
            from .llm import CrawlerLLM

            llm = CrawlerLLM(self.llm_cfg or LLMConfig())
            if st.link_mode == "smart":
                selector = llm.build_link_selector(
                    st.topic or "general relevant content", self.cfg.max_links_per_level
                )
        return _Res(http, llm, selector)

    def _worker_res(self, st: _State) -> _Res:
        r = getattr(self._tls, "res", None)
        if r is None:
            r = self._build_res(st)
            self._tls.res = r
            with st.lock:
                self._created_res.append(r)
        return r

    # -- drivers --------------------------------------------------------------

    def _crawl_seq(self, st, url, depth, source_url, start_domain, res) -> None:
        """Sequential depth-first driver."""
        links = self._process_one(st, url, depth, source_url, start_domain, res)
        if depth >= st.max_depth:
            return
        for _, link_url in links:
            if self._reached_cap(st):
                break
            if self.http_cfg.link_delay:
                time.sleep(self.http_cfg.link_delay)
            try:
                self._crawl_seq(st, link_url, depth + 1, url, start_domain, res)
            except Exception:
                if self.cfg.strict:
                    raise
                log.exception("error crawling %s", link_url[:70])

    def _crawl_parallel(self, st, seeds) -> None:
        """Native parallel driver: level-by-level BFS over a bounded thread pool."""
        self._tls = threading.local()
        self._created_res = []
        frontier = [(url, dom, None) for (url, dom) in seeds]  # (url, start_domain, source_url)
        depth = 0
        try:
            with ThreadPoolExecutor(max_workers=self.cfg.max_workers) as pool:
                while frontier and not self._reached_cap(st):
                    fut_map = {
                        pool.submit(self._worker_process, st, url, depth, src, dom): (url, dom)
                        for (url, dom, src) in frontier
                    }
                    next_frontier: List[Tuple[str, str, str]] = []
                    seen_next: Set[str] = set()
                    for fut in as_completed(fut_map):
                        parent_url, parent_dom = fut_map[fut]
                        try:
                            links = fut.result() or []
                        except Exception:
                            if self.cfg.strict:
                                raise
                            log.exception("parallel worker error on %s", parent_url[:70])
                            links = []
                        for _, link_url in links:
                            nu = normalize_url(link_url)
                            if nu in seen_next:
                                continue
                            seen_next.add(nu)
                            next_frontier.append((link_url, parent_dom, parent_url))
                    depth += 1
                    if depth > st.max_depth:
                        break
                    frontier = next_frontier
        finally:
            for r in self._created_res:
                try:
                    r.http.close()
                except Exception:
                    log.debug("failed closing a worker HTTP client", exc_info=True)

    def _worker_process(self, st, url, depth, source_url, start_domain):
        return self._process_one(st, url, depth, source_url, start_domain, self._worker_res(st))

    # -- unified per-page processing ------------------------------------------

    def _process_one(self, st, url, depth, source_url, start_domain, res) -> List[Tuple[str, str]]:
        """
        Process one URL: cache/fetch/extract/emit. Returns the (already selected)
        links to follow next — the driver handles traversal. [] = nothing to follow.
        """
        cfg = self.cfg
        if self._reached_cap(st):
            return []
        url = normalize_url(url)
        if is_blacklisted_domain(url, self.blacklist):
            return []
        if self.http_cfg.block_private_addresses and is_blocked_address(url):
            log.info("SSRF guard: blocking private/loopback address %s", url)
            self._emit(
                st,
                PageResult(
                    url=url,
                    url_hash=_url_hash(url),
                    status="fetch_error",
                    mode=st.content_mode,
                    depth=depth,
                    source_url=source_url,
                    error="Blocked private/loopback address (SSRF guard)",
                ),
                count=False,
            )
            return []
        if not self._mark_visited(st, url):
            return []
        uh = _url_hash(url)
        with st.lock:
            _page_num = st.pages_done + 1
        log.info("[d%d | p%d/%d] %s", depth, _page_num, self.cfg.max_pages, url[:90])

        # robots.txt gate (enabled by default; CrawlerConfig.respect_robots=False to disable)
        if self._robots is not None and not self._robots.allowed(url):
            log.info("robots.txt disallows %s - skipping", url)
            self._emit(
                st,
                PageResult(
                    url=url,
                    url_hash=uh,
                    status="robots_blocked",
                    mode=st.content_mode,
                    depth=depth,
                    source_url=source_url,
                    error="Disallowed by robots.txt",
                ),
                count=False,
            )
            return []

        # DEDUP level 1: fresh URL cache (content-mode-aware)
        cached = self._try_cache(st, url, uh, depth, source_url, res)
        if cached is not None:
            return cached

        # FETCH (rate-limited per host; robots Crawl-delay honored on top)
        self._rate.wait(url)
        fr = res.http.fetch(url)
        html, raw_text, status_code, pdf_bytes = fr.html, fr.text, fr.status, fr.content
        log.debug(
            "  fetch: HTTP %s | html=%d chars | text=%d chars | pdf_bytes=%d",
            status_code or "ERR",
            len(html or ""),
            len(raw_text or "") if raw_text else 0,
            len(pdf_bytes or b""),
        )
        if not html and not (raw_text or "").strip() and not pdf_bytes:
            log.debug("  -> fetch_error: no HTML/text/bytes returned")
            self._emit(
                st,
                PageResult(
                    url=url,
                    url_hash=uh,
                    status="fetch_error",
                    mode=st.content_mode,
                    depth=depth,
                    source_url=source_url,
                    error=f"Fetch failed (status={status_code})",
                ),
                count=False,
            )
            return []

        # PDF vs HTML
        is_pdf = bool(pdf_bytes) or looks_like_pdf(url, html or "", raw_text or "")
        if is_pdf:
            log.debug("  detected as PDF")
        published_iso: Optional[str] = None
        pdf_title = ""
        if is_pdf:
            if pdf_bytes:
                # bytes already downloaded by HTTPClient -> no second download
                pdf_text, pdf_title, pdf_pub = extract_pdf_bytes(pdf_bytes)
            else:
                # rare: detected via magic bytes in text (e.g. JS-render path)
                pdf_text, pdf_title, pdf_pub = extract_pdf(
                    url,
                    timeout=self.http_cfg.pdf_timeout,
                    user_agent=self.http_cfg.user_agent,
                    verify=(self.http_cfg.ca_bundle or self.http_cfg.verify_ssl),
                )
            if pdf_pub:
                published_iso = pdf_pub
            if pdf_text.strip():
                raw_text = pdf_text
                html = ""
        else:
            canonical = extract_canonical_url(html, url)
            if canonical:
                cnorm = normalize_url(canonical)
                if is_blacklisted_domain(cnorm, self.blacklist):
                    return []
                if cnorm != url and self._mark_visited(st, cnorm):
                    url = cnorm
                    uh = _url_hash(url)
            published_iso = extract_published_datetime(html, url)

        # candidate links
        candidates = self._extract_candidates(st, html, url, start_domain, depth, is_pdf)

        # no text
        if not (raw_text or "").strip():
            log.debug("  -> no_text: trafilatura/fallback returned nothing")
            self._emit(
                st,
                PageResult(
                    url=url,
                    url_hash=uh,
                    status="no_text",
                    mode=st.content_mode,
                    depth=depth,
                    source_url=source_url,
                    published_iso=published_iso,
                    is_pdf=is_pdf,
                    error="No extractable text",
                ),
                count=False,
                candidate_links=candidates,
            )
            return self._select_next(st, candidates, "", res)

        preclean = preprocess_text(raw_text)
        title = (
            (pdf_title or title_from_pdf_text(preclean) or title_from_url(url))
            if is_pdf
            else extract_page_title(html)
        )
        log.debug("  title: %r", (title or "")[:80])

        # DEDUP level 2/3: content_hash
        chash = _content_hash(raw_text)
        if self.db is not None:
            existing = self.db.find_by_content_hash(chash)
            if existing and self._can_reuse(existing, st.content_mode):
                # create the page row BEFORE the edge (crawl_edges has an FK on pages)
                if existing.get("url_hash") != uh:
                    self._copy_content(existing, url, uh, candidates)
                self.db.add_edge(st.session_id, uh, source_url=source_url, depth=depth)
                reused = self.db.get_page(uh) or existing
                result = self._result_from_row(reused, depth, source_url, from_cache=True)
                self._add_counted(st, result)
                log.debug("  content-hash dedup - reused stored content, skipped extraction")
                return self._select_next(st, candidates, reused.get("clean_text") or "", res)

        # content extraction
        if st.content_mode == "pure":
            log.debug(
                "  content [pure]: %d chars (preclean=%d, limit=%d)",
                min(len(preclean), cfg.max_chars_pure),
                len(preclean),
                cfg.max_chars_pure,
            )
            result = PageResult(
                url=url,
                url_hash=uh,
                status="done",
                mode="pure",
                title=title,
                text=preclean[: cfg.max_chars_pure],
                published_iso=published_iso,
                is_pdf=is_pdf,
                depth=depth,
                source_url=source_url,
            )
        else:
            log.debug("  content [smart]: LLM extraction (preclean=%d chars)...", len(preclean))
            result = self._smart_extract(
                st, url, uh, preclean, title, published_iso, is_pdf, depth, source_url, res
            )

        # optional artifacts (tables / images / charts) — HTML and PDF.
        # (Run before Markdown so anchoring can replace artifacts with placeholders.)
        anchored_html: Optional[str] = None
        if cfg.extract_artifacts:
            result.artifacts, anchored_html = self._collect_artifacts(
                st, html, url, pdf_bytes, is_pdf, res
            )

        # optional Markdown render (RAG); HTML-only, skip PDFs
        if cfg.emit_markdown and html and not is_pdf:
            from .markdown import html_to_markdown

            md = html_to_markdown(anchored_html or html, url)
            result.markdown = (md[: cfg.max_chars_pure] if md else None) or None

        self._emit(
            st,
            result,
            count=(result.status == "done"),
            raw_text=raw_text,
            content_hash=chash,
            candidate_links=candidates,
        )
        if self.db is not None and result.artifacts:
            self.db.add_artifacts(result.url_hash, result.artifacts)
        return self._select_next(st, candidates, preclean, res)

    def _extract_candidates(
        self, st, html, url, start_domain, depth, is_pdf
    ) -> List[Tuple[str, str]]:
        """Extract page links and filter out visited/blacklisted ones."""
        cfg = self.cfg
        if depth >= st.max_depth:
            log.debug("  links: skipped (at max_depth=%d)", st.max_depth)
            return []
        if is_pdf or not html:
            if is_pdf:
                log.debug("  links: skipped (PDF)")
            return []
        candidates = extract_candidate_links(
            html,
            url,
            start_domain,
            same_domain_only=cfg.same_domain_only,
            max_links=cfg.max_candidate_links,
            exclude_pattern=self._exclude_re,
        )
        return self._filter_candidates(st, candidates)

    def _filter_candidates(self, st, candidates) -> List[Tuple[str, str]]:
        """Drop already-visited and blacklisted links."""
        before = len(candidates)
        with st.lock:
            visited_snapshot = set(st.visited)
        filtered = [
            (t, u)
            for (t, u) in candidates
            if normalize_url(u) not in visited_snapshot
            and not is_blacklisted_domain(u, self.blacklist)
        ]
        if before:
            log.debug(
                "  candidates: %d -> -%d visited/blacklisted -> %d to explore",
                before,
                before - len(filtered),
                len(filtered),
            )
        return filtered

    # -- cache ----------------------------------------------------------------

    def _try_cache(self, st, url, uh, depth, source_url, res) -> Optional[List[Tuple[str, str]]]:
        """
        Returns None on cache miss (caller proceeds to fetch), or a links list
        (handled). Cached hits are terminal -> []. Enrich (pure->smart) re-runs
        the LLM on stored text, no re-fetch -> [].
        """
        if self.db is None:
            return None
        row = self.db.get_fresh_page(url)
        if not row:
            return None

        if self._satisfies(row, st.content_mode):
            log.debug("  cache hit (fresh, content=%s) - skipping fetch", st.content_mode)
            result = self._result_from_row(row, depth, source_url, from_cache=True)
            self._add_counted(st, result)
            if self.db:
                self.db.add_edge(st.session_id, uh, source_url=source_url, depth=depth)
            # Optionally keep recursing from the links stored at crawl time, so a
            # warm cache yields the same frontier as a cold one (no re-fetch).
            if self.cfg.recurse_from_cache and depth < st.max_depth:
                stored = [(a, u) for a, u in (row.get("links") or []) if u]
                if stored:
                    cands = self._filter_candidates(st, stored)
                    log.debug(
                        "  cache recurse: %d stored link(s) -> %d to follow",
                        len(stored),
                        len(cands),
                    )
                    return self._select_next(st, cands, row.get("clean_text") or "", res)
            return []

        if st.content_mode == "smart":
            base = row.get("raw_text") or row.get("clean_text") or ""
            if base.strip():
                log.debug("  cache enrich (pure->smart) - no fetch, LLM only")
                preclean = preprocess_text(base)
                result = self._smart_extract(
                    st,
                    url,
                    uh,
                    preclean,
                    row.get("title") or "",
                    row.get("published_iso"),
                    bool(row.get("is_pdf")),
                    depth,
                    source_url,
                    res,
                )
                self._emit(
                    st,
                    result,
                    count=(result.status == "done"),
                    raw_text=row.get("raw_text") or base,
                    content_hash=row.get("content_hash") or _content_hash(base),
                )
                return []
        return None

    @staticmethod
    def _satisfies(row: dict, content_mode: str) -> bool:
        if content_mode == "pure":
            return bool(row.get("clean_text"))
        return row.get("mode") == "smart"

    @staticmethod
    def _can_reuse(existing: dict, content_mode: str) -> bool:
        if existing.get("status") != "done":
            return False
        if content_mode == "pure":
            return True
        return existing.get("mode") == "smart"

    # -- smart content extraction ---------------------------------------------

    def _smart_extract(
        self, st, url, uh, preclean, title, published_iso, is_pdf, depth, source_url, res
    ) -> PageResult:
        cfg = self.cfg
        content_text = preclean[: cfg.max_chars_content]
        if len(preclean) > cfg.large_doc_threshold:
            _n_chunks = min(
                len(preclean) // cfg.large_doc_chunk_chars + 1, cfg.large_doc_max_chunks
            )
            log.debug(
                "  large-doc: %d chars > threshold=%d -> LLM map-reduce (%d chunks ~%d chars ea)",
                len(preclean),
                cfg.large_doc_threshold,
                _n_chunks,
                cfg.large_doc_chunk_chars,
            )
            content_text = res.llm.summarize_large(
                url,
                preclean,
                max_chars_out=cfg.max_chars_content,
                threshold=cfg.large_doc_threshold,
                chunk_chars=cfg.large_doc_chunk_chars,
                max_chunks=cfg.large_doc_max_chunks,
            )
            log.debug("  large-doc: summarized to %d chars", len(content_text))

        extract = res.llm.extract_content(url, content_text, schema=st.schema)
        if extract is None:
            log.debug("  content [smart]: LLM returned None (llm_error)")
            return PageResult(
                url=url,
                url_hash=uh,
                status="llm_error",
                mode="smart",
                title=title,
                published_iso=published_iso,
                is_pdf=is_pdf,
                depth=depth,
                source_url=source_url,
                error="LLM extraction failed",
            )

        data = extract.model_dump()
        text = getattr(extract, "clean_text", None) or None
        if st.schema is not None and not text:
            text = json.dumps(data, ensure_ascii=False)  # keep custom data searchable
        _title_out = getattr(extract, "title", None) or title
        _summary_out = getattr(extract, "summary", None) or ""
        _entities_out = list(getattr(extract, "entities", None) or [])
        _topics_out = list(getattr(extract, "topics", None) or [])
        _sentiment_out = getattr(extract, "sentiment", None)
        log.debug(
            "  content [smart]: title=%r | summary=%d chars | %d entities | %d topics | sentiment=%s",
            (_title_out or "")[:60],
            len(_summary_out),
            len(_entities_out),
            len(_topics_out),
            _sentiment_out or "?",
        )
        return PageResult(
            url=url,
            url_hash=uh,
            status="done",
            mode="smart",
            title=_title_out,
            text=text,
            summary=_summary_out or None,
            entities=_entities_out,
            topics=_topics_out,
            sentiment=_sentiment_out,
            notes=getattr(extract, "notes", None) or None,
            data=data,
            published_iso=published_iso,
            is_pdf=is_pdf,
            depth=depth,
            source_url=source_url,
        )

    # -- artifacts ------------------------------------------------------------

    def _collect_artifacts(
        self, st, html, url, pdf_bytes, is_pdf, res
    ) -> "Tuple[List[Artifact], Optional[str]]":
        """
        Extract artifacts (HTML or PDF), then download bytes / enrich as configured.
        Returns ``(artifacts, anchored_html)`` — ``anchored_html`` is the HTML with
        each artifact replaced by a ``[[artifact:<hash>]]`` placeholder when Markdown
        anchoring is enabled, else None.
        """
        cfg = self.cfg
        want = set(cfg.artifact_types or ())
        if not want:
            return [], None
        arts: List[Artifact] = []
        anchored_html: Optional[str] = None
        anchor = bool(cfg.emit_markdown and cfg.markdown_artifact_anchors and html and not is_pdf)
        try:
            if is_pdf and pdf_bytes:
                from .pdf import extract_pdf_artifacts

                for d in extract_pdf_artifacts(
                    pdf_bytes,
                    want=want,
                    max_artifacts=cfg.max_artifacts_per_page,
                    min_image_dim=cfg.min_image_dim,
                ):
                    arts.append(Artifact(**d))
            elif html:
                opts = dict(
                    types=want,
                    min_image_dim=cfg.min_image_dim,
                    context_chars=cfg.artifact_context_chars,
                    max_artifacts=cfg.max_artifacts_per_page,
                    same_domain_images=cfg.same_domain_images,
                )
                if anchor:
                    arts, anchored_html = extract_html_artifacts_anchored(html, url, **opts)
                else:
                    arts = extract_html_artifacts(html, url, **opts)
        except Exception:
            if cfg.strict:
                raise
            log.exception("artifact extraction failed for %s", url[:80])
            return [], None
        return self._post_process_artifacts(st, arts, res), anchored_html

    def _post_process_artifacts(self, st, arts: List[Artifact], res) -> List[Artifact]:
        cfg = self.cfg
        # download image/chart bytes (HTML images only have a src_url at this point)
        if cfg.download_artifact_bytes:
            for a in arts:
                if a.blob is None and a.src_url and a.artifact_type in ("image", "chart"):
                    self._rate.wait(a.src_url)
                    body, ctype, _ = res.http.fetch_bytes(a.src_url)
                    if body:
                        mime, w, h = sniff_image(body, ctype)
                        a.mime = a.mime or mime
                        a.width = a.width or w
                        a.height = a.height or h
                        a.size_bytes = len(body)
                        a.bytes_hash = bytes_sha256(body)
                        if len(body) <= cfg.max_artifact_bytes:
                            a.blob = body
        # hash any blob (e.g. PDF-embedded images) + finalize the dedup key
        for a in arts:
            if a.blob is not None and not a.bytes_hash:
                a.bytes_hash = bytes_sha256(a.blob)
                a.size_bytes = a.size_bytes or len(a.blob)
            a.ensure_content_hash()
        # optional vision/LLM enrichment (smart mode only, capped)
        if cfg.enrich_artifacts and st.content_mode == "smart" and res.llm is not None:
            for a in arts[: cfg.max_artifacts_to_enrich]:
                res.llm.enrich_artifact(a)
        return arts

    # -- link selection -------------------------------------------------------

    def _select_next(self, st, candidates, excerpt, res) -> List[Tuple[str, str]]:
        cfg = self.cfg
        if not candidates:
            log.debug("  next: no candidates -> nothing queued")
            return []
        if st.link_mode == "smart" and res.link_selector is not None:
            log.debug(
                "  next: LLM link selection from %d candidates (topic=%r)...",
                len(candidates),
                (st.topic or "")[:50],
            )
            selected = res.llm.select_links(
                res.link_selector, excerpt, candidates, cfg.max_links_per_level
            )
            log.debug("  next: LLM selected %d link(s):", len(selected))
            for i, (anchor, link_url) in enumerate(selected[:5]):
                log.debug("    [%d] %s -> %s", i + 1, (anchor or "")[:50], link_url[:80])
            if len(selected) > 5:
                log.debug("    ... and %d more", len(selected) - 5)
        else:
            selected = candidates[: cfg.max_links_per_level]
            log.debug(
                "  next: heuristic (first %d of %d candidates) -> %d queued",
                cfg.max_links_per_level,
                len(candidates),
                len(selected),
            )
            if log.isEnabledFor(10):  # DEBUG = 10
                for i, (anchor, link_url) in enumerate(selected[:5]):
                    log.debug("    [%d] %s -> %s", i + 1, (anchor or "")[:50], link_url[:80])
                if len(selected) > 5:
                    log.debug("    ... and %d more", len(selected) - 5)
        after_bl = [(a, u) for (a, u) in selected if not is_blacklisted_domain(u, self.blacklist)]
        if len(after_bl) < len(selected):
            log.debug(
                "  next: -%d blacklisted -> %d final", len(selected) - len(after_bl), len(after_bl)
            )
        return after_bl

    # -- thread-safe state + persistence --------------------------------------

    def _mark_visited(self, st, url) -> bool:
        with st.lock:
            if url in st.visited:
                return False
            st.visited.add(url)
            return True

    def _reached_cap(self, st) -> bool:
        with st.lock:
            return st.pages_done >= self.cfg.max_pages

    def _add_counted(self, st, result: PageResult) -> None:
        with st.lock:
            st.results.append(result)
            st.pages_done += 1

    def _emit(
        self,
        st,
        result: PageResult,
        *,
        count: bool,
        raw_text: Optional[str] = None,
        content_hash: Optional[str] = None,
        candidate_links: Optional[List[Tuple[str, str]]] = None,
    ) -> None:
        with st.lock:
            st.results.append(result)
            if count:
                st.pages_done += 1
        if self.db is None:
            return
        self.db.upsert_page(
            {
                "url": result.url,
                "url_hash": result.url_hash,
                "domain": get_base_domain(result.url),
                "is_pdf": result.is_pdf,
                "status": result.status,
                "mode": result.mode,
                "error": result.error,
                "raw_text": raw_text,
                "clean_text": result.text,
                "title": result.title,
                "summary": result.summary,
                "entities": result.entities,
                "topics": result.topics,
                "sentiment": result.sentiment,
                "notes": result.notes,
                "data": result.data,
                "markdown": result.markdown,
                "published_iso": result.published_iso,
                "content_hash": content_hash,
                "links": [[a, u] for (a, u) in (candidate_links or [])] or None,
            }
        )
        if st.session_id:
            self.db.add_edge(
                st.session_id, result.url_hash, source_url=result.source_url, depth=result.depth
            )

    def _copy_content(
        self,
        existing: dict,
        url: str,
        uh: str,
        candidate_links: Optional[List[Tuple[str, str]]] = None,
    ) -> None:
        page = dict(existing)
        page.update({"url": url, "url_hash": uh})
        page.pop("entities", None)
        page.pop("topics", None)
        page.pop("data", None)
        # store this URL's own candidate links (not the source row's)
        page.pop("links", None)
        page["links_json"] = (
            json.dumps([[a, u] for (a, u) in candidate_links], ensure_ascii=False)
            if candidate_links
            else None
        )
        self.db.upsert_page(page)

    def _load_artifacts(self, url_hash: str) -> List[Artifact]:
        """Reconstruct a cached page's artifacts from the DB (blob omitted)."""
        if self.db is None or not self.cfg.extract_artifacts or not url_hash:
            return []
        try:
            return [Artifact(**a) for a in self.db.get_artifacts(url_hash=url_hash)]
        except Exception:
            log.debug("failed loading cached artifacts for %s", url_hash, exc_info=True)
            return []

    def _result_from_row(
        self, row: dict, depth: int, source_url: Optional[str], from_cache: bool
    ) -> PageResult:
        return PageResult(
            artifacts=self._load_artifacts(row.get("url_hash", "")),
            url=row.get("url", ""),
            url_hash=row.get("url_hash", ""),
            status=row.get("status", "done"),
            mode=row.get("mode", "pure"),
            title=row.get("title"),
            text=row.get("clean_text"),
            summary=row.get("summary"),
            entities=row.get("entities") or [],
            topics=row.get("topics") or [],
            sentiment=row.get("sentiment"),
            notes=row.get("notes"),
            data=row.get("data"),
            published_iso=row.get("published_iso"),
            is_pdf=bool(row.get("is_pdf")),
            depth=depth,
            source_url=source_url,
            error=row.get("error"),
            from_cache=from_cache,
            markdown=row.get("markdown"),
        )

    # -- helpers --------------------------------------------------------------

    def _ensure_llm(self) -> None:
        if self._llm is None:
            from .llm import CrawlerLLM

            self._llm = CrawlerLLM(self.llm_cfg or LLMConfig())

    @staticmethod
    def _default_session_id(topic: str, content_mode: str) -> str:
        # microseconds + a short random suffix so two runs in the same second
        # (e.g. rapid tool calls) never collide.
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        rand = uuid.uuid4().hex[:6]
        slug = re.sub(r"[^a-z0-9]+", "-", (topic or "crawl").lower()).strip("-")[:32] or "crawl"
        return f"{slug}_{content_mode}_{ts}_{rand}"

    def close(self) -> None:
        self._http.close()
        if self._robots is not None:
            try:
                self._robots._http.close()
            except Exception:
                log.debug("failed closing robots HTTP client", exc_info=True)
        for r in self._created_res:
            try:
                r.http.close()
            except Exception:
                log.debug("failed closing a worker HTTP client", exc_info=True)

    def __enter__(self) -> "WebCrawler":
        return self

    def __exit__(self, *exc) -> bool:
        self.close()
        return False
