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
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, List, Literal, Optional, Set, Tuple

from pydantic import BaseModel, Field

from .config import CrawlerConfig, HTTPConfig, LLMConfig
from .db import CrawlerDB
from .http import (
    HTTPClient,
    get_base_domain,
    is_blacklisted_domain,
    load_blacklist_from_excel,
    normalize_url,
    content_hash as _content_hash,
    url_hash as _url_hash,
)
from .pdf import extract_pdf, looks_like_pdf, title_from_pdf_text, title_from_url
from .text import (
    extract_candidate_links,
    extract_canonical_url,
    extract_page_title,
    extract_published_datetime,
    preprocess_text,
)

Mode = Literal["pure", "smart"]
Status = Literal["done", "fetch_error", "no_text", "llm_error", "blacklisted"]


# =============================================================================
# OUTPUT MODEL
# =============================================================================

class PageResult(BaseModel):
    """Result of crawling a single page."""
    url: str
    url_hash: str = ""
    status: Status = "done"
    mode: Mode = "pure"                  # content mode that produced this result
    title: Optional[str] = None
    text: Optional[str] = None          # clean text (pure: cleaned; smart: LLM clean_text)
    summary: Optional[str] = None       # smart content only
    entities: List[str] = Field(default_factory=list)
    topics: List[str] = Field(default_factory=list)
    data: Optional[dict] = None         # full structured object (custom schema)
    published_iso: Optional[str] = None
    is_pdf: bool = False
    depth: int = 0
    source_url: Optional[str] = None
    error: Optional[str] = None
    from_cache: bool = False


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
    link_selector: Any = None           # sequential link-selection agent
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

        self._http = HTTPClient(self.http_cfg)   # shared client for sequential mode
        self._llm = None                          # lazy CrawlerLLM for sequential mode
        self._tls = threading.local()             # per-thread resources (parallel)
        self._created_res: List[_Res] = []        # thread-local resources to close

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
    ) -> List[PageResult]:
        """Crawl a single URL (and its links up to max_depth)."""
        return self.crawl_many([url], mode=mode, content=content, links=links,
                               topic=topic, schema=schema, session_id=session_id)

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
    ) -> List[PageResult]:
        """Crawl a list of URLs sharing state (visited set, page counter)."""
        content_mode: Mode = content or mode
        link_mode: Mode = links or mode
        st = _State(content_mode=content_mode, link_mode=link_mode,
                    topic=topic, session_id=session_id, schema=schema)

        if self.db is not None:
            st.session_id = session_id or self._default_session_id(topic, content_mode)
            self.db.create_session(
                st.session_id, topic=topic, seed=urls[0] if urls else "",
                mode=content_mode, source=source,
            )

        seeds = [(u, get_base_domain(u)) for u in urls
                 if not is_blacklisted_domain(u, self.blacklist)]

        print(f"  [CRAWL] content={content_mode} links={link_mode} "
              f"workers={self.cfg.max_workers} depth={self.cfg.max_depth} "
              f"max_pages={self.cfg.max_pages}")

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
                except Exception as e:
                    print(f"  [CRAWL] error on {url[:80]}: {type(e).__name__}: {e}")

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
        if depth >= self.cfg.max_depth:
            return
        for _, link_url in links:
            if self._reached_cap(st):
                break
            if self.http_cfg.link_delay:
                time.sleep(self.http_cfg.link_delay)
            try:
                self._crawl_seq(st, link_url, depth + 1, url, start_domain, res)
            except Exception as e:
                print(f"  error {link_url[:70]}: {type(e).__name__}: {e}")

    def _crawl_parallel(self, st, seeds) -> None:
        """Native parallel driver: level-by-level BFS over a bounded thread pool."""
        self._tls = threading.local()
        self._created_res = []
        frontier = [(url, dom, None) for (url, dom) in seeds]   # (url, start_domain, source_url)
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
                        except Exception as e:
                            print(f"  [PARALLEL] worker error: {type(e).__name__}: {e}")
                            links = []
                        for _, link_url in links:
                            nu = normalize_url(link_url)
                            if nu in seen_next:
                                continue
                            seen_next.add(nu)
                            next_frontier.append((link_url, parent_dom, parent_url))
                    depth += 1
                    if depth > self.cfg.max_depth:
                        break
                    frontier = next_frontier
        finally:
            for r in self._created_res:
                try:
                    r.http.close()
                except Exception:
                    pass

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
        if not self._mark_visited(st, url):
            return []
        uh = _url_hash(url)
        print(f"  [d{depth}] {url[:90]}")

        # DEDUP level 1: fresh URL cache (content-mode-aware)
        cached = self._try_cache(st, url, uh, depth, source_url, res)
        if cached is not None:
            return cached

        # FETCH
        html, raw_text, status_code = res.http.fetch(url)
        if not html and not (raw_text or "").strip():
            self._emit(st, PageResult(
                url=url, url_hash=uh, status="fetch_error", mode=st.content_mode,
                depth=depth, source_url=source_url,
                error=f"Fetch failed (status={status_code})",
            ), count=False)
            return []

        # PDF vs HTML
        is_pdf = looks_like_pdf(url, html or "", raw_text or "")
        published_iso: Optional[str] = None
        pdf_title = ""
        if is_pdf:
            pdf_text, pdf_title, pdf_pub = extract_pdf(
                url, timeout=self.http_cfg.pdf_timeout, user_agent=self.http_cfg.user_agent
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
        candidates: List[Tuple[str, str]] = []
        if depth < cfg.max_depth and not is_pdf and html:
            candidates = extract_candidate_links(
                html, url, start_domain,
                same_domain_only=cfg.same_domain_only,
                max_links=cfg.max_candidate_links,
            )
            with st.lock:
                visited_snapshot = set(st.visited)
            candidates = [
                (t, u) for (t, u) in candidates
                if normalize_url(u) not in visited_snapshot
                and not is_blacklisted_domain(u, self.blacklist)
            ]

        # no text
        if not (raw_text or "").strip():
            self._emit(st, PageResult(
                url=url, url_hash=uh, status="no_text", mode=st.content_mode,
                depth=depth, source_url=source_url, published_iso=published_iso,
                is_pdf=is_pdf, error="No extractable text",
            ), count=False)
            return self._select_next(st, candidates, "", res)

        preclean = preprocess_text(raw_text)
        title = (pdf_title or title_from_pdf_text(preclean) or title_from_url(url)) if is_pdf \
            else extract_page_title(html)

        # DEDUP level 2/3: content_hash
        chash = _content_hash(raw_text)
        if self.db is not None:
            existing = self.db.find_by_content_hash(chash)
            if existing and self._can_reuse(existing, st.content_mode):
                # create the page row BEFORE the edge (crawl_edges has an FK on pages)
                if existing.get("url_hash") != uh:
                    self._copy_content(existing, url, uh)
                self.db.add_edge(st.session_id, uh, source_url=source_url, depth=depth)
                reused = self.db.get_page(uh) or existing
                result = self._result_from_row(reused, depth, source_url, from_cache=True)
                self._add_counted(st, result)
                return self._select_next(st, candidates, reused.get("clean_text") or "", res)

        # content extraction
        if st.content_mode == "pure":
            result = PageResult(
                url=url, url_hash=uh, status="done", mode="pure",
                title=title, text=preclean[: cfg.max_chars_pure],
                published_iso=published_iso, is_pdf=is_pdf,
                depth=depth, source_url=source_url,
            )
        else:
            result = self._smart_extract(st, url, uh, preclean, title,
                                         published_iso, is_pdf, depth, source_url, res)

        self._emit(st, result, count=(result.status == "done"),
                   raw_text=raw_text, content_hash=chash)
        return self._select_next(st, candidates, preclean, res)

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
            result = self._result_from_row(row, depth, source_url, from_cache=True)
            self._add_counted(st, result)
            if self.db:
                self.db.add_edge(st.session_id, uh, source_url=source_url, depth=depth)
            return []

        if st.content_mode == "smart":
            base = (row.get("raw_text") or row.get("clean_text") or "")
            if base.strip():
                preclean = preprocess_text(base)
                result = self._smart_extract(
                    st, url, uh, preclean, row.get("title") or "",
                    row.get("published_iso"), bool(row.get("is_pdf")), depth, source_url, res,
                )
                self._emit(st, result, count=(result.status == "done"),
                           raw_text=row.get("raw_text") or base,
                           content_hash=row.get("content_hash") or _content_hash(base))
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

    def _smart_extract(self, st, url, uh, preclean, title, published_iso,
                       is_pdf, depth, source_url, res) -> PageResult:
        cfg = self.cfg
        content_text = preclean[: cfg.max_chars_content]
        if len(preclean) > cfg.large_doc_threshold:
            content_text = res.llm.summarize_large(
                url, preclean,
                max_chars_out=cfg.max_chars_content,
                threshold=cfg.large_doc_threshold,
                chunk_chars=cfg.large_doc_chunk_chars,
                max_chunks=cfg.large_doc_max_chunks,
            )

        extract = res.llm.extract_content(url, content_text, schema=st.schema)
        if extract is None:
            return PageResult(
                url=url, url_hash=uh, status="llm_error", mode="smart",
                title=title, published_iso=published_iso, is_pdf=is_pdf,
                depth=depth, source_url=source_url, error="LLM extraction failed",
            )

        data = extract.model_dump()
        text = getattr(extract, "clean_text", None) or None
        if st.schema is not None and not text:
            text = json.dumps(data, ensure_ascii=False)   # keep custom data searchable
        return PageResult(
            url=url, url_hash=uh, status="done", mode="smart",
            title=getattr(extract, "title", None) or title,
            text=text,
            summary=getattr(extract, "summary", None) or None,
            entities=list(getattr(extract, "entities", None) or []),
            topics=list(getattr(extract, "topics", None) or []),
            data=data,
            published_iso=published_iso, is_pdf=is_pdf,
            depth=depth, source_url=source_url,
        )

    # -- link selection -------------------------------------------------------

    def _select_next(self, st, candidates, excerpt, res) -> List[Tuple[str, str]]:
        cfg = self.cfg
        if not candidates:
            return []
        if st.link_mode == "smart" and res.link_selector is not None:
            selected = res.llm.select_links(res.link_selector, excerpt, candidates, cfg.max_links_per_level)
        else:
            selected = candidates[: cfg.max_links_per_level]
        return [(a, u) for (a, u) in selected if not is_blacklisted_domain(u, self.blacklist)]

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

    def _emit(self, st, result: PageResult, *, count: bool,
              raw_text: Optional[str] = None, content_hash: Optional[str] = None) -> None:
        with st.lock:
            st.results.append(result)
            if count:
                st.pages_done += 1
        if self.db is None:
            return
        self.db.upsert_page({
            "url": result.url, "url_hash": result.url_hash,
            "domain": get_base_domain(result.url), "is_pdf": result.is_pdf,
            "status": result.status, "mode": result.mode, "error": result.error,
            "raw_text": raw_text, "clean_text": result.text, "title": result.title,
            "summary": result.summary, "entities": result.entities,
            "topics": result.topics, "data": result.data,
            "published_iso": result.published_iso, "content_hash": content_hash,
        })
        if st.session_id:
            self.db.add_edge(st.session_id, result.url_hash,
                             source_url=result.source_url, depth=result.depth)

    def _copy_content(self, existing: dict, url: str, uh: str) -> None:
        page = dict(existing)
        page.update({"url": url, "url_hash": uh})
        page.pop("entities", None)
        page.pop("topics", None)
        page.pop("data", None)
        self.db.upsert_page(page)

    def _result_from_row(self, row: dict, depth: int, source_url: Optional[str], from_cache: bool) -> PageResult:
        return PageResult(
            url=row.get("url", ""), url_hash=row.get("url_hash", ""),
            status=row.get("status", "done"), mode=row.get("mode", "pure"),
            title=row.get("title"), text=row.get("clean_text"),
            summary=row.get("summary"),
            entities=row.get("entities") or [], topics=row.get("topics") or [],
            data=row.get("data"),
            published_iso=row.get("published_iso"), is_pdf=bool(row.get("is_pdf")),
            depth=depth, source_url=source_url,
            error=row.get("error"), from_cache=from_cache,
        )

    # -- helpers --------------------------------------------------------------

    def _ensure_llm(self) -> None:
        if self._llm is None:
            from .llm import CrawlerLLM
            self._llm = CrawlerLLM(self.llm_cfg or LLMConfig())

    @staticmethod
    def _default_session_id(topic: str, content_mode: str) -> str:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        slug = re.sub(r"[^a-z0-9]+", "-", (topic or "crawl").lower()).strip("-")[:32] or "crawl"
        return f"{slug}_{content_mode}_{ts}"

    def close(self) -> None:
        self._http.close()
        for r in self._created_res:
            try:
                r.http.close()
            except Exception:
                pass
