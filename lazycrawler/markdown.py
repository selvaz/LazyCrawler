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

from typing import Any, List, Optional
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


# =============================================================================
# RAG RENDERING — recompose text + artifacts into one document
# =============================================================================


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from a PageResult/Artifact (attr) or a DB row (dict)."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _artifact_block(a: Any) -> str:
    """Render one artifact as a resolvable Markdown block keyed by its anchor."""
    from .artifacts import artifact_anchor

    chash = _get(a, "content_hash") or ""
    atype = _get(a, "artifact_type") or "artifact"
    caption = _get(a, "caption") or _get(a, "alt") or ""
    summary = _get(a, "summary") or ""
    head = f"### {artifact_anchor(chash)} · {atype}"
    if caption:
        head += f" — {caption}"
    parts: List[str] = [head]
    if summary:
        parts.append(summary)
    fmt = _get(a, "content_format")
    content = _get(a, "content")
    if atype == "table" and content:
        parts.append(content)  # Markdown table
    elif atype in ("image", "chart"):
        src = _get(a, "src_url")
        if src:
            parts.append(f"![{caption or atype}]({src})")
        dims = (_get(a, "width"), _get(a, "height"))
        if all(dims):
            parts.append(f"*(image {dims[0]}×{dims[1]})*")
    elif fmt == "svg":
        parts.append("*(inline SVG omitted)*")
    data = _get(a, "data")
    if atype == "chart" and data:
        parts.append(f"data: {data}")
    return "\n\n".join(p for p in parts if p)


def render_for_rag(page: Any, artifacts: Optional[List[Any]] = None) -> str:
    """
    Recompose a page's text and its artifacts into a single RAG-ready Markdown
    document (best practice: narrative with inline ``[[artifact:<hash>]]`` anchors,
    plus a resolvable "Artifacts" appendix that pairs each anchor with its table /
    image reference / vision summary).

    ``page`` may be a :class:`PageResult` or a DB page row (dict). ``artifacts``
    defaults to ``page.artifacts`` (PageResult) — pass ``db.get_artifacts(...)`` rows
    when recomposing from the database. Works whether or not Markdown anchoring was
    enabled at crawl time (anchors simply line up when it was).
    """
    body = _get(page, "markdown") or _get(page, "text") or _get(page, "clean_text") or ""
    title = _get(page, "title") or ""
    arts = artifacts if artifacts is not None else (_get(page, "artifacts") or [])

    sections: List[str] = []
    if title:
        sections.append(f"# {title}")
    if body:
        sections.append(body)
    if arts:
        sections.append("---\n\n## Artifacts")
        sections.extend(_artifact_block(a) for a in arts)
    return "\n\n".join(s for s in sections if s).strip()
