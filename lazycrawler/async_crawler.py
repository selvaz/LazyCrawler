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
Only ``content="pure"`` is currently supported (no LLM / ML in the async path).
Smart/ML extraction can be layered on top by post-processing ``PageResult``
objects with ``CrawlerLLM`` or ``MLEngine`` after the crawl completes.

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
import ipaddress
import re
import socket
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import List, Optional, Set, Tuple
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

from ._log import log
from .config import CrawlerConfig, HTTPConfig
from .http import (
    compile_exclude,
    get_base_domain,
    get_hostname,
    is_blacklisted_domain,
    normalize_url,
)
from .http import url_hash as _url_hash
from .models import PageResult

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
    """Async SSRF guard: resolves DNS in a thread executor, checks every IP."""
    host = get_hostname(url)
    if not host:
        return True
    if host == "localhost" or host.endswith(".local") or host in {"metadata", "metadata.google.internal"}:
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
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
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


class _AsyncHTTPClient:
    """Thin aiohttp-based fetch client."""

    def __init__(self, cfg: HTTPConfig):
        self.cfg = cfg
        self._session: Optional[aiohttp.ClientSession] = None

    async def _ensure(self) -> "aiohttp.ClientSession":
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(ssl=self.cfg.verify_ssl)
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
                session = await self._ensure()
                async with session.get(
                    url, max_redirects=cfg.max_redirects, allow_redirects=True
                ) as resp:
                    status = resp.status
                    if status in (429, 500, 502, 503, 504):
                        raise aiohttp.ClientResponseError(
                            resp.request_info, resp.history, status=status
                        )
                    if 400 <= status < 600:
                        return _AsyncFetchResult(status=status)
                    content_type = resp.headers.get("Content-Type", "").lower()
                    if "application/pdf" in content_type or url.lower().endswith(".pdf"):
                        return _AsyncFetchResult(status=status)  # skip PDFs in async mode
                    body = await resp.read()
                    if len(body) > cfg.max_html_bytes:
                        body = body[: cfg.max_html_bytes]
                    enc = None
                    if "charset=" in content_type:
                        enc = content_type.split("charset=")[-1].split(";")[0].strip() or None
                    html = body.decode(enc or "utf-8", errors="replace")
                    text = self._extract(html)
                    return _AsyncFetchResult(html=html, text=text, status=status)
            except Exception as exc:
                if attempt < cfg.max_retries:
                    await asyncio.sleep(cfg.backoff_base_sec * (2 ** (attempt - 1)))
                else:
                    log.warning("async fetch failed for %s after %d attempts: %s", url, attempt, exc)
                    return _AsyncFetchResult()
        return _AsyncFetchResult()

    @staticmethod
    def _extract(html: str) -> Optional[str]:
        if _TRAFILATURA_OK:
            try:
                out = trafilatura.extract(html, include_comments=False, favor_recall=True)
                if out and len(out.strip()) >= 50:
                    return out.strip()
            except Exception:
                pass
        # basic strip fallback
        s = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "", html)
        s = re.sub(r"(?is)<.*?>", " ", s)
        s = re.sub(r"\s+", " ", s).strip()
        return s if len(s) >= 50 else None

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
# PER-RUN STATE
# =============================================================================


@dataclass
class _AsyncState:
    topic: str
    max_depth: int
    cfg: CrawlerConfig
    visited: Set[str] = field(default_factory=set)
    visited_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    results: List[PageResult] = field(default_factory=list)
    results_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pages_done: int = 0


# =============================================================================
# ASYNC WEB CRAWLER
# =============================================================================


