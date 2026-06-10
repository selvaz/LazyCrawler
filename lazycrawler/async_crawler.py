# -*- coding: utf-8 -*-
"""
lazycrawler.async_crawler
=========================
Async (asyncio + aiohttp) web crawler for high-throughput I/O-bound crawling.

``AsyncWebCrawler`` mirrors the ``WebCrawler`` public API but is fully async:
all I/O (HTTP, robots.txt, per-host delay) is non-blocking. It is independent
of the sync crawler and does not share state with it.

Modes
-----
``content`` / ``links`` accept ``"pure"`` or ``"ml"`` (set both via ``mode=``):

  - ``pure`` — clean text only (the original async behavior).
  - ``ml``   — zero-token local ML: semantic best-first link selection
    (``links="ml"``) and/or local content extraction (``content="ml"``:
    TextRank summary, YAKE topics, spaCy entities, VADER sentiment).

The async engine fetches over aiohttp (non-blocking I/O) but reuses the *exact*
synchronous post-fetch pipeline (``PagePipeline.process_fetched``) for redirect
adoption, PDF/canonical handling, content extraction, artifact collection,
Markdown rendering and DB persistence — run in a thread executor so the CPU-bound
ML work never blocks the event loop. This guarantees full feature parity with the
synchronous ``WebCrawler`` (artifacts, persistence/reporting, dedup) across both
``pure`` and ``ml`` modes. ``smart`` (LLM) extraction is intentionally *not*
available on the async path — use the synchronous ``WebCrawler`` for that.

Requirements
------------
    pip install "lazycrawler[async]"
    # or: pip install aiohttp

SSRF guard
----------
``block_private_addresses=True`` is **the default** in the async crawler because
its primary use case is high-throughput crawling of external URLs. Pass
``block_private_addresses=False`` explicitly for internal/intranet crawling.

Usage
-----
    import asyncio
    from lazycrawler.async_crawler import AsyncWebCrawler
    from lazycrawler.config import CrawlerConfig, HTTPConfig

    async def main():
        cfg = CrawlerConfig(max_depth=1, max_pages=50)
        http_cfg = HTTPConfig(block_private_addresses=True)
        async with AsyncWebCrawler(cfg, http_cfg) as crawler:
            results = await crawler.crawl("https://example.com/", topic="python")
            for r in results:
                print(r.status, r.url, len(r.text or ""))

    asyncio.run(main())
"""

from __future__ import annotations

import asyncio
import heapq
import ipaddress
import itertools
import re
import socket
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import Any, List, Literal, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

from ._log import log
from ._pipeline import PagePipeline
from .config import CrawlerConfig, HTTPConfig, MLConfig
from .crawler import WebCrawler, _Res, _State
from .http import (
    FetchResult,
    HTTPClient,
    compile_exclude,
    get_hostname,
    is_blacklisted_domain,
    normalize_url,
)
from .http import url_hash as _url_hash
from .models import PageResult
from .ratelimit import HostRateLimiter

Mode = Literal["pure", "ml"]

try:
    import aiohttp

    _AIOHTTP_OK = True
except ImportError:
    _AIOHTTP_OK = False

try:
    import trafilatura  # type: ignore

    _TRAFILATURA_OK = True
except ImportError:
    _TRAFILATURA_OK = False


# =============================================================================
# SSRF GUARD (async-safe: DNS in executor)
# =============================================================================


async def _is_blocked_async(url: str) -> bool:
    """Async SSRF guard: resolves DNS in a thread executor, checks every IP.

    .. warning::
       Best-effort guard, not network isolation. The IPs are validated at check
       time but aiohttp re-resolves the host at connect time, so DNS rebinding /
       TOCTOU is possible. See :func:`lazycrawler.http.is_blocked_address`.
    """
    host = get_hostname(url)
    if not host:
        return True
    if (
        host == "localhost"
        or host.endswith(".local")
        or host in {"metadata", "metadata.google.internal"}
    ):
        return True
    loop = asyncio.get_event_loop()
    try:
        infos = await loop.run_in_executor(None, socket.getaddrinfo, host, None)
    except Exception:
        return True  # DNS failure -> fail-closed
    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return True
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            ip = mapped
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return True
    return False


# =============================================================================
# PER-HOST ASYNC RATE LIMITER
# =============================================================================


