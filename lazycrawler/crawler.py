# -*- coding: utf-8 -*-
"""
lazycrawler.crawler
===================
WebCrawler: recursive crawl with two INDEPENDENT LLM knobs.

There are two distinct LLM uses, each toggled separately:
  - content : how page content is produced
              "pure"  -> trafilatura/regex (no LLM)
              "smart" -> LLM structured extraction (title, summary, entities...)
  - links   : how links to follow are chosen
              "pure"  -> heuristic (first N candidates, already filtered)
              "smart" -> LLM relevance ranking against the topic

``mode`` is a shortcut that sets both; ``content=`` / ``links=`` override it:
  crawl(url, mode="smart")                  -> content=smart, links=smart
  crawl(url, mode="pure")                   -> content=pure,  links=pure
  crawl(url, content="smart", links="pure") -> LLM summary, heuristic links
  crawl(url, content="pure",  links="smart")-> no summary, LLM link selection

Output:
  - always: List[PageResult] (ready to be consumed by an LLM/agent)
  - optional: persistence to CrawlerDB with 3-level dedup and a TTL cache

DB cache: if a page is already in the DB (fresh), the crawler returns it from
the DB instead of re-fetching — the clean text (content=pure) or the summary +
structured fields (content=smart), depending on the requested content mode. A
page cached in pure content can be enriched to smart from the stored text (LLM
only, no re-fetch).

Hard rule: if content and links are both "pure", no LLM is ever built or called.
"""

from __future__ import annotations

import re
import time
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
    published_iso: Optional[str] = None
    is_pdf: bool = False
    depth: int = 0
    source_url: Optional[str] = None
    error: Optional[str] = None
    from_cache: bool = False


# =============================================================================
# CRAWL STATE (shared across the seeds of one run)
# =============================================================================

@dataclass
class _State:
    content_mode: Mode          # how page content is produced
    link_mode: Mode             # how links are selected
    topic: str
    session_id: Optional[str]
    link_selector: Any = None
    visited: Set[str] = field(default_factory=set)
    results: List[PageResult] = field(default_factory=list)
    pages_done: int = 0


# =============================================================================
# WEB CRAWLER
# =============================================================================

