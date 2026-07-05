# -*- coding: utf-8 -*-
"""
lazycrawler.browser
===================
Optional headless-browser rendering via Playwright, for JavaScript-heavy pages
(SPAs, client-side-rendered content) that plain HTTP cannot capture.

Opt-in: HTTPConfig(render_js=True). Requires:
    pip install playwright
    playwright install chromium

If Playwright is not installed, rendering returns None and the caller falls back
to a normal requests fetch.

Event-loop safety: Playwright's *sync* API cannot run inside a thread that has a
running asyncio loop (e.g. Spyder/Jupyter, or an async host). BrowserRenderer
therefore owns a dedicated single worker thread and runs all Playwright work
there.

BrowserRenderer reuses a Playwright browser/context across calls. In parallel
crawls each worker owns its own HTTPClient, so browser reuse is naturally
thread-local.
"""

from __future__ import annotations

import concurrent.futures
import threading
from typing import Optional
from urllib.parse import urlparse

from ._log import log

_WARNED = False


def _is_web_url(url: str) -> bool:
    """True only for http/https URLs.

    Chromium happily loads ``file://``, ``chrome://``, ``view-source:`` etc.
    When ``render_js`` is on the SSRF guard is disabled (they are mutually
    exclusive), so the browser is the one fetch primitive that could otherwise
    read local files or internal pages from an attacker-supplied seed URL. We
    refuse any non-web scheme here as a hard boundary.
    """
    try:
        return urlparse(url).scheme in ("http", "https")
    except Exception:
        return False


def playwright_available() -> bool:
    try:
        import playwright  # noqa: F401

        return True
    except Exception:
        return False


class BrowserRenderer:
    """
    Reusable Playwright renderer.

    The sync Playwright API is thread-bound, so all rendering is routed through a
    dedicated single worker thread. That keeps reuse safe whether callers are in
    plain synchronous code, crawl worker threads, or an async host.
    """

    def __init__(
        self,
        *,
        user_agent: str = "Mozilla/5.0",
        headless: bool = True,
        wait_until: str = "domcontentloaded",
        timeout_ms: int = 30000,
    ):
        self.user_agent = user_agent
        self.headless = headless
        self.wait_until = wait_until
        self.timeout_ms = timeout_ms
        self._lock = threading.RLock()
        self._executor: concurrent.futures.ThreadPoolExecutor = (
            concurrent.futures.ThreadPoolExecutor(max_workers=1)
        )
        self._thread_id: Optional[int] = None
        self._playwright = None
        self._browser = None
        self._context = None

    def render(self, url: str) -> Optional[str]:
        """
        Render ``url`` and return HTML, or None when Playwright is unavailable
        or rendering fails.
        """
        if not _is_web_url(url):
            log.warning("browser render: refusing non-http(s) URL %s", url)
            return None
        return self._executor.submit(self._render_sync, url).result()

    def _ensure_session(self) -> bool:
        """Open a browser/context for the current thread if needed."""
        global _WARNED
        tid = threading.get_ident()
        if self._context is not None and self._thread_id == tid:
            return True
        if self._context is not None:
            self._close_sync()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            if not _WARNED:
                log.warning(
                    "Playwright not installed - render_js falls back to requests "
                    "(pip install playwright && playwright install chromium)"
                )
                _WARNED = True
            return False
        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=self.headless)
            self._context = self._browser.new_context(user_agent=self.user_agent)
            self._thread_id = tid
            return True
        except Exception as e:
            log.warning("browser startup failed (%s: %s)", type(e).__name__, e, exc_info=True)
            self._close_sync()
            return False

    def _render_sync(self, url: str) -> Optional[str]:
        """Actual Playwright work. Must run in the renderer's owning thread."""
        with self._lock:
            if not self._ensure_session():
                return None
            page = None
            try:
                page = self._context.new_page()
                page.goto(url, wait_until=self.wait_until, timeout=self.timeout_ms)
                return page.content()
            except Exception as e:
                log.warning(
                    "browser render failed for %s (%s: %s)", url, type(e).__name__, e, exc_info=True
                )
                # A crashed browser/context should not poison future renders.
                self._close_sync()
                return None
            finally:
                if page is not None:
                    try:
                        page.close()
                    except Exception:
                        log.debug("failed closing browser page", exc_info=True)

    def close(self) -> None:
        """Close the browser/context and any async-loop helper executor."""
        executor = self._executor
        if self._thread_id is not None:
            try:
                executor.submit(self._close_sync).result(timeout=10)
            except Exception:
                log.debug("failed closing browser renderer in worker", exc_info=True)
        else:
            self._close_sync()
        executor.shutdown(wait=True)

    def _close_sync(self) -> None:
        for obj, name in (
            (self._context, "context"),
            (self._browser, "browser"),
            (self._playwright, "playwright"),
        ):
            if obj is None:
                continue
            try:
                if name == "playwright":
                    obj.stop()
                else:
                    obj.close()
            except Exception:
                log.debug("failed closing browser %s", name, exc_info=True)
        self._context = None
        self._browser = None
        self._playwright = None
        self._thread_id = None


def _render_sync(
    url: str, user_agent: str, headless: bool, wait_until: str, timeout_ms: int
) -> Optional[str]:
    """Compatibility helper: render one URL with a short-lived renderer."""
    global _WARNED
    if not _is_web_url(url):
        log.warning("browser render: refusing non-http(s) URL %s", url)
        return None
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        if not _WARNED:
            log.warning(
                "Playwright not installed - render_js falls back to requests "
                "(pip install playwright && playwright install chromium)"
            )
            _WARNED = True
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            try:
                page = browser.new_page(user_agent=user_agent)
                page.goto(url, wait_until=wait_until, timeout=timeout_ms)
                return page.content()
            finally:
                browser.close()
    except Exception as e:
        log.warning(
            "browser render failed for %s (%s: %s)", url, type(e).__name__, e, exc_info=True
        )
        return None


def render(
    url: str,
    *,
    user_agent: str = "Mozilla/5.0",
    headless: bool = True,
    wait_until: str = "domcontentloaded",
    timeout_ms: int = 30000,
) -> Optional[str]:
    """
    Render ``url`` in a headless browser and return its HTML, or None on failure
    (including when Playwright is unavailable).
    """
    renderer = BrowserRenderer(
        user_agent=user_agent,
        headless=headless,
        wait_until=wait_until,
        timeout_ms=timeout_ms,
    )

    def work():
        return renderer.render(url)

    try:
        return work()
    finally:
        renderer.close()