class _AsyncRateLimiter:
    """Minimum-gap limiter keyed by host. Asyncio-safe (no threading.Lock)."""

    def __init__(self, delay: float = 0.0):
        self._delay = max(0.0, delay)
        self._next: dict = defaultdict(float)

    async def wait(self, url: str) -> None:
        if self._delay <= 0:
            return
        host = get_hostname(url)
        if not host:
            return
        now = time.monotonic()
        nxt = max(now, self._next[host])
        self._next[host] = nxt + self._delay
        sleep_for = nxt - now
        if sleep_for > 0:
            log.debug("  async rate-limit: %.2fs for %s", sleep_for, host)
            await asyncio.sleep(sleep_for)


# =============================================================================
# ASYNC HTTP CLIENT
# =============================================================================


@dataclass
class _AsyncFetchResult:
    html: Optional[str] = None
    text: Optional[str] = None
    status: Optional[int] = None
    content: Optional[bytes] = None  # raw bytes for PDFs (downloaded once)
    content_type: str = ""
    final_url: Optional[str] = None  # last hop after manual redirect following

    def to_fetch_result(self) -> FetchResult:
        """Adapt to the synchronous :class:`~lazycrawler.http.FetchResult` shape
        consumed by ``PagePipeline.process_fetched``."""
        return FetchResult(
            html=self.html,
            text=self.text,
            status=self.status,
            content=self.content,
            content_type=self.content_type,
            final_url=self.final_url,
        )


class _AsyncHTTPClient:
    """Thin aiohttp-based fetch client."""

    def __init__(self, cfg: HTTPConfig):
        self.cfg = cfg
        self._session: Optional[aiohttp.ClientSession] = None

    def _ssl_param(self):
        """SSL setting for aiohttp honoring the same semantics as the sync client:
        a ``ca_bundle`` path -> custom CA context; ``verify_ssl=False`` -> no
        verification; otherwise default verification."""
        if self.cfg.ca_bundle:
            import ssl as _ssl

            return _ssl.create_default_context(cafile=self.cfg.ca_bundle)
        return True if self.cfg.verify_ssl else False

    async def _ensure(self) -> "aiohttp.ClientSession":
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(ssl=self._ssl_param())
            headers = {
                "User-Agent": self.cfg.user_agent,
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            }
            timeout = aiohttp.ClientTimeout(
                connect=self.cfg.timeout_connect,
                sock_read=self.cfg.timeout_read,
            )
            self._session = aiohttp.ClientSession(
                connector=connector, headers=headers, timeout=timeout
            )
        return self._session

    async def fetch(self, url: str) -> _AsyncFetchResult:
        cfg = self.cfg
        for attempt in range(1, cfg.max_retries + 1):
            try:
                # ClientResponseError (retryable HTTP status) and transport errors
                # are both retried with backoff by the generic handler below.
                return await self._fetch_once(url)
            except Exception as exc:
                if attempt < cfg.max_retries:
                    await asyncio.sleep(cfg.backoff_base_sec * (2 ** (attempt - 1)))
                else:
                    log.warning(
                        "async fetch failed for %s after %d attempts: %s", url, attempt, exc
                    )
                    return _AsyncFetchResult()
        return _AsyncFetchResult()

    async def _fetch_once(self, url: str) -> _AsyncFetchResult:
        """One fetch attempt with **manual** redirect handling: every hop is
        re-validated by the SSRF guard, the hop count is bounded, and the body is
        streamed with a hard ``max_html_bytes`` cap (mirrors the sync client)."""
        cfg = self.cfg
        session = await self._ensure()
        current = url
        for _ in range(cfg.max_redirects + 1):
            if cfg.block_private_addresses and await _is_blocked_async(current):
                log.warning("async SSRF guard: refusing redirect/fetch to %s", current)
                return _AsyncFetchResult(final_url=current)
            async with session.get(current, allow_redirects=False) as resp:
                status = resp.status
                # Manual redirect: re-loop so the next hop is SSRF-checked.
                if status in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
                    current = urljoin(current, resp.headers["Location"])
                    continue
                if status in (429, 500, 502, 503, 504):
                    raise aiohttp.ClientResponseError(
                        resp.request_info, resp.history, status=status
                    )
                if 400 <= status < 600:
                    return _AsyncFetchResult(status=status, final_url=current)
                content_type = resp.headers.get("Content-Type", "").lower()
                # Strip the query before the extension test (parity with the sync
                # client) so a PDF URL carrying a query string (``/doc.pdf?t=…``) is
                # still grabbed as bytes here, rather than mis-capped as HTML and
                # forced into the pipeline's urllib re-download path.
                is_pdf = "application/pdf" in content_type or current.lower().split("?")[
                    0
                ].endswith(".pdf")
                # PDFs are downloaded once as raw bytes (capped); text extraction
                # is deferred to the shared pipeline (extract_pdf_bytes), matching
                # the synchronous client. HTML is streamed to max_html_bytes.
                cap = cfg.max_pdf_bytes if is_pdf else cfg.max_html_bytes
                total = 0
                chunks: List[bytes] = []
                async for chunk in resp.content.iter_chunked(65536):
                    if not chunk:
                        continue
                    chunks.append(chunk)
                    total += len(chunk)
                    if total >= cap:
                        break
                body = b"".join(chunks)[:cap]
                if is_pdf:
                    return _AsyncFetchResult(
                        status=status,
                        content=body,
                        content_type=content_type,
                        final_url=current,
                    )
                enc = None
                if "charset=" in content_type:
                    enc = content_type.split("charset=")[-1].split(";")[0].strip() or None
                html = body.decode(enc or "utf-8", errors="replace")
                text = self._extract(html)
                return _AsyncFetchResult(html=html, text=text, status=status, final_url=current)
        log.warning("async: too many redirects (> %d) for %s", cfg.max_redirects, url)
        return _AsyncFetchResult(final_url=current)

    def _extract(self, html: str) -> Optional[str]:
        # Honor the configured min_text_chars threshold (parity with the sync client).
        min_chars = self.cfg.min_text_chars
        if _TRAFILATURA_OK:
            try:
                out = trafilatura.extract(html, include_comments=False, favor_recall=True)
                if out and len(out.strip()) >= min_chars:
                    return out.strip()
            except Exception:
                pass
        # basic strip fallback
        s = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "", html)
        s = re.sub(r"(?is)<.*?>", " ", s)
        s = re.sub(r"\s+", " ", s).strip()
        return s if len(s) >= min_chars else None

    async def get_robots(self, url: str) -> Optional[str]:
        try:
            p = urlparse(url)
            robots_url = f"{p.scheme}://{p.netloc}/robots.txt"
            session = await self._ensure()
            async with session.get(robots_url) as resp:
                if resp.status < 400:
                    return await resp.text()
        except Exception:
            pass
        return None

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


