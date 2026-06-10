# -*- coding: utf-8 -*-
"""
lazycrawler._pipeline
=====================
Per-page processing pipeline, extracted from WebCrawler to keep it testable
in isolation.

``PagePipeline`` owns everything that happens to **a single page**: cache
lookup, fetch delegation, text extraction dispatch (pure/ml/smart), artifact
collection, link selection, and DB persistence. It is purely functional given
a ``_CrawlerCtx`` (static, per-crawl context) and ``_State``/``_Res``
(dynamic, per-run/per-worker state) received from the orchestrator.

``WebCrawler`` imports this and calls ``pipeline.process_one()``. The traversal
strategies (sequential DFS, parallel BFS, best-first) live in the orchestrator
and are not mixed into the pipeline logic.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple

from ._log import log
from .artifacts import (
    Artifact,
    bytes_sha256,
    extract_html_artifacts,
    extract_html_artifacts_anchored,
    sniff_image,
)
from .http import content_hash as _content_hash
from .http import (
    get_base_domain,
    is_blacklisted_domain,
    normalize_url,
)
from .http import url_hash as _url_hash
from .models import PageResult
from .pdf import looks_like_pdf, title_from_pdf_text, title_from_url
from .text import (
    extract_canonical_url,
    extract_page_title,
    extract_published_datetime,
    preprocess_text,
)


def _crawler_fn(name):
    """
    Lazy accessor for functions that live in lazycrawler.crawler.

    Accessing them via the crawler module (rather than direct import) allows
    test monkeypatching of ``lazycrawler.crawler.<name>`` to work correctly even
    after the logic moved to _pipeline.py.  The circular import is safe because
    this is always called at function execution time, never at module load time.
    """
    import lazycrawler.crawler as _cm  # lazy: avoids circular import at module level

    return getattr(_cm, name)


class PagePipeline:
    """
    Stateless per-page processor.

    All crawler state (blacklist, db, robots, rate limiter, http_cfg) is passed
    at construction as a ``_CrawlerCtx``-like bundle so the pipeline methods are
    free of ``self._`` crawl-level attributes and can be tested with mock objects.

    Parameters
    ----------
    blacklist : list[str]
        Domain blacklist (crawl-level, immutable after construction).
    http_cfg : HTTPConfig
        HTTP configuration (ssl, timeouts, SSRF, pdf caps).
    db : CrawlerDB | None
        Persistence layer; None = no caching.
    robots : RobotsChecker | None
        robots.txt gate; None = respect_robots=False.
    rate : HostRateLimiter
        Per-host polite delay gate.
    """

    def __init__(self, blacklist, http_cfg, db, robots, rate, exclude_re):
        self.blacklist = blacklist
        self.http_cfg = http_cfg
        self.db = db
        self.robots = robots
        self.rate = rate
        self.exclude_re = exclude_re  # compiled link-exclusion regex

    # -------------------------------------------------------------------------
    # Entry point
    # -------------------------------------------------------------------------

    def process_one(
        self,
        st: Any,
        url: str,
        depth: int,
        source_url: Optional[str],
        start_domain: str,
        res: Any,
    ) -> List[Tuple[float, str, str]]:
        """
        Process one URL: cache/fetch/extract/emit. Returns the (already selected)
        links to follow next as ``[(score, anchor, url)]``. ``[]`` = nothing to follow.

        Parameters
        ----------
        st : _State
            Per-run shared state (visited set, results, counters, lock).
        url : str
            URL to process (will be normalized inside).
        depth : int
            Current crawl depth.
        source_url : str | None
            The page that linked here (for provenance).
        start_domain : str
            Registrable domain of the seed URL (for same-domain enforcement).
        res : _Res
            Per-worker resources (http client, llm, ml engine).
        """
        cfg = st.cfg
        if self._reached_cap(st):
            return []
        url = normalize_url(url)
        if is_blacklisted_domain(url, self.blacklist):
            return []
        if self.http_cfg.block_private_addresses and _crawler_fn("is_blocked_address")(url):
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
        log.info("[d%d | p%d/%d] %s", depth, _page_num, cfg.max_pages, url[:90])

        # robots.txt gate
        if self.robots is not None and not self.robots.allowed(url):
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

        # DEDUP level 1: fresh URL cache
        cached = self._try_cache(st, url, uh, depth, source_url, res)
        if cached is not None:
            return cached

        # FETCH
        self.rate.wait(url)
        fr = res.http.fetch(url)
        log.debug(
            "  fetch: HTTP %s | html=%d chars | text=%d chars | pdf_bytes=%d",
            fr.status or "ERR",
            len(fr.html or ""),
            len(fr.text or "") if fr.text else 0,
            len(fr.content or b""),
        )
        # Post-fetch processing (extract/artifacts/persist/select) is shared with
        # the async crawler, which performs its own (non-blocking) fetch and then
        # delegates the tail here. Keeping it in one method guarantees the sync and
        # async engines stay feature-identical (PDF, canonical, dedup, ml/smart,
        # artifacts, markdown, persistence) with no divergence.
        return self.process_fetched(st, url, uh, depth, source_url, start_domain, res, fr)

    def process_fetched(
        self,
        st: Any,
        url: str,
        uh: str,
        depth: int,
        source_url: Optional[str],
        start_domain: str,
        res: Any,
        fr: Any,
    ) -> List[Tuple[float, str, str]]:
        """Process an already-fetched page: redirect adoption, PDF/canonical
        resolution, content extraction (pure/ml/smart), artifact collection,
        Markdown rendering, persistence, and link selection.

        Split out of :meth:`process_one` so both the synchronous crawler (which
        fetches inline) and :class:`~lazycrawler.async_crawler.AsyncWebCrawler`
        (which fetches via aiohttp, then calls this in a thread executor) share
        the exact same post-fetch behavior. ``fr`` is any object exposing the
        :class:`~lazycrawler.http.FetchResult` interface
        (``html``/``text``/``status``/``content``/``final_url``).
        """
        cfg = st.cfg
        html, raw_text, status_code, pdf_bytes = fr.html, fr.text, fr.status, fr.content
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

        # Post-redirect: re-check the final host and **adopt** it as the page
        # identity so link resolution, cache keys, edges, canonical base, and
        # provenance all reflect the origin the content actually came from.
        requested_url: Optional[str] = None
        final = normalize_url(fr.final_url or url)
        if final != url:
            if self.robots is not None and not self.robots.allowed(final):
                log.info("robots.txt disallows redirect target %s - skipping", final[:90])
                self._emit(
                    st,
                    PageResult(
                        url=url,
                        url_hash=uh,
                        status="robots_blocked",
                        mode=st.content_mode,
                        depth=depth,
                        source_url=source_url,
                        error="Disallowed by robots.txt (redirect target)",
                    ),
                    count=False,
                )
                return []
            if is_blacklisted_domain(final, self.blacklist):
                return []
            if self.http_cfg.block_private_addresses and _crawler_fn("is_blocked_address")(final):
                log.info("SSRF guard: redirect target is a blocked address %s", final[:90])
                self._emit(
                    st,
                    PageResult(
                        url=url,
                        url_hash=uh,
                        status="fetch_error",
                        mode=st.content_mode,
                        depth=depth,
                        source_url=source_url,
                        error="Blocked private/loopback redirect target (SSRF guard)",
                    ),
                    count=False,
                )
                return []
            requested_url, url = url, final
            uh = _url_hash(url)
            # Adopt the redirect target as this page's identity. If it was already
            # visited (another seed/page redirected or normalized to the same final
            # URL), it is already represented — bail without re-emitting, mirroring
            # the canonical-adoption guard below. Without this, two source URLs that
            # land on one target both emit it and both count toward max_pages.
            if not self._mark_visited(st, url):
                log.debug("  redirect target already visited - skipping duplicate: %s", url[:90])
                return []

        # PDF vs HTML detection
        is_pdf = bool(pdf_bytes) or looks_like_pdf(url, html or "", raw_text or "")
        if is_pdf:
            log.debug("  detected as PDF")
        published_iso: Optional[str] = None
        pdf_title = ""
        if is_pdf:
            if pdf_bytes:
                pdf_text, pdf_title, pdf_pub = _crawler_fn("extract_pdf_bytes")(pdf_bytes)
            else:
                pdf_text, pdf_title, pdf_pub = _crawler_fn("extract_pdf")(
                    url,
                    timeout=self.http_cfg.pdf_timeout,
                    user_agent=self.http_cfg.user_agent,
                    verify=(self.http_cfg.ca_bundle or self.http_cfg.verify_ssl),
                    max_bytes=self.http_cfg.max_pdf_bytes,
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
                if self.http_cfg.block_private_addresses and _crawler_fn("is_blocked_address")(
                    cnorm
                ):
                    log.info("canonical points to a blocked address - ignoring: %s", cnorm[:90])
                elif cnorm != url and self._mark_visited(st, cnorm):
                    url = cnorm
                    uh = _url_hash(url)
            published_iso = extract_published_datetime(html, url)

        # candidate links (extracted before text check so empty-text pages still
        # contribute to the frontier in best-first / smart-link modes)
        candidates = self._extract_candidates(st, html, url, start_domain, depth, is_pdf)

        # no text
        if not (raw_text or "").strip():
            log.debug("  -> no_text: trafilatura/fallback returned nothing")
            nt = PageResult(
                url=url,
                url_hash=uh,
                status="no_text",
                mode=st.content_mode,
                depth=depth,
                source_url=source_url,
                published_iso=published_iso,
                is_pdf=is_pdf,
                requested_url=requested_url,
                error="No extractable text",
            )
            if cfg.extract_artifacts:
                nt.artifacts, _ = self._collect_artifacts(st, html, url, pdf_bytes, is_pdf, res)
            self._emit(st, nt, count=False, candidate_links=candidates)
            if self.db is not None and nt.artifacts:
                self.db.add_artifacts(nt.url_hash, nt.artifacts)
            return self._select_next(st, candidates, "", res)

        preclean = preprocess_text(raw_text)
        title = (
            (pdf_title or title_from_pdf_text(preclean) or title_from_url(url))
            if is_pdf
            else extract_page_title(html)
        )
        log.debug("  title: %r", (title or "")[:80])

        # DEDUP level 2: content_hash (skip LLM if we already have this content)
        chash = _content_hash(raw_text)
        if self.db is not None:
            existing = self.db.find_by_content_hash(chash)
            if existing and self._can_reuse(existing, st.content_mode):
                if existing.get("url_hash") != uh:
                    self._copy_content(existing, url, uh, candidates)
                self.db.add_edge(st.session_id, uh, source_url=source_url, depth=depth)
                reused = self.db.get_page(uh) or existing
                result = self._result_from_row(st, reused, depth, source_url, from_cache=True)
                self._add_counted(st, result)
                log.debug("  content-hash dedup - reused stored content, skipped extraction")
                return self._select_next(st, candidates, reused.get("clean_text") or "", res)

        # content extraction (pure / ml / smart)
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
        elif st.content_mode == "ml":
            log.debug("  content [ml]: local extraction (preclean=%d chars)...", len(preclean))
            result = self._ml_extract(
                st, url, uh, preclean, title, published_iso, is_pdf, depth, source_url, res
            )
        else:
            log.debug("  content [smart]: LLM extraction (preclean=%d chars)...", len(preclean))
            result = self._smart_extract(
                st, url, uh, preclean, title, published_iso, is_pdf, depth, source_url, res
            )

        if requested_url:
            result.requested_url = requested_url

        # artifacts (tables / images / charts / svg)
        anchored_html: Optional[str] = None
        if cfg.extract_artifacts:
            result.artifacts, anchored_html = self._collect_artifacts(
                st, html, url, pdf_bytes, is_pdf, res
            )

        # optional Markdown render for RAG ingestion (HTML only)
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

    # -------------------------------------------------------------------------
    # Link candidate extraction
    # -------------------------------------------------------------------------

    def _extract_candidates(
        self, st: Any, html: Optional[str], url: str, start_domain: str, depth: int, is_pdf: bool
    ) -> List[Tuple[str, str]]:
        from .text import extract_candidate_links

        cfg = st.cfg
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
            exclude_pattern=self.exclude_re,
            same_host_only=cfg.same_host_only,
        )
        return self._filter_candidates(st, candidates)

    def _filter_candidates(
        self, st: Any, candidates: List[Tuple[str, str]]
    ) -> List[Tuple[str, str]]:
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

    # -------------------------------------------------------------------------
    # Cache helpers
    # -------------------------------------------------------------------------

    def _try_cache(
        self, st: Any, url: str, uh: str, depth: int, source_url: Optional[str], res: Any
    ) -> Optional[List[Tuple[float, str, str]]]:
        if self.db is None:
            return None
        row = self.db.get_fresh_page(url)
        if not row:
            return None

        if self._satisfies(row, st.content_mode):
            log.debug("  cache hit (fresh, content=%s) - skipping fetch", st.content_mode)
            result = self._result_from_row(st, row, depth, source_url, from_cache=True)
            self._add_counted(st, result)
            if self.db:
                self.db.add_edge(st.session_id, uh, source_url=source_url, depth=depth)
            if st.cfg.recurse_from_cache and depth < st.max_depth:
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

        if st.content_mode in ("smart", "ml"):
            base = row.get("raw_text") or row.get("clean_text") or ""
            if base.strip():
                log.debug("  cache enrich (pure->%s) - no fetch", st.content_mode)
                preclean = preprocess_text(base)
                enrich = self._ml_extract if st.content_mode == "ml" else self._smart_extract
                result = enrich(
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
        if content_mode == "ml":
            return row.get("mode") in ("ml", "smart")
        return row.get("mode") == "smart"

    @staticmethod
    def _can_reuse(existing: dict, content_mode: str) -> bool:
        if existing.get("status") != "done":
            return False
        if content_mode == "pure":
            return True
        if content_mode == "ml":
            return existing.get("mode") in ("ml", "smart")
        return existing.get("mode") == "smart"

    # -------------------------------------------------------------------------
    # Content extraction
    # -------------------------------------------------------------------------

    def _ml_extract(
        self, st, url, uh, preclean, title, published_iso, is_pdf, depth, source_url, res
    ) -> PageResult:
        cfg = st.cfg
        extract = res.ml.extract_content(url, preclean[: cfg.max_chars_content], schema=None)
        text = (getattr(extract, "clean_text", None) or preclean[: cfg.max_chars_pure]) or None
        return PageResult(
            url=url,
            url_hash=uh,
            status="done",
            mode="ml",
            title=title,
            text=text,
            summary=getattr(extract, "summary", None) or None,
            entities=list(getattr(extract, "entities", None) or []),
            topics=list(getattr(extract, "topics", None) or []),
            sentiment=getattr(extract, "sentiment", None),
            published_iso=published_iso,
            is_pdf=is_pdf,
            depth=depth,
            source_url=source_url,
        )

    def _smart_extract(
        self, st, url, uh, preclean, title, published_iso, is_pdf, depth, source_url, res
    ) -> PageResult:
        cfg = st.cfg
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
            text = json.dumps(data, ensure_ascii=False)
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

    # -------------------------------------------------------------------------
    # Artifacts
    # -------------------------------------------------------------------------

    def _collect_artifacts(
        self, st, html, url, pdf_bytes, is_pdf, res
    ) -> "Tuple[List[Artifact], Optional[str]]":
        cfg = st.cfg
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
        cfg = st.cfg
        if cfg.download_artifact_bytes:
            for a in arts:
                if a.blob is None and a.src_url and a.artifact_type in ("image", "chart"):
                    self.rate.wait(a.src_url)
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
        for a in arts:
            if a.blob is not None and not a.bytes_hash:
                a.bytes_hash = bytes_sha256(a.blob)
                a.size_bytes = a.size_bytes or len(a.blob)
            a.ensure_content_hash()
        if cfg.enrich_artifacts and st.content_mode == "smart" and res.llm is not None:
            for a in arts[: cfg.max_artifacts_to_enrich]:
                res.llm.enrich_artifact(a)
        return arts

    # -------------------------------------------------------------------------
    # Link selection
    # -------------------------------------------------------------------------

    def _select_next(
        self, st, candidates: List[Tuple[str, str]], excerpt: str, res
    ) -> List[Tuple[float, str, str]]:
        cfg = st.cfg
        if not candidates:
            log.debug("  next: no candidates -> nothing queued")
            return []
        if st.link_mode == "ml" and res.link_selector is not None:
            scored = res.ml.select_links(
                res.link_selector, excerpt, candidates, cfg.max_links_per_level
            )
            log.debug(
                "  next: ML scored %d candidate(s) -> %d queued", len(candidates), len(scored)
            )
            for i, (score, anchor, link_url) in enumerate(scored[:5]):
                log.debug(
                    "    [%d] %.3f %s -> %s", i + 1, score, (anchor or "")[:40], link_url[:80]
                )
        elif st.link_mode == "smart" and res.link_selector is not None:
            selected = res.llm.select_links(
                res.link_selector, excerpt, candidates, cfg.max_links_per_level
            )
            log.debug("  next: LLM selected %d link(s)", len(selected))
            scored = [(0.0, a, u) for (a, u) in selected]
        else:
            selected = candidates[: cfg.max_links_per_level]
            log.debug(
                "  next: heuristic (first %d of %d) -> %d queued",
                cfg.max_links_per_level,
                len(candidates),
                len(selected),
            )
            scored = [(0.0, a, u) for (a, u) in selected]
        after_bl = [
            (s, a, u) for (s, a, u) in scored if not is_blacklisted_domain(u, self.blacklist)
        ]
        if len(after_bl) < len(scored):
            log.debug(
                "  next: -%d blacklisted -> %d final", len(scored) - len(after_bl), len(after_bl)
            )
        return after_bl

    # -------------------------------------------------------------------------
    # Thread-safe state helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def _mark_visited(st, url: str) -> bool:
        with st.lock:
            if url in st.visited:
                return False
            st.visited.add(url)
            return True

    @staticmethod
    def _reached_cap(st) -> bool:
        with st.lock:
            return st.pages_done >= st.cfg.max_pages

    @staticmethod
    def _add_counted(st, result: PageResult) -> None:
        with st.lock:
            if st.pages_done >= st.cfg.max_pages:
                return
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
            if count and st.pages_done >= st.cfg.max_pages:
                return
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
                st.session_id,
                result.url_hash,
                source_url=result.source_url,
                depth=result.depth,
            )

    def _copy_content(
        self,
        existing: dict,
        url: str,
        uh: str,
        candidate_links: Optional[List[Tuple[str, str]]] = None,
    ) -> None:
        # Same content (content_hash match) reached under a new URL. ``existing`` is
        # a RAW pages row (find_by_content_hash returns dict(row), not _row_to_page),
        # so its serialized columns (raw_text/clean_text/*_json) copy across verbatim
        # — correct, since the content is identical. We only re-key it to the new
        # URL, attach THIS page's freshly-found candidate links, and stamp a fresh
        # crawl time so the new URL gets its own TTL window (not the original's
        # remaining one — upsert_page only defaults crawled_at when absent).
        page = dict(existing)
        page.update(
            {
                "url": url,
                "url_hash": uh,
                "crawled_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        page["links_json"] = (
            json.dumps([[a, u] for (a, u) in candidate_links], ensure_ascii=False)
            if candidate_links
            else None
        )
        self.db.upsert_page(page)

    def _load_artifacts(self, st, url_hash: str) -> List[Artifact]:
        if self.db is None or not st.cfg.extract_artifacts or not url_hash:
            return []
        try:
            return [Artifact(**a) for a in self.db.get_artifacts(url_hash=url_hash)]
        except Exception:
            log.debug("failed loading cached artifacts for %s", url_hash, exc_info=True)
            return []

    def _result_from_row(
        self, st, row: dict, depth: int, source_url: Optional[str], from_cache: bool
    ) -> PageResult:
        return PageResult(
            artifacts=self._load_artifacts(st, row.get("url_hash", "")),
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
