# -*- coding: utf-8 -*-
"""
lazycrawler.browser
===================
Optional headless-browser rendering via Playwright, for JavaScript-heavy pages
(SPAs, client-side-rendered content) that plain HTTP cannot capture.

Opt-in: HTTPConfig(render_js=True). Requires:
    pip install playwright
    playwright install chromium

If Playwright is not installed, render() returns None and the caller falls back
to a normal requests fetch.

Event-loop safety: Playwright's *sync* API cannot run inside a thread that has a
running asyncio loop (e.g. Spyder/Jupyter, or an async host). render() detects a
running loop and offloads the work to a fresh worker thread; in plain threads
(including LazyCrawler's parallel workers) it runs inline.

Note: this v1 launches a browser per call (simple and correct in every context).
Persisting a browser across calls is a future optimization.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from typing import Optional

from ._log import log

_WARNED = False


def playwright_available() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except Exception:
        return False


def _render_sync(
    url: str, user_agent: str, headless: bool, wait_until: str, timeout_ms: int
) -> Optional[str]:
    """Actual Playwright work. Must run in a thread with no running asyncio loop."""
    global _WARNED
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        if not _WARNED:
            log.warning("Playwright not installed - render_js falls back to requests "
                        "(pip install playwright && playwright install chromium)")
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
        log.warning("browser render failed for %s (%s: %s)",
                    url, type(e).__name__, e, exc_info=True)
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
    def work():
        return _render_sync(url, user_agent, headless, wait_until, timeout_ms)

    # If an asyncio loop is running in this thread, the sync API would raise;
    # offload to a fresh thread that has no loop.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return work()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(work).result()
