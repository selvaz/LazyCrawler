# -*- coding: utf-8 -*-
"""
lazycrawler.markdown
====================
Optional HTML -> Markdown rendering for RAG ingestion.

Renders headings, lists, tables and links (resolved against the page URL) to
Markdown. Uses ``markdownify`` when available (``pip install
lazycrawler[markdown]``); if it is absent, degrades gracefully to the basic
HTML-strip fallback so ``CrawlerConfig(emit_markdown=True)`` never hard-fails.

Pure function, no I/O — safe to import lazily and test in isolation.
"""

from __future__ import annotations

from urllib.parse import urljoin

from ._log import log

_WARNED = False


def _resolve_links(html: str, base_url: str) -> str:
    """Rewrite relative <a href> / <img src> to absolute URLs (best effort)."""
    if not base_url:
        return html
    try:
        from bs4 import BeautifulSoup
    except Exception:
        return html
    try:
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            a["href"] = urljoin(base_url, a["href"])
        for img in soup.find_all("img", src=True):
            img["src"] = urljoin(base_url, img["src"])
        return str(soup)
    except Exception:
        log.debug("markdown: link resolution failed - using raw html", exc_info=True)
        return html


def html_to_markdown(html: str, base_url: str = "") -> str:
    """
    Render HTML to Markdown for RAG ingestion.

    Headings, lists, tables and links are preserved; relative links are resolved
    against ``base_url``. Returns "" if ``html`` is empty. If ``markdownify`` is
    not installed, falls back to a basic HTML-to-text strip (logged once).
    """
    if not html or not html.strip():
        return ""
    global _WARNED
    try:
        from markdownify import markdownify as _md
    except ImportError:
        if not _WARNED:
            log.warning(
                "markdownify not installed - emit_markdown degrades to basic strip "
                "(pip install lazycrawler[markdown])"
            )
            _WARNED = True
        from .http import html_to_text_basic

        return html_to_text_basic(html)
    try:
        resolved = _resolve_links(html, base_url)
        md = _md(resolved, heading_style="ATX", strip=["script", "style"])
        return (md or "").strip()
    except Exception:
        log.warning("markdownify failed - falling back to basic strip", exc_info=True)
        from .http import html_to_text_basic

        return html_to_text_basic(html)