class AsyncWebCrawler:
    """
    High-throughput async web crawler (pure mode).

    .. note::
       ``block_private_addresses`` defaults to ``True`` in the async crawler
       (unlike the sync ``WebCrawler``). Pass ``HTTPConfig(block_private_addresses=False)``
       explicitly for internal/intranet use.

    Requirements: ``pip install aiohttp`` (or ``pip install lazycrawler[async]``).
    """

    def __init__(
        self,
        crawler_cfg: Optional[CrawlerConfig] = None,
        http_cfg: Optional[HTTPConfig] = None,
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
        if not raw_http.block_private_addresses:
            from dataclasses import replace
            self.http_cfg = replace(raw_http, block_private_addresses=True)
            log.debug("AsyncWebCrawler: block_private_addresses enabled by default")
        else:
            self.http_cfg = raw_http
        self.blacklist = list(self.cfg.blacklist)
        self._exclude_re = compile_exclude(self.cfg.exclude_patterns)
        self._http = _AsyncHTTPClient(self.http_cfg)
        self._robots: Optional[_AsyncRobotsChecker] = (
            _AsyncRobotsChecker(self._http, self.http_cfg.user_agent)
            if self.cfg.respect_robots else None
        )
        self._rate = _AsyncRateLimiter(self.http_cfg.per_host_delay)

    async def crawl(
        self,
        url: str,
        *,
        topic: str = "",
        max_depth: Optional[int] = None,
        session_id: Optional[str] = None,
    ) -> List[PageResult]:
        """Crawl a URL and its links (pure mode, no LLM)."""
        return await self.crawl_many(
            [url], topic=topic, max_depth=max_depth, session_id=session_id
        )

    async def crawl_many(
        self,
        urls: List[str],
        *,
        topic: str = "",
        max_depth: Optional[int] = None,
        session_id: Optional[str] = None,
    ) -> List[PageResult]:
        """Crawl multiple seed URLs sharing state (pure mode, no LLM)."""
        eff_depth = self.cfg.max_depth if max_depth is None else max(0, int(max_depth))
        st = _AsyncState(topic=topic, max_depth=eff_depth, cfg=self.cfg)
        seeds = [u for u in urls if not is_blacklisted_domain(u, self.blacklist)]
        log.info(
            "async crawl: seeds=%d depth=%d max_pages=%d workers=%d",
            len(seeds), eff_depth, self.cfg.max_pages, self.cfg.max_workers,
        )
        # BFS level-by-level with concurrency limited by max_workers
        frontier = [(normalize_url(u), get_base_domain(u), None) for u in seeds]
        depth = 0
        sem = asyncio.Semaphore(max(1, self.cfg.max_workers))
        while frontier and not await self._cap(st):
            tasks = [
                self._process(st, url, depth, src, dom, sem)
                for (url, dom, src) in frontier
            ]
            next_lists = await asyncio.gather(*tasks, return_exceptions=False)
            seen_next: Set[str] = set()
            frontier = []
            if depth < eff_depth:
                for links in next_lists:
                    for link_url, link_dom in (links or []):
                        nu = normalize_url(link_url)
                        if nu not in seen_next:
                            seen_next.add(nu)
                            frontier.append((link_url, link_dom, None))
            depth += 1
            if depth > eff_depth:
                break
        log.info("async crawl done: %d pages collected", len(st.results))
        return st.results

    async def _cap(self, st: _AsyncState) -> bool:
        async with st.results_lock:
            return st.pages_done >= st.cfg.max_pages

    async def _mark_visited(self, st: _AsyncState, url: str) -> bool:
        async with st.visited_lock:
            if url in st.visited:
                return False
            st.visited.add(url)
            return True

    async def _process(
        self, st: _AsyncState, url: str, depth: int, source_url: Optional[str],
        start_domain: str, sem: asyncio.Semaphore,
    ) -> List[Tuple[str, str]]:
        """Process one URL; return (url, domain) pairs for the next frontier."""
        async with sem:
            return await self._do_process(st, url, depth, source_url, start_domain)

    async def _do_process(
        self, st: _AsyncState, url: str, depth: int, source_url: Optional[str], start_domain: str
    ) -> List[Tuple[str, str]]:
        if await self._cap(st):
            return []
        url = normalize_url(url)
        if is_blacklisted_domain(url, self.blacklist):
            return []
        if self.http_cfg.block_private_addresses and await _is_blocked_async(url):
            log.info("async SSRF guard: blocking %s", url)
            return []
        if not await self._mark_visited(st, url):
            return []

        # robots.txt
        if self._robots is not None and not await self._robots.allowed(url):
            log.info("async: robots.txt disallows %s", url)
            async with st.results_lock:
                st.results.append(PageResult(
                    url=url, url_hash=_url_hash(url), status="robots_blocked",
                    mode="pure", depth=depth, source_url=source_url,
                    error="Disallowed by robots.txt",
                ))
            return []

        await self._rate.wait(url)
        fr = await self._http.fetch(url)
        if not fr.html and not fr.text:
            async with st.results_lock:
                st.results.append(PageResult(
                    url=url, url_hash=_url_hash(url), status="fetch_error",
                    mode="pure", depth=depth, source_url=source_url,
                    error=f"Fetch failed (status={fr.status})",
                ))
            return []

        cfg = st.cfg
        page = PageResult(
            url=url, url_hash=_url_hash(url), status="done", mode="pure",
            text=(fr.text or "")[: cfg.max_chars_pure] or None,
            depth=depth, source_url=source_url,
        )
        async with st.results_lock:
            if st.pages_done >= cfg.max_pages:
                return []
            st.results.append(page)
            st.pages_done += 1
        log.info("[d%d | p%d] %s", depth, st.pages_done, url[:90])

        if depth >= st.max_depth or not fr.html:
            return []

        # link extraction
        from .text import extract_candidate_links
        candidates = extract_candidate_links(
            fr.html, url, start_domain,
            same_domain_only=cfg.same_domain_only,
            max_links=cfg.max_candidate_links,
            exclude_pattern=self._exclude_re,
            same_host_only=cfg.same_host_only,
        )
        async with st.visited_lock:
            visited_snap = set(st.visited)
        links = [
            (u, get_base_domain(u)) for (_a, u) in candidates[: cfg.max_links_per_level]
            if normalize_url(u) not in visited_snap
            and not is_blacklisted_domain(u, self.blacklist)
        ]
        return links

    async def close(self) -> None:
        await self._http.close()

    async def __aenter__(self) -> "AsyncWebCrawler":
        return self

    async def __aexit__(self, *exc) -> bool:
        await self.close()
        return False
