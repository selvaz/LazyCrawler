# -*- coding: utf-8 -*-
"""
lazycrawler.crawler
===================
WebCrawler: orchestrator for recursive crawls with three independent modes
(pure / ml / smart) and a native parallel mode.

Architecture (after refactoring):
  - models.py      → PageResult (public output type)
  - _pipeline.py   → PagePipeline (per-page processing: fetch/extract/emit)
  - crawler.py     → WebCrawler (this file: traversal strategies + wiring)

LLM knobs (set independently):
  content : "pure" | "ml" | "smart"
  links   : "pure" | "ml" | "smart"

``mode`` sets both; ``content=`` / ``links=`` override either independently.

Parallel mode:
  CrawlerConfig(max_workers=N) with N>1 → bounded ThreadPoolExecutor,
  level-by-level BFS. N=1 → sequential DFS.

JavaScript rendering:
  HTTPConfig(render_js=True) routes fetches through a headless browser.

WARNING: SSRF guard
  ``HTTPConfig.block_private_addresses`` is **False by default** (so library
  users can crawl local/intranet sites). If you accept URLs from external
  sources or from an LLM agent, enable it:

      http_cfg = HTTPConfig(block_private_addresses=True)
      crawler  = WebCrawler(http_cfg=http_cfg, ...)

  ``CrawlerTools`` (the agent-facing wrapper) enables this automatically.
  See also: https://github.com/selvaz/lazycrawler#ssrf-guard
"""

from __future__ import annotations

import heapq
import itertools
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, List, Literal, Optional, Set, Tuple

from ._log import log
from ._pipeline import PagePipeline
from .config import CrawlerConfig, HTTPConfig, LLMConfig, MLConfig
from .db import CrawlerDB
from .http import (
    HTTPClient,
    RobotsChecker,
    compile_exclude,
    get_base_domain,
    is_blacklisted_domain,
    load_blacklist_from_excel,
    normalize_url,
)
from .models import PageResult  # noqa: F401  (re-exported for backward compat)
from .pdf import extract_pdf, extract_pdf_bytes  # noqa: F401  (re-exported: tests patch these)
from .ratelimit import HostRateLimiter

# Re-export is_blocked_address so existing test monkeypatches continue to work.
# Tests patch lazycrawler.crawler.is_blocked_address; _pipeline.py accesses it
# lazily via this module to pick up the patch.
from .http import is_blocked_address  # noqa: F401

Mode = Literal["pure", "ml", "smart"]
Status = Literal["done", "fetch_error", "no_text", "llm_error", "blacklisted", "robots_blocked"]


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
    max_depth: int = 0
    cfg: Any = None
    ml_cfg: Any = None
    link_selector: Any = None
    visited: Set[str] = field(default_factory=set)
    results: List[PageResult] = field(default_factory=list)
    pages_done: int = 0
    lock: Any = field(default_factory=threading.Lock)


@dataclass
class _Res:
    """Resources for one worker (shared in sequential, per-thread in parallel)."""

    http: HTTPClient
    llm: Any = None
    ml: Any = None
    link_selector: Any = None


# =============================================================================
# WEB CRAWLER  (orchestrator)
# =============================================================================