# =============================================================================
# ASYNC ROBOTS CHECKER
# =============================================================================


class _AsyncRobotsChecker:
    """Async robots.txt gate with per-host caching."""

    def __init__(self, http: _AsyncHTTPClient, user_agent: str):
        self._http = http
        self._ua = user_agent or "*"
        self._cache: dict = {}
        self._locks: dict = {}

    async def allowed(self, url: str) -> bool:
        try:
            p = urlparse(url)
            host = (p.netloc or "").lower()
        except Exception:
            return True
        if not host:
            return True
        rp = await self._get(p.scheme or "https", host, url)
        if rp is None:
            return True
        try:
            return rp.can_fetch(self._ua, url)
        except Exception:
            return True

    async def _get(self, scheme: str, host: str, original_url: str) -> Optional[RobotFileParser]:
        if host in self._cache:
            return self._cache[host]
        if host not in self._locks:
            self._locks[host] = asyncio.Lock()
        async with self._locks[host]:
            if host in self._cache:
                return self._cache[host]
            text = await self._http.get_robots(original_url)
            rp: Optional[RobotFileParser] = None
            if text:
                rp = RobotFileParser()
                try:
                    rp.parse(text.splitlines())
                except Exception:
                    rp = None
            self._cache[host] = rp
            return rp


# =============================================================================
# ASYNC WEB CRAWLER
# =============================================================================
#
# Shared per-run state (``_State``) and per-worker resources (``_Res``) come from
# the synchronous crawler so the async engine reuses the exact same post-fetch
# pipeline. The async layer owns only the non-blocking I/O (robots, rate, fetch)
# and the traversal orchestration; everything after the fetch is delegated to
# ``PagePipeline.process_fetched`` in a thread executor.