class WebCrawler:
    """
    Recursive crawler with independent content/link LLM modes and optional DB
    persistence.

    Parameters
    ----------
    crawler_cfg : CrawlerConfig
        Depth/page/link limits, large-doc thresholds, blacklist.
    http_cfg : HTTPConfig
        Timeouts, retries, user-agent, link delay, SSL verification.
    llm_cfg : LLMConfig, optional
        LLM configuration used when content and/or links is "smart".
    db : CrawlerDB, optional
        If provided, pages are persisted with 3-level dedup.
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

        self._http = HTTPClient(self.http_cfg)
        self._llm = None  # built lazily when content or links is "smart"

    # -- public API -----------------------------------------------------------

    def crawl(
        self,
        url: str,
        *,
        mode: Mode = "pure",
        content: Optional[Mode] = None,
        links: Optional[Mode] = None,
        topic: str = "",
        session_id: Optional[str] = None,
    ) -> List[PageResult]:
        """
        Crawl a single URL (and its links up to max_depth).

        ``mode`` sets both content and links; ``content=`` / ``links=`` override.
        """
        return self.crawl_many([url], mode=mode, content=content, links=links,
                               topic=topic, session_id=session_id)

    def crawl_many(
        self,
        urls: List[str],
        *,
        mode: Mode = "pure",
        content: Optional[Mode] = None,
        links: Optional[Mode] = None,
        topic: str = "",
        session_id: Optional[str] = None,
        source: str = "crawl",
    ) -> List[PageResult]:
        """
        Crawl a list of URLs sharing state (visited set, page counter).
        Also used by WebSearch to crawl search results.
        """
        content_mode: Mode = content or mode
        link_mode: Mode = links or mode
        st = _State(content_mode=content_mode, link_mode=link_mode,
                    topic=topic, session_id=session_id)

        if content_mode == "smart" or link_mode == "smart":
            self._ensure_llm()
        if link_mode == "smart":
            st.link_selector = self._llm.build_link_selector(
                topic or "general relevant content", self.cfg.max_links_per_level
            )

        if self.db is not None:
            st.session_id = session_id or self._default_session_id(topic, content_mode)
            self.db.create_session(
                st.session_id, topic=topic, seed=urls[0] if urls else "",
                mode=content_mode, source=source,
            )

        print(f"  [CRAWL] content={content_mode} links={link_mode} "
              f"depth={self.cfg.max_depth} max_pages={self.cfg.max_pages}")

        for i, url in enumerate(urls):
            if st.pages_done >= self.cfg.max_pages:
                print(f"  [CRAWL] reached max_pages ({self.cfg.max_pages}) - stop")
                break
            if is_blacklisted_domain(url, self.blacklist):
                print(f"  [CRAWL] seed blacklisted, skip: {url}")
                continue
            if i > 0:
                time.sleep(self.http_cfg.link_delay)
            try:
                self._crawl_page(st, url, depth=0, source_url=None,
                                 start_domain=get_base_domain(url))
            except Exception as e:
                print(f"  [CRAWL] error on {url[:80]}: {type(e).__name__}: {e} - continuing")

        return st.results

    # -- recursive core -------------------------------------------------------

    def _crawl_page(
        self,
        st: _State,
        url: str,
        depth: int,
        source_url: Optional[str],
        start_domain: str,
    ) -> None:
        cfg = self.cfg
        if st.pages_done >= cfg.max_pages:
            return

        url = normalize_url(url)
        indent = "  " * depth

        if is_blacklisted_domain(url, self.blacklist):
            print(f"{indent}[d{depth}] blacklisted - skip: {url[:80]}")
            return
        if url in st.visited:
            return
        st.visited.add(url)
        uh = _url_hash(url)

        print(f"\n{indent}[d{depth}/{cfg.max_depth}] ({st.pages_done}/{cfg.max_pages}) {url[:90]}")

        # -- DEDUP level 1: fresh URL cache (content-mode-aware) --------------
        if self._try_cache(st, url, uh, depth, source_url):
            return

        # -- FETCH ------------------------------------------------------------
        html, raw_text, status_code = self._http.fetch(url)
        if not html and not (raw_text or "").strip():
            self._emit(st, PageResult(
                url=url, url_hash=uh, status="fetch_error", mode=st.content_mode,
                depth=depth, source_url=source_url,
                error=f"Fetch failed (status={status_code})",
            ), count=False)
            return

        # -- PDF vs HTML ------------------------------------------------------
        is_pdf = looks_like_pdf(url, html or "", raw_text or "")
        published_iso: Optional[str] = None
        pdf_title = ""

        if is_pdf:
            print(f"{indent}  [PDF] dedicated extraction")
            pdf_text, pdf_title, pdf_pub = extract_pdf(
                url, timeout=self.http_cfg.pdf_timeout, user_agent=self.http_cfg.user_agent
            )
            if pdf_pub:
                published_iso = pdf_pub
            if pdf_text.strip():
                raw_text = pdf_text
                html = ""  # no link extraction from a PDF
        else:
            canonical = extract_canonical_url(html, url)
            if canonical:
                cnorm = normalize_url(canonical)
                if is_blacklisted_domain(cnorm, self.blacklist):
                    print(f"{indent}  canonical blacklisted - skip")
                    return
                if cnorm != url and cnorm not in st.visited:
                    st.visited.discard(url)
                    url = cnorm
                    uh = _url_hash(url)
                    st.visited.add(url)
            published_iso = extract_published_datetime(html, url)

        # -- candidate links (for recursion) ----------------------------------
        candidates: List[Tuple[str, str]] = []
        if depth < cfg.max_depth and not is_pdf and html:
            candidates = extract_candidate_links(
                html, url, start_domain,
                same_domain_only=cfg.same_domain_only,
                max_links=cfg.max_candidate_links,
            )
            candidates = [
                (t, u) for (t, u) in candidates
                if normalize_url(u) not in st.visited
                and not is_blacklisted_domain(u, self.blacklist)
            ]

        # -- no text: emit no_text, optionally recurse ------------------------
        if not (raw_text or "").strip():
            self._emit(st, PageResult(
                url=url, url_hash=uh, status="no_text", mode=st.content_mode,
                depth=depth, source_url=source_url, published_iso=published_iso,
                is_pdf=is_pdf, error="No extractable text",
            ), count=False)
            self._recurse(st, candidates, depth, url, start_domain)
            return

        preclean = preprocess_text(raw_text)
        if is_pdf:
            title = pdf_title or title_from_pdf_text(preclean) or title_from_url(url)
        else:
            title = extract_page_title(html)

        # -- DEDUP level 2/3: content_hash ------------------------------------
        chash = _content_hash(raw_text)
        if self.db is not None:
            existing = self.db.find_by_content_hash(chash)
            if existing and self._can_reuse(existing, st.content_mode):
                self.db.add_edge(st.session_id, uh, source_url=source_url, depth=depth)
                if existing.get("url_hash") != uh:
                    self._copy_content(existing, url, uh)
                reused = self.db.get_page(uh) or existing
                st.results.append(self._result_from_row(reused, depth, source_url, from_cache=True))
                st.pages_done += 1
                print(f"{indent}  [cache] content dedup - skip extraction")
                self._recurse(st, candidates, depth, url, start_domain,
                              excerpt=reused.get("clean_text") or "")
                return

        # -- content extraction (content mode) --------------------------------
        if st.content_mode == "pure":
            result = PageResult(
                url=url, url_hash=uh, status="done", mode="pure",
                title=title, text=preclean[: cfg.max_chars_pure],
                published_iso=published_iso, is_pdf=is_pdf,
                depth=depth, source_url=source_url,
            )
        else:
            result = self._smart_extract(st, url, uh, preclean, title, published_iso, is_pdf, depth, source_url)

        self._emit(st, result, count=(result.status == "done"), raw_text=raw_text, content_hash=chash)

        # -- recursion (link mode) --------------------------------------------
        self._recurse(st, candidates, depth, url, start_domain, excerpt=preclean)

    # -- DB cache (content-mode-aware) ----------------------------------------

    def _try_cache(self, st, url, uh, depth, source_url) -> bool:
        """
        Try to satisfy this URL from the DB cache without re-fetching.

        Returns True if handled:
          - full hit: cached content satisfies the content mode (no fetch, no LLM)
          - enrich:   cached as pure but content=smart requested -> run LLM on
                      the stored text (no fetch)
        Cached hits are terminal (no link recursion: the HTML is not stored).
        """
        if self.db is None:
            return False
        row = self.db.get_fresh_page(url)
        if not row:
            return False
        indent = "  " * depth

        if self._satisfies(row, st.content_mode):
            self.db.add_edge(st.session_id, uh, source_url=source_url, depth=depth)
            st.results.append(self._result_from_row(row, depth, source_url, from_cache=True))
            st.pages_done += 1
            print(f"{indent}  [cache] hit (fresh, content={st.content_mode}) - no fetch")
            return True

        # cached as pure but smart content requested -> enrich from stored text
        if st.content_mode == "smart":
            base = (row.get("raw_text") or row.get("clean_text") or "")
            if base.strip():
                print(f"{indent}  [cache] enrich (pure->smart) - no fetch, LLM only")
                self._ensure_llm()
                preclean = preprocess_text(base)
                result = self._smart_extract(
                    st, url, uh, preclean, row.get("title") or "",
                    row.get("published_iso"), bool(row.get("is_pdf")), depth, source_url,
                )
                self._emit(st, result, count=(result.status == "done"),
                           raw_text=row.get("raw_text") or base,
                           content_hash=row.get("content_hash") or _content_hash(base))
                return True
        return False

    @staticmethod
    def _satisfies(row: dict, content_mode: str) -> bool:
        """Whether a cached row already provides what the content mode needs."""
        if content_mode == "pure":
            return bool(row.get("clean_text"))
        return row.get("mode") == "smart"

    # -- smart content extraction ---------------------------------------------

    def _smart_extract(
        self, st, url, uh, preclean, title, published_iso, is_pdf, depth, source_url
    ) -> PageResult:
        cfg = self.cfg
        content_text = preclean[: cfg.max_chars_content]
        # large documents -> map-reduce before structured extraction
        if len(preclean) > cfg.large_doc_threshold:
            content_text = self._llm.summarize_large(
                url, preclean,
                max_chars_out=cfg.max_chars_content,
                threshold=cfg.large_doc_threshold,
                chunk_chars=cfg.large_doc_chunk_chars,
                max_chunks=cfg.large_doc_max_chunks,
            )

        extract = self._llm.extract_content(url, content_text)
        if extract is None:
            return PageResult(
                url=url, url_hash=uh, status="llm_error", mode="smart",
                title=title, published_iso=published_iso, is_pdf=is_pdf,
                depth=depth, source_url=source_url, error="LLM extraction failed",
            )
        return PageResult(
            url=url, url_hash=uh, status="done", mode="smart",
            title=extract.title or title,
            text=extract.clean_text or None,
            summary=extract.summary or None,
            entities=extract.entities or [],
            topics=extract.topics or [],
            published_iso=published_iso, is_pdf=is_pdf,
            depth=depth, source_url=source_url,
        )

    # -- recursion over selected links (link mode) ----------------------------

    def _recurse(self, st, candidates, depth, source_url, start_domain, excerpt="") -> None:
        cfg = self.cfg
        if depth >= cfg.max_depth or st.pages_done >= cfg.max_pages or not candidates:
            return

        if st.link_mode == "smart" and st.link_selector is not None:
            selected = self._llm.select_links(
                st.link_selector, excerpt, candidates, cfg.max_links_per_level
            )
        else:
            # pure: heuristic - first N candidates (already filtered)
            selected = candidates[: cfg.max_links_per_level]

        selected = [(a, u) for (a, u) in selected if not is_blacklisted_domain(u, self.blacklist)]
        if not selected:
            return

        indent = "  " * depth
        print(f"{indent}  -> {len(selected)} links selected (links={st.link_mode})")
        for _, link_url in selected:
            if st.pages_done >= cfg.max_pages:
                break
            time.sleep(self.http_cfg.link_delay)
            try:
                self._crawl_page(st, link_url, depth + 1, source_url=source_url,
                                 start_domain=start_domain)
            except Exception as e:
                print(f"{indent}  error {link_url[:70]}: {type(e).__name__}: {e}")

    # -- persistence / results ------------------------------------------------

    def _emit(
        self, st, result: PageResult, *, count: bool,
        raw_text: Optional[str] = None, content_hash: Optional[str] = None,
    ) -> None:
        """Append the result, update the counter, persist to DB."""
        st.results.append(result)
        if count:
            st.pages_done += 1

        if self.db is None:
            return
        page = {
            "url": result.url, "url_hash": result.url_hash,
            "domain": get_base_domain(result.url), "is_pdf": result.is_pdf,
            "status": result.status, "mode": result.mode, "error": result.error,
            "raw_text": raw_text, "clean_text": result.text, "title": result.title,
            "summary": result.summary, "entities": result.entities,
            "topics": result.topics, "published_iso": result.published_iso,
            "content_hash": content_hash,
        }
        self.db.upsert_page(page)
        if st.session_id:
            self.db.add_edge(st.session_id, result.url_hash,
                             source_url=result.source_url, depth=result.depth)

    @staticmethod
    def _can_reuse(existing: dict, content_mode: str) -> bool:
        """
        Decide whether to reuse content already in the DB (dedup level 2/3).
        Reuse if the existing row is 'smart' (already rich) or the requested
        content is 'pure'. If content='smart' and existing='pure', do NOT reuse:
        re-run the LLM to enrich (level 3, without re-fetch).
        """
        if existing.get("status") != "done":
            return False
        if content_mode == "pure":
            return True
        return existing.get("mode") == "smart"

    def _copy_content(self, existing: dict, url: str, uh: str) -> None:
        """Write the reused content under a new url_hash (a content alias)."""
        page = dict(existing)
        page.update({"url": url, "url_hash": uh})
        page.pop("entities", None)
        page.pop("topics", None)
        self.db.upsert_page(page)

    def _result_from_row(self, row: dict, depth: int, source_url: Optional[str], from_cache: bool) -> PageResult:
        return PageResult(
            url=row.get("url", ""), url_hash=row.get("url_hash", ""),
            status=row.get("status", "done"), mode=row.get("mode", "pure"),
            title=row.get("title"), text=row.get("clean_text"),
            summary=row.get("summary"),
            entities=row.get("entities") or [], topics=row.get("topics") or [],
            published_iso=row.get("published_iso"), is_pdf=bool(row.get("is_pdf")),
            depth=depth, source_url=source_url,
            error=row.get("error"), from_cache=from_cache,
        )

    # -- helpers --------------------------------------------------------------

    def _ensure_llm(self) -> None:
        if self._llm is None:
            from .llm import CrawlerLLM  # lazy import: pure/pure never touches LazyBridge
            self._llm = CrawlerLLM(self.llm_cfg or LLMConfig())

    @staticmethod
    def _default_session_id(topic: str, content_mode: str) -> str:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        slug = re.sub(r"[^a-z0-9]+", "-", (topic or "crawl").lower()).strip("-")[:32] or "crawl"
        return f"{slug}_{content_mode}_{ts}"

    def close(self) -> None:
        self._http.close()