class WebCrawler:
    """
    Recursive crawler with independent content/link modes, optional DB
    persistence, native parallel mode, and optional JS rendering.

    .. note:: SSRF guard

       ``HTTPConfig.block_private_addresses`` defaults to ``False`` for library
       compatibility (internal crawls must work). Pass ``True`` whenever URLs
       come from an untrusted source or an LLM agent. ``CrawlerTools`` sets it
       automatically on the agent path.
    """

    def __init__(
        self,
        crawler_cfg: Optional[CrawlerConfig] = None,
        http_cfg: Optional[HTTPConfig] = None,
        llm_cfg: Optional[LLMConfig] = None,
        db: Optional[CrawlerDB] = None,
        ml_cfg: Optional[MLConfig] = None,
    ):
        self.cfg = crawler_cfg or CrawlerConfig()
        self.http_cfg = http_cfg or HTTPConfig()
        self.llm_cfg = llm_cfg
        self.ml_cfg = ml_cfg
        self.db = db

        self.blacklist = list(self.cfg.blacklist)
        if self.cfg.blacklist_excel:
            self.blacklist += load_blacklist_from_excel(
                self.cfg.blacklist_excel,
                self.cfg.blacklist_excel_sheet,
                self.cfg.blacklist_excel_column,
            )

        self._http = HTTPClient(self.http_cfg)
        self._llm = None  # lazy CrawlerLLM for sequential mode
        self._tls = threading.local()
        self._created_res: List[_Res] = []
        self._robots = (
            RobotsChecker(HTTPClient(self.http_cfg), self.http_cfg.user_agent)
            if self.cfg.respect_robots
            else None
        )
        self._exclude_re = compile_exclude(self.cfg.exclude_patterns)
        self._rate = HostRateLimiter(self.http_cfg.per_host_delay, self._robots)
        self._call_depth = 0
        self._call_lock = threading.Lock()

        # Per-page processing delegate (stateless, reused across runs)
        self._pipeline = PagePipeline(
            blacklist=self.blacklist,
            http_cfg=self.http_cfg,
            db=self.db,
            robots=self._robots,
            rate=self._rate,
            exclude_re=self._exclude_re,
        )

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
        overrides: Optional[dict] = None,
        ml_overrides: Optional[dict] = None,
    ) -> List[PageResult]:
        """Crawl a single URL (and its links up to max_depth).

        ``max_depth`` overrides ``CrawlerConfig.max_depth`` for this call only.
        ``overrides`` / ``ml_overrides`` apply per-call config without mutating
        the shared instance (the preset mechanism).
        """
        return self.crawl_many(
            [url], mode=mode, content=content, links=links, topic=topic, schema=schema,
            session_id=session_id, max_depth=max_depth, overrides=overrides,
            ml_overrides=ml_overrides,
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
        overrides: Optional[dict] = None,
        ml_overrides: Optional[dict] = None,
    ) -> List[PageResult]:
        """Crawl a list of URLs sharing state (visited set, page counter)."""
        content_mode: Mode = content or mode
        link_mode: Mode = links or mode
        eff_cfg = replace(self.cfg, **overrides) if overrides else self.cfg
        base_ml = self.ml_cfg or MLConfig()
        eff_ml_cfg = replace(base_ml, **ml_overrides) if ml_overrides else base_ml
        eff_depth = eff_cfg.max_depth if max_depth is None else max(0, int(max_depth))
        st = _State(
            content_mode=content_mode, link_mode=link_mode, topic=topic,
            session_id=session_id, schema=schema, max_depth=eff_depth,
            cfg=eff_cfg, ml_cfg=eff_ml_cfg,
        )

        if self.db is not None:
            st.session_id = session_id or self._default_session_id(topic, content_mode)
            self.db.create_session(
                st.session_id, topic=topic, seed=urls[0] if urls else "",
                mode=content_mode, source=source,
            )

        seeds = [
            (u, get_base_domain(u)) for u in urls
            if not is_blacklisted_domain(u, self.blacklist)
        ]

        log.info(
            "crawl: content=%s links=%s workers=%d depth=%d max_pages=%d robots=%s strict=%s",
            content_mode, link_mode, self.cfg.max_workers, eff_depth,
            eff_cfg.max_pages, self.cfg.respect_robots, self.cfg.strict,
        )
        log.debug("seeds: %d URL(s), start_domain(s): %s", len(seeds), [d for _, d in seeds])

        if link_mode == "ml" and st.ml_cfg.best_first:
            self._crawl_ordered(st, seeds)
        elif self.cfg.max_workers > 1:
            self._crawl_parallel(st, seeds)
        else:
            res = self._sequential_res(st)
            for i, (url, dom) in enumerate(seeds):
                if self._cap_reached(st):
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
        if st.content_mode == "smart" or st.link_mode == "smart":
            self._ensure_llm()
        ml = None
        if st.content_mode == "ml" or st.link_mode == "ml":
            from .ml import MLEngine
            ml = MLEngine(st.ml_cfg)
        selector = self._build_link_selector(st, self._llm, ml)
        st.link_selector = selector
        return _Res(self._http, llm=self._llm, ml=ml, link_selector=selector)

    def _build_res(self, st: _State) -> _Res:
        http = HTTPClient(self.http_cfg)
        llm = ml = None
        if st.content_mode == "smart" or st.link_mode == "smart":
            from .llm import CrawlerLLM
            llm = CrawlerLLM(self.llm_cfg or LLMConfig())
        if st.content_mode == "ml" or st.link_mode == "ml":
            from .ml import MLEngine
            ml = MLEngine(st.ml_cfg)
        selector = self._build_link_selector(st, llm, ml)
        return _Res(http, llm=llm, ml=ml, link_selector=selector)

    def _build_link_selector(self, st: _State, llm, ml):
        topic = st.topic or "general relevant content"
        if st.link_mode == "smart" and llm is not None:
            return llm.build_link_selector(topic, st.cfg.max_links_per_level)
        if st.link_mode == "ml" and ml is not None:
            return ml.build_link_selector(topic, st.cfg.max_links_per_level)
        return None

    def _worker_res(self, st: _State) -> _Res:
        r = getattr(self._tls, "res", None)
        if r is None:
            r = self._build_res(st)
            self._tls.res = r
            with st.lock:
                self._created_res.append(r)
        return r

    # -- traversal strategies -------------------------------------------------

    def _crawl_seq(self, st, url, depth, source_url, start_domain, res) -> None:
        """Sequential depth-first traversal."""
        links = self._pipeline.process_one(st, url, depth, source_url, start_domain, res)
        if depth >= st.max_depth:
            return
        for _score, _anchor, link_url in links:
            if self._cap_reached(st):
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
        """Native parallel BFS over a bounded thread pool (level-by-level)."""
        self._tls = threading.local()
        self._created_res = []
        frontier = [(url, dom, None) for (url, dom) in seeds]
        depth = 0
        try:
            with ThreadPoolExecutor(max_workers=self.cfg.max_workers) as pool:
                while frontier and not self._cap_reached(st):
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
                        for _score, _anchor, link_url in links:
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
            self._close_worker_res()

    def _crawl_ordered(self, st, seeds) -> None:
        """Best-first BFS (links="ml"): globally score-ordered frontier."""
        self._tls = threading.local()
        self._created_res = []
        counter = itertools.count()
        heap: List[Tuple[float, int, int, str, Optional[str], str]] = []
        for url, dom in seeds:
            heapq.heappush(heap, (-1e9, 0, next(counter), url, None, dom))
        min_score = st.ml_cfg.min_link_score
        workers = max(1, self.cfg.max_workers)
        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                while heap and not self._cap_reached(st):
                    wave = [heapq.heappop(heap) for _ in range(min(workers, len(heap)))]
                    fut_map = {
                        pool.submit(self._worker_process, st, url, depth, src, dom): (url, depth, dom)
                        for (_neg, depth, _cnt, url, src, dom) in wave
                    }
                    for fut in as_completed(fut_map):
                        parent_url, parent_depth, parent_dom = fut_map[fut]
                        try:
                            links = fut.result() or []
                        except Exception:
                            if self.cfg.strict:
                                raise
                            log.exception("best-first worker error on %s", parent_url[:70])
                            links = []
                        if parent_depth >= st.max_depth:
                            continue
                        for score, _anchor, link_url in links:
                            if score < min_score:
                                continue
                            heapq.heappush(
                                heap,
                                (-score, parent_depth + 1, next(counter),
                                 link_url, parent_url, parent_dom),
                            )
        finally:
            self._close_worker_res()

    def _worker_process(self, st, url, depth, source_url, start_domain):
        return self._pipeline.process_one(
            st, url, depth, source_url, start_domain, self._worker_res(st)
        )

    # -- helpers --------------------------------------------------------------

    @staticmethod
    def _cap_reached(st: _State) -> bool:
        with st.lock:
            return st.pages_done >= st.cfg.max_pages

    def _close_worker_res(self) -> None:
        for r in self._created_res:
            try:
                r.http.close()
            except Exception:
                log.debug("failed closing a worker HTTP client", exc_info=True)

    def _ensure_llm(self) -> None:
        if self._llm is None:
            from .llm import CrawlerLLM
            self._llm = CrawlerLLM(self.llm_cfg or LLMConfig())

    @staticmethod
    def _default_session_id(topic: str, content_mode: str) -> str:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        rand = uuid.uuid4().hex[:6]
        slug = re.sub(r"[^a-z0-9]+", "-", (topic or "crawl").lower()).strip("-")[:32] or "crawl"
        return f"{slug}_{content_mode}_{ts}_{rand}"

    def release(self) -> None:
        """Release transient HTTP resources (sockets/browser), keeping the crawler reusable."""
        self._http.release()
        if self._robots is not None:
            try:
                self._robots._http.release()
            except Exception:
                log.debug("failed releasing robots HTTP client", exc_info=True)
        for r in self._created_res:
            try:
                r.http.release()
            except Exception:
                log.debug("failed releasing a worker HTTP client", exc_info=True)
        self._created_res = []

    def close(self) -> None:
        self.release()

    def _begin_call(self) -> None:
        with self._call_lock:
            self._call_depth += 1

    def _end_call_release(self) -> None:
        with self._call_lock:
            self._call_depth = max(0, self._call_depth - 1)
            if self._call_depth > 0:
                return
        self.release()

    def __enter__(self) -> "WebCrawler":
        return self

    def __exit__(self, *exc) -> bool:
        self.close()
        return False