class AsyncWebCrawler:
    """
    High-throughput async web crawler with full ``pure`` / ``ml`` parity.

    Fetches over aiohttp (non-blocking) and reuses the synchronous
    :class:`~lazycrawler._pipeline.PagePipeline` for all post-fetch processing
    (content extraction, artifacts, persistence) in a thread executor, so the
    CPU-bound ML work never blocks the event loop and behavior matches the
    synchronous :class:`~lazycrawler.crawler.WebCrawler`.

    Parameters
    ----------
    crawler_cfg, http_cfg :
        As in :class:`~lazycrawler.crawler.WebCrawler`.
    db : CrawlerDB | None
        Optional persistence layer (sessions, pages, edges, artifacts). The
        async engine writes through it from executor threads (it is thread-safe).
    ml_cfg : MLConfig | None
        Configuration for ``ml`` mode (semantic link scoring / local extraction).

    .. note::
       ``block_private_addresses`` defaults to ``True`` in the async crawler
       (unlike the sync ``WebCrawler``). Pass ``HTTPConfig(block_private_addresses=False)``
       explicitly for internal/intranet use.

    Requirements: ``pip install aiohttp`` (or ``pip install lazycrawler[async]``);
    ``ml`` mode additionally needs ``pip install lazycrawler[ml]`` (and ``[nlp]``
    for ``content="ml"`` entities/sentiment).
    """

    def __init__(
        self,
        crawler_cfg: Optional[CrawlerConfig] = None,
        http_cfg: Optional[HTTPConfig] = None,
        db: Any = None,
        ml_cfg: Optional[MLConfig] = None,
    ):
        if not _AIOHTTP_OK:
            raise RuntimeError(
                "AsyncWebCrawler requires aiohttp. Install with:\n"
                "    pip install aiohttp\n"
                "or: pip install lazycrawler[async]"
            )
        self.cfg = crawler_cfg or CrawlerConfig()
        # SSRF guard is ON by default in the async crawler (external URL use case).
        raw_http = http_cfg or HTTPConfig()
        # The async engine never renders JS (it fetches over aiohttp), and the SSRF
        # guard it forces on below is incompatible with render_js in the sync helper
        # client used for artifact/PDF downloads. Disable render_js (with a warning)
        # rather than letting that helper raise mid-crawl.
        if raw_http.render_js:
            log.warning("AsyncWebCrawler does not render JS - ignoring render_js=True")
            raw_http = replace(raw_http, render_js=False)
        if not raw_http.block_private_addresses:
            self.http_cfg = replace(raw_http, block_private_addresses=True)
            log.debug("AsyncWebCrawler: block_private_addresses enabled by default")
        else:
            self.http_cfg = raw_http
        self.ml_cfg = ml_cfg
        self.db = db
        self.blacklist = list(self.cfg.blacklist)
        self._exclude_re = compile_exclude(self.cfg.exclude_patterns)
        self._http = _AsyncHTTPClient(self.http_cfg)
        self._robots: Optional[_AsyncRobotsChecker] = (
            _AsyncRobotsChecker(self._http, self.http_cfg.user_agent)
            if self.cfg.respect_robots
            else None
        )
        self._rate = _AsyncRateLimiter(self.http_cfg.per_host_delay)
        # Shared synchronous post-fetch pipeline. robots=None: the async path does
        # robots/rate/SSRF before the fetch, so the pipeline must not redo blocking
        # robots calls. The (synchronous) rate limiter is used only for artifact
        # byte downloads that the pipeline performs inside executor threads.
        self._pipeline = PagePipeline(
            blacklist=self.blacklist,
            http_cfg=self.http_cfg,
            db=self.db,
            robots=None,
            rate=HostRateLimiter(self.http_cfg.per_host_delay),
            exclude_re=self._exclude_re,
        )
        # Per-worker (thread-local) resources are built lazily in executor threads
        # and live on the per-run ``_State`` (not the instance), so concurrent
        # crawl() calls on one AsyncWebCrawler never clobber each other's resources.

    # -- public API -----------------------------------------------------------

    async def crawl(
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
        """Crawl a URL and its links. ``mode``/``content``/``links`` accept
        ``"pure"`` or ``"ml"`` (see the module docstring)."""
        return await self.crawl_many(
            [url],
            mode=mode,
            content=content,
            links=links,
            topic=topic,
            schema=schema,
            session_id=session_id,
            max_depth=max_depth,
            overrides=overrides,
            ml_overrides=ml_overrides,
        )

    async def crawl_many(
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
        """Crawl multiple seed URLs sharing state (visited set, page counter)."""
        content_mode: Mode = content or mode
        link_mode: Mode = links or mode
        for which, m in (("content", content_mode), ("links", link_mode)):
            if m not in ("pure", "ml"):
                raise ValueError(
                    f"AsyncWebCrawler {which}={m!r} is not supported; use 'pure' or 'ml'. "
                    "For 'smart' (LLM) extraction use the synchronous WebCrawler."
                )
        eff_cfg = replace(self.cfg, **overrides) if overrides else self.cfg
        base_ml = self.ml_cfg or MLConfig()
        eff_ml_cfg = replace(base_ml, **ml_overrides) if ml_overrides else base_ml
        eff_depth = eff_cfg.max_depth if max_depth is None else max(0, int(max_depth))
        st = _State(
            content_mode=content_mode,
            link_mode=link_mode,
            topic=topic,
            session_id=session_id,
            schema=schema,
            max_depth=eff_depth,
            cfg=eff_cfg,
            ml_cfg=eff_ml_cfg,
        )
        # Fresh per-run worker resources (on the run state, not the instance).
        st.tls = threading.local()

        if self.db is not None:
            st.session_id = session_id or WebCrawler._default_session_id(topic, content_mode)
            self.db.create_session(
                st.session_id,
                topic=topic,
                seed=urls[0] if urls else "",
                mode=content_mode,
                source=source,
            )

        seeds = [
            (normalize_url(u), get_hostname(u))
            for u in urls
            if not is_blacklisted_domain(u, self.blacklist)
        ]
        workers = max(1, eff_cfg.max_workers)
        log.info(
            "async crawl: content=%s links=%s seeds=%d depth=%d max_pages=%d workers=%d",
            content_mode,
            link_mode,
            len(seeds),
            eff_depth,
            eff_cfg.max_pages,
            workers,
        )

        executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="lc-async")
        try:
            if link_mode == "ml" and eff_ml_cfg.best_first:
                await self._crawl_best_first(st, seeds, executor, workers)
            else:
                await self._crawl_bfs(st, seeds, executor, workers)
        finally:
            executor.shutdown(wait=True)
            self._close_worker_res(st)

        log.info("async crawl done: %d pages collected", len(st.results))
        return st.results

    # -- traversal strategies -------------------------------------------------

    async def _crawl_bfs(self, st, seeds, executor, workers: int) -> None:
        """Level-by-level BFS (links="pure", or ml with best_first disabled)."""
        frontier: List[Tuple[str, str, Optional[str]]] = [(u, dom, None) for (u, dom) in seeds]
        depth = 0
        sem = asyncio.Semaphore(workers)
        while frontier and not self._cap(st):
            tasks = [
                self._process(st, executor, url, depth, src, dom, sem)
                for (url, dom, src) in frontier
            ]
            results = await asyncio.gather(*tasks)
            if depth >= st.max_depth:
                break
            next_frontier: List[Tuple[str, str, Optional[str]]] = []
            seen_next: Set[str] = set()
            # zip preserves order, so each child carries its true parent URL/domain.
            for (parent_url, parent_dom, _src), links in zip(frontier, results, strict=False):
                for _score, _anchor, link_url in links or []:
                    nu = normalize_url(link_url)
                    if nu in seen_next:
                        continue
                    seen_next.add(nu)
                    next_frontier.append((link_url, parent_dom, parent_url))
            frontier = next_frontier
            depth += 1

    async def _crawl_best_first(self, st, seeds, executor, workers: int) -> None:
        """Best-first BFS (links="ml"): a globally score-ordered frontier, expanded
        ``workers`` pages at a time (async analogue of ``WebCrawler._crawl_ordered``)."""
        counter = itertools.count()
        heap: List[Tuple[float, int, int, str, Optional[str], str]] = []
        for url, dom in seeds:
            heapq.heappush(heap, (-1e9, 0, next(counter), url, None, dom))
        min_score = st.ml_cfg.min_link_score
        sem = asyncio.Semaphore(workers)
        while heap and not self._cap(st):
            wave = [heapq.heappop(heap) for _ in range(min(workers, len(heap)))]
            tasks = [
                self._process(st, executor, url, depth, src, dom, sem)
                for (_neg, depth, _cnt, url, src, dom) in wave
            ]
            results = await asyncio.gather(*tasks)
            for (_neg, parent_depth, _cnt, parent_url, _src, parent_dom), links in zip(
                wave, results, strict=False
            ):
                if parent_depth >= st.max_depth:
                    continue
                for score, _anchor, link_url in links or []:
                    if score < min_score:
                        continue
                    heapq.heappush(
                        heap,
                        (-score, parent_depth + 1, next(counter), link_url, parent_url, parent_dom),
                    )

    # -- per-URL processing ---------------------------------------------------

    async def _process(
        self,
        st,
        executor: ThreadPoolExecutor,
        url: str,
        depth: int,
        source_url: Optional[str],
        start_domain: str,
        sem: asyncio.Semaphore,
    ) -> List[Tuple[float, str, str]]:
        """Async pre-checks + non-blocking fetch, then delegate post-fetch work to
        the shared synchronous pipeline in a thread executor. Returns the selected
        links ``[(score, anchor, url)]`` for the next frontier."""
        async with sem:
            if self._cap(st):
                return []
            url = normalize_url(url)
            if is_blacklisted_domain(url, self.blacklist):
                return []
            if self.http_cfg.block_private_addresses and await _is_blocked_async(url):
                log.info("async SSRF guard: blocking %s", url)
                self._emit_status(
                    st,
                    url,
                    depth,
                    source_url,
                    "fetch_error",
                    "Blocked private/loopback address (SSRF guard)",
                )
                return []
            if not self._mark_visited(st, url):
                return []
            if self._robots is not None and not await self._robots.allowed(url):
                log.info("async: robots.txt disallows %s", url)
                self._emit_status(
                    st, url, depth, source_url, "robots_blocked", "Disallowed by robots.txt"
                )
                return []

            await self._rate.wait(url)
            afr = await self._http.fetch(url)

            # Robots gate on the post-redirect target (the sync pipeline does this
            # too; we run it on the async path because the pipeline has robots=None).
            final = normalize_url(afr.final_url or url)
            if final != url and self._robots is not None and not await self._robots.allowed(final):
                log.info("async: robots.txt disallows redirect target %s", final)
                self._emit_status(
                    st,
                    url,
                    depth,
                    source_url,
                    "robots_blocked",
                    "Disallowed by robots.txt (redirect target)",
                )
                return []

            fr = afr.to_fetch_result()
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                executor, self._run_pipeline, st, url, depth, source_url, start_domain, fr
            )

    def _run_pipeline(self, st, url, depth, source_url, start_domain, fr):
        """Executed in a worker thread: build/reuse per-thread resources and run
        the shared post-fetch pipeline."""
        res = self._worker_res(st)
        return self._pipeline.process_fetched(
            st, url, _url_hash(url), depth, source_url, start_domain, res, fr
        )

    # -- resources ------------------------------------------------------------

    def _build_res(self, st) -> _Res:
        http = HTTPClient(self.http_cfg)  # sync client: artifact byte downloads / PDFs
        ml = None
        if st.content_mode == "ml" or st.link_mode == "ml":
            from .ml import MLEngine

            ml = MLEngine(st.ml_cfg)
        selector = None
        if st.link_mode == "ml" and ml is not None:
            selector = ml.build_link_selector(
                st.topic or "general relevant content", st.cfg.max_links_per_level
            )
        return _Res(http, llm=None, ml=ml, link_selector=selector)

    def _worker_res(self, st) -> _Res:
        r = getattr(st.tls, "res", None)
        if r is None:
            r = self._build_res(st)
            st.tls.res = r
            with st.res_lock:
                st.created_res.append(r)
        return r

    def _close_worker_res(self, st) -> None:
        for r in st.created_res:
            try:
                r.http.close()
            except Exception:
                log.debug("failed closing a worker HTTP client", exc_info=True)
        st.created_res = []

    # -- state helpers (synchronous: shared _State uses a threading lock) ------

    @staticmethod
    def _cap(st) -> bool:
        with st.lock:
            return st.pages_done >= st.cfg.max_pages

    @staticmethod
    def _mark_visited(st, url: str) -> bool:
        with st.lock:
            if url in st.visited:
                return False
            st.visited.add(url)
            return True

    def _emit_status(self, st, url, depth, source_url, status, error) -> None:
        self._pipeline._emit(
            st,
            PageResult(
                url=url,
                url_hash=_url_hash(url),
                status=status,
                mode=st.content_mode,
                depth=depth,
                source_url=source_url,
                error=error,
            ),
            count=False,
        )

    async def close(self) -> None:
        # Per-run worker clients are closed in crawl_many's ``finally`` (they live on
        # the run state); here we only need to close the shared aiohttp session.
        await self._http.close()

    async def __aenter__(self) -> "AsyncWebCrawler":
        return self

    async def __aexit__(self, *exc) -> bool:
        await self.close()
        return False
