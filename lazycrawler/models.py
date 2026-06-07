# -*- coding: utf-8 -*-
"""
lazycrawler.models
==================
Public output types for LazyCrawler.

``PageResult`` was previously defined in ``crawler.py``; it lives here so
callers can import it without pulling the full crawl engine. Backward compat:
``from lazycrawler.crawler import PageResult`` and ``from lazycrawler import
PageResult`` both continue to work (crawler.py re-exports this).
"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

Mode = Literal["pure", "ml", "smart"]
Status = Literal["done", "fetch_error", "no_text", "llm_error", "blacklisted", "robots_blocked"]


class PageResult(BaseModel):
    """Result of crawling a single page."""

    url: str
    url_hash: str = ""
    status: Status = "done"
    mode: Mode = "pure"  # content mode that produced this result
    title: Optional[str] = None
    text: Optional[str] = None  # clean text (pure: cleaned; smart: LLM clean_text)
    summary: Optional[str] = None  # smart content only
    entities: List[str] = Field(default_factory=list)
    topics: List[str] = Field(default_factory=list)
    sentiment: Optional[str] = None  # smart: negative|neutral|positive
    notes: Optional[str] = None  # smart: reserved research tags/notes
    data: Optional[dict] = None  # full structured object (custom schema)
    published_iso: Optional[str] = None
    is_pdf: bool = False
    depth: int = 0
    source_url: Optional[str] = None
    requested_url: Optional[str] = None  # original URL when a redirect was adopted
    error: Optional[str] = None
    from_cache: bool = False
    markdown: Optional[str] = None  # optional HTML->Markdown render (emit_markdown)
    artifacts: List = Field(default_factory=list)  # tables/images/charts (Artifact)
