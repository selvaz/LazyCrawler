# -*- coding: utf-8 -*-
"""
lazycrawler.config
==================
Configuration dataclasses for each LazyCrawler component.

Philosophy:
- each class holds ONLY the parameters relevant to its own component
- no domain-specific parameters (this is a generic crawler, not finance/news)
- LLMs are ALWAYS built via LazyBridge: to switch provider/model just change
  the ``LLMConfig.model`` string (e.g. "gpt-4o-mini" -> "claude-haiku-4-5" ->
  "gemini-3-flash-preview"). The provider is inferred automatically.

Typical use:
    from lazycrawler.config import CrawlerConfig, HTTPConfig, LLMConfig, DBConfig

    crawler_cfg = CrawlerConfig(max_depth=2, max_pages=20)
    http_cfg    = HTTPConfig(link_delay=1.0)
    llm_cfg     = LLMConfig(model="claude-haiku-4-5")
    db_cfg      = DBConfig(db_path="lazycrawler.db", ttl_hours=24)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Literal, Optional

# =============================================================================
# CrawlerConfig
# =============================================================================


@dataclass
class CrawlerConfig:
    """
    Configuration for the recursive crawl engine.

    Attributes
    ----------
    max_depth : int
        Maximum recursion depth. 0 = only the seed URLs (no internal links).
    max_pages : int
        Hard cap on successfully extracted pages per run.
    max_links_per_level : int
        Max links followed per page (after heuristic/LLM selection).
    max_candidate_links : int
        Max candidate links extracted from a page before filtering.
    same_domain_only : bool
        If True, only follow links within the source page's domain.
    max_workers : int
        Concurrency. 1 = sequential DFS (default). N>1 = native parallel mode:
        a bounded thread pool crawls level-by-level (BFS) with N workers.
        In parallel mode the per-fetch link_delay is not applied (parallelism
        replaces it); use a polite max_workers for shared/target sites.
    respect_robots : bool
        Honor each host's robots.txt (default True). URLs disallowed for the
        configured User-Agent are skipped (emitted with status='robots_blocked').
        Set False to ignore robots.txt (authorized crawling of your own sites).
    strict : bool
        If True, per-page/worker exceptions propagate (fail-fast) instead of
        being logged-and-skipped. Default False (resilient: log and continue).
        Either way exceptions are never silently swallowed — they are logged.
    max_chars_content : int
        Max characters of text sent to the LLM (smart mode).
    max_chars_pure : int
        Max characters of text returned in pure mode.
    large_doc_threshold : int
        Character count above which map-reduce summarization kicks in
        (smart mode only). In pure mode the text is truncated to max_chars_pure.
    large_doc_chunk_chars : int
        Size of each chunk for map-reduce summarization.
    large_doc_max_chunks : int
        Maximum number of chunks processed (cost cap).
    recurse_from_cache : bool
        If True, when a page is served from the cache its stored candidate links
        are followed (recursion continues) instead of stopping. This makes the
        result set independent of whether the DB is warm or cold. Default False
        (a cache hit is terminal — the historical behavior).
    emit_markdown : bool
        If True, also render each crawled HTML page to Markdown (headings, lists,
        tables, links as citations) for RAG ingestion. Populated on
        ``PageResult.markdown`` and persisted. Requires the ``markdown`` extra
        (``pip install lazycrawler[markdown]``); degrades to a basic strip if the
        renderer is unavailable. PDFs are skipped (no HTML). Default False.
    extract_artifacts : bool
        If True, extract non-textual content — tables, images, figures, charts,
        inline SVG — as structured ``Artifact`` records (``PageResult.artifacts``,
        persisted to the ``artifacts`` table). Works on HTML and, with the pdf
        extra, on PDFs (tables via pdfplumber, images via PyMuPDF). Default False.
    artifact_types : tuple[str, ...]
        Which artifact types to collect (subset of table/image/figure/svg/chart).
    download_artifact_bytes : bool
        If True, download image/chart bytes through the crawler's HTTP client
        (honors SSL + SSRF guard), store a sha256 hash and the bytes (size-capped)
        in the DB. Default False (reference-only: URL + alt + caption + context).
    max_artifact_bytes : int
        Max image size to store as a blob (larger images keep hash/metadata only).
    min_image_dim : int
        Drop images whose declared width/height is below this (filters icons).
    artifact_context_chars : int
        Characters of surrounding text captured for images lacking a caption.
    max_artifacts_per_page : int
        Hard cap on artifacts collected per page.
    same_domain_images : bool
        If True, keep only images hosted on the page's own domain.
    enrich_artifacts : bool
        If True and content="smart", enrich artifacts with a vision LLM (image
        caption / chart data / table summary) via LazyBridge. Default False.
    max_artifacts_to_enrich : int
        Per-page cap on LLM-enriched artifacts (cost control).
    markdown_artifact_anchors : bool
        If True (with emit_markdown + extract_artifacts), replace each table/image
        in the rendered Markdown with an inline ``[[artifact:<hash>]]`` placeholder
        instead of duplicating it. Keeps narrative + position, externalizes the
        heavy/structured content to the ``artifacts`` table; ``render_for_rag()``
        recomposes the two for RAG ingestion. Default False (tables/images stay
        inline). HTML only.
    exclude_patterns : list[str] | None
        Regex fragments used to drop uninteresting links during crawling. None
        uses the built-in default (login/cart/checkout/account, tracking,
        social, mailto/tel). Unlike older versions this default no longer drops
        /about, /contact, /tag/, /category/ or /author/ — for a generic crawler
        those are often real content. Pass a custom list to fully override.
    blacklist : list[str]
        Domains to always skip (e.g. ["facebook.com", "x.com"]).
    blacklist_excel : str
        Optional path to an .xlsx file to load the blacklist from.
    blacklist_excel_sheet : str | None
        Excel sheet (None = first sheet).
    blacklist_excel_column : str | None
        Excel column holding the domains (None = autodetect / first column).
    """

    max_depth: int = 2
    max_pages: int = 20
    max_links_per_level: int = 15
    max_candidate_links: int = 300
    same_domain_only: bool = True
    max_workers: int = 1
    respect_robots: bool = True
    strict: bool = False
    recurse_from_cache: bool = False

    emit_markdown: bool = False

    # -- artifacts (tables / images / figures / charts / svg) --
    extract_artifacts: bool = False
    artifact_types: tuple = ("table", "image", "figure", "svg", "chart")
    download_artifact_bytes: bool = False
    max_artifact_bytes: int = 5_000_000
    min_image_dim: int = 48
    artifact_context_chars: int = 200
    max_artifacts_per_page: int = 100
    same_domain_images: bool = False
    enrich_artifacts: bool = False
    max_artifacts_to_enrich: int = 8
    markdown_artifact_anchors: bool = False

    max_chars_content: int = 100_000
    max_chars_pure: int = 10_000

    large_doc_threshold: int = 20_000
    large_doc_chunk_chars: int = 12_000
    large_doc_max_chunks: int = 12

    exclude_patterns: Optional[List[str]] = None
    blacklist: List[str] = field(default_factory=list)
    blacklist_excel: str = ""
    blacklist_excel_sheet: Optional[str] = None
    blacklist_excel_column: Optional[str] = None


# =============================================================================
# HTTPConfig
# =============================================================================


@dataclass
class HTTPConfig:
    """
    Configuration for the HTTP client used to fetch pages.

    Attributes
    ----------
    user_agent : str
        User-agent sent with every request.
    timeout_connect : int
        TCP connect timeout (seconds).
    timeout_read : int
        Response read timeout (seconds).
    max_retries : int
        Attempts for HTTP 429/5xx and network errors.
    backoff_base_sec : float
        Base backoff; actual wait = backoff_base_sec * 2^(attempt-1).
    link_delay : float
        Pause (seconds) between consecutive fetches in *sequential* mode. Not
        applied in parallel mode — use ``per_host_delay`` for politeness there.
    per_host_delay : float
        Minimum seconds between two fetches to the *same host*. Applied in BOTH
        sequential and parallel mode (a per-host rate limiter). 0 disables it.
        robots.txt ``Crawl-delay`` is always honored on top of this when
        ``respect_robots`` is on, so the effective delay is the larger of the two.
    min_text_chars : int
        Minimum length (characters) for extracted text to be accepted. Shorter
        pages (docs, changelogs, landing pages) are no longer discarded as
        ``no_text``. Default 50 (was an implicit 200 in older versions).
    pdf_timeout : int
        Dedicated timeout for PDF downloads (usually larger).
    verify_ssl : bool
        TLS certificate verification. Default True (secure). Set False only in
        environments with SSL inspection / MITM (e.g. antivirus such as Avast,
        corporate proxies) that present a root cert Python does not recognize.
    ca_bundle : str
        Optional path to a custom CA bundle (.pem). If set, it takes precedence
        over verify_ssl (this is the *secure* way to handle a MITM: point at the
        antivirus/proxy cert instead of disabling verification).
    render_js : bool
        If True, fetch HTML through a headless browser (Playwright) so that
        client-side-rendered pages (SPAs, dynamic content) are captured. Requires
        ``pip install playwright`` + ``playwright install chromium``. Falls back to
        plain requests if Playwright is unavailable.
    browser_headless : bool
        Run the browser headless (default True).
    browser_wait_until : str
        Playwright wait condition: "load" | "domcontentloaded" | "networkidle".
    browser_timeout_ms : int
        Per-page navigation timeout for the browser (milliseconds).
    block_private_addresses : bool
        SSRF guard. If True, refuse to fetch URLs that resolve to loopback,
        link-local, private (RFC-1918), reserved, multicast or unspecified
        addresses, plus ``localhost`` / ``*.local`` / cloud metadata endpoints
        (e.g. 169.254.169.254). Default False for library use (so localhost
        crawling and the offline tests keep working); ``CrawlerTools`` turns it
        ON by default because an agent may pass arbitrary URLs. Note: a public
        host that redirects to a private one is not caught (requests follows
        redirects internally).
    """

    user_agent: str = "LazyCrawler/0.8 (+https://github.com/selvaz/lazycrawler)"
    timeout_connect: int = 5
    timeout_read: int = 25
    max_retries: int = 4
    backoff_base_sec: float = 1.0
    link_delay: float = 1.0
    per_host_delay: float = 0.0
    min_text_chars: int = 50
    pdf_timeout: int = 60
    verify_ssl: bool = True
    ca_bundle: str = ""
    render_js: bool = False
    browser_headless: bool = True
    browser_wait_until: str = "domcontentloaded"
    browser_timeout_ms: int = 30000
    block_private_addresses: bool = False


# =============================================================================
# LLMConfig  (always via LazyBridge)
# =============================================================================


@dataclass
class LLMConfig:
    """
    LLM configuration for smart mode. Every call goes through LazyBridge: to
    switch provider/model just change ``model``.

    Example model strings (provider inferred by LazyBridge):
        "gpt-4o-mini"            -> OpenAI
        "claude-haiku-4-5"       -> Anthropic
        "gemini-3-flash-preview" -> Google
        "deepseek-chat"          -> DeepSeek

    Attributes
    ----------
    model : str
        Main model for content extraction and link selection.
    large_doc_model : str
        Model (usually cheaper) for large-document summarization.
        Empty string = use ``model``.
    vision_model : str
        Vision-capable model for artifact enrichment (image caption / chart data).
        Empty string = use ``model`` (which must then support vision).
    temperature : float
        Sampling temperature. LazyBridge handles models that do not support it
        (e.g. reasoning models).
    request_timeout : float
        Timeout (seconds) for each LLM call.
    max_links_excerpt_chars : int
        Characters of page excerpt sent to the LLM for link selection.
    max_candidates_to_llm : int
        Maximum candidate links passed to the LLM for selection.
    """

    model: str = "gpt-4o-mini"
    large_doc_model: str = ""
    vision_model: str = ""  # model for artifact vision enrichment ("" = use model)
    temperature: float = 0.0
    request_timeout: float = 120.0
    max_links_excerpt_chars: int = 3_000
    max_candidates_to_llm: int = 80


# =============================================================================
# SearchConfig
# =============================================================================


@dataclass
class SearchConfig:
    """
    Configuration for WebSearch (a crawler seeded from search results).

    Attributes
    ----------
    engine : str
        Search engine: "duckduckgo" (default, no LLM cost for the search step)
        or "gemini" (grounded answer via LazyBridge native search).
    n_results : int
        Number of URLs to obtain from the search engine.
    crawl_depth : int
        Crawl depth applied to each found URL.
        0 = crawl only the direct URL (recommended for search).
    same_domain_only : bool
        If True, for each result follow only links on the same domain.
    expand_topic : bool
        If True (smart mode), expand the query via LLM into a topic description
        used for link selection during the crawl.
    gemini_model : str
        LazyBridge model for grounded search (engine="gemini").
    region : str
        DuckDuckGo region code (e.g. "us-en", "wt-wt" for no region). Default
        "wt-wt".
    timelimit : str | None
        DuckDuckGo time filter: "d" (day), "w" (week), "m" (month), "y" (year),
        or None for no limit.
    safesearch : str
        DuckDuckGo safe-search level: "on", "moderate", or "off".
    backend : str
        DuckDuckGo backend selection passed through to ddgs (e.g. "auto").
    """

    engine: Literal["duckduckgo", "gemini"] = "duckduckgo"
    n_results: int = 10
    crawl_depth: int = 0
    same_domain_only: bool = False
    expand_topic: bool = True
    gemini_model: str = "gemini-3-flash-preview"
    region: str = "wt-wt"
    timelimit: Optional[str] = None
    safesearch: str = "moderate"
    backend: str = "auto"


# =============================================================================
# DBConfig
# =============================================================================


@dataclass
class DBConfig:
    """
    SQLite database configuration.

    Attributes
    ----------
    db_path : str
        Path to the SQLite file.
    ttl_hours : float
        Page cache time-to-live: a 'done' page newer than ttl_hours is reused
        instead of being re-fetched.
    force_refresh : bool
        If True, ignore the cache and always re-fetch/re-process.
    enable_fts : bool
        If True, build and maintain the full-text (FTS5) index on clean_text.
    """

    db_path: str = "lazycrawler.db"
    ttl_hours: float = 24.0
    force_refresh: bool = False
    enable_fts: bool = True
