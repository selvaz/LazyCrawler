# -*- coding: utf-8 -*-
"""
lazycrawler.presets
===================
Named, intent-level crawl presets the agent can pick by name.

A preset is **not** a raw knob — it is an *intent* ("quick lookup", "deep
research", "RAG ingestion") that bundles a ready-made configuration: how content
is extracted (pure/smart), whether links are followed and how (heuristic/LLM),
crawl depth, page/result caps, artifact extraction, Markdown output and search
recency, plus a coarse ``cost`` hint.

This keeps the LLM-facing tool schema simple: the model reasons about *what it
wants to do*, not about content/link/artifact/markdown flags. ``CrawlerTools``
exposes the catalog through ``list_presets()`` and accepts ``preset=<name>`` on
``web_search`` / ``web_crawl``.

The built-in catalog (:data:`DEFAULT_PRESETS`) can be extended or overridden per
``CrawlerTools(presets={...})`` — see :func:`resolve_presets`.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

# Coarse cost hint shown to the model (drives its pure/smart trade-off intuition).
Cost = str  # "minimal" | "low" | "medium" | "high"


@dataclass(frozen=True)
class CrawlPreset:
    """
    One named, ready-made crawl configuration.

    Only the fields that matter for an *intent* are exposed; everything else
    falls back to the crawler's own ``CrawlerConfig`` / ``SearchConfig``.

    Attributes
    ----------
    name : str
        Stable identifier passed as ``preset=`` (e.g. "deep_research").
    description : str
        One-line intent description shown to the LLM (when to pick this preset).
    cost : str
        Coarse cost hint: "minimal" | "low" | "medium" | "high".
    content : str
        Content mode for this preset: "pure" (no LLM) or "smart" (LLM extract).
    links : str
        Link mode: "pure" (heuristic) or "smart" (LLM relevance ranking).
    max_depth : int
        Crawl depth (0 = only the seed/result URLs).
    max_pages : int
        Hard cap on extracted pages for the run.
    max_links_per_level : int | None
        Branching factor: links followed **per page** (see
        ``CrawlerConfig.max_links_per_level``). None = keep the crawler's own
        default (only meaningful when ``max_depth > 0``).
    max_results : int
        Default number of search results (web_search only).
    extract_artifacts : bool
        Extract tables/images/figures/charts as structured artifacts.
    artifact_types : tuple[str, ...]
        Which artifact types to collect when ``extract_artifacts`` is on.
    emit_markdown : bool
        Also render each HTML page to Markdown (RAG ingestion).
    markdown_artifact_anchors : bool
        Replace tables/images in the Markdown with ``[[artifact:<hash>]]``
        anchors (externalized artifacts; needs emit_markdown + extract_artifacts).
    timelimit : str | None
        Search recency filter: "d" (day), "w" (week), "m" (month), "y" (year),
        or None for no limit (web_search only).
    """

    name: str
    description: str
    cost: Cost = "low"
    content: str = "pure"
    links: str = "pure"
    max_depth: int = 0
    max_pages: int = 5
    max_links_per_level: Optional[int] = None
    max_results: int = 8
    extract_artifacts: bool = False
    artifact_types: Tuple[str, ...] = ("table", "image", "figure", "svg", "chart")
    emit_markdown: bool = False
    markdown_artifact_anchors: bool = False
    timelimit: Optional[str] = None

    def crawl_overrides(self) -> Dict[str, object]:
        """The ``CrawlerConfig`` fields this preset overrides for a single run.

        Returned as a dict consumed by ``WebCrawler.crawl(..., overrides=...)``;
        content/links/depth and search recency are passed separately (they are
        not ``CrawlerConfig`` fields).
        """
        overrides: Dict[str, object] = {
            "max_pages": self.max_pages,
            "extract_artifacts": self.extract_artifacts,
            "artifact_types": self.artifact_types,
            "emit_markdown": self.emit_markdown,
            "markdown_artifact_anchors": self.markdown_artifact_anchors,
        }
        # Only override the branching factor when the preset sets it (else keep
        # the crawler's own max_links_per_level).
        if self.max_links_per_level is not None:
            overrides["max_links_per_level"] = self.max_links_per_level
        return overrides

    def brief(self) -> Dict[str, object]:
        """Compact, LLM-friendly view of the preset (for ``list_presets``)."""
        return {
            "name": self.name,
            "intent": self.description,
            "cost": self.cost,
            "content": self.content,
            "follows_links": self.max_depth > 0,
            "link_mode": self.links,
            "depth": self.max_depth,
            "links_per_page": self.max_links_per_level,
            "artifacts": self.extract_artifacts,
            "markdown": self.emit_markdown,
            "recency": self.timelimit,
        }


# =============================================================================
# BUILT-IN CATALOG
# =============================================================================

DEFAULT_PRESETS: Dict[str, CrawlPreset] = {
    "quick_lookup": CrawlPreset(
        name="quick_lookup",
        description=(
            "Fast, cheap lookup: clean text of the top results, no link-following, "
            "no LLM. Use for a quick factual check or to grab a page's text."
        ),
        cost="minimal",
        content="pure",
        links="pure",
        max_depth=0,
        max_pages=5,
        max_results=6,
    ),
    "deep_research": CrawlPreset(
        name="deep_research",
        description=(
            "Thorough multi-source research: LLM extraction (summary, entities, "
            "sentiment) and LLM-ranked link-following from each source. Use when "
            "the question needs depth across several pages."
        ),
        cost="high",
        content="smart",
        links="smart",
        max_depth=1,
        max_pages=20,
        max_links_per_level=25,  # wide branching: chase more links from each source
        max_results=12,
    ),
    "news_scan": CrawlPreset(
        name="news_scan",
        description=(
            "Recent-news sweep: LLM extraction with sentiment and date, limited to "
            "the last week, no link-following, more results. Use for current "
            "events or monitoring."
        ),
        cost="medium",
        content="smart",
        links="pure",
        max_depth=0,
        max_pages=15,
        max_results=15,
        timelimit="w",
    ),
    "extract_data": CrawlPreset(
        name="extract_data",
        description=(
            "Structured-data extraction: pull tables and images off a page as "
            "artifacts (no link-following, no LLM). Use to get tables/figures from "
            "a report or dataset page."
        ),
        cost="low",
        content="pure",
        links="pure",
        max_depth=0,
        max_pages=5,
        max_results=6,
        extract_artifacts=True,
        artifact_types=("table", "image", "figure", "chart"),
    ),
    "research_ml": CrawlPreset(
        name="research_ml",
        description=(
            "Smart-but-zero-token research: local ML extraction (summary, "
            "keyphrases, entities, sentiment) and a best-first semantic frontier "
            "that follows the most relevant links — no LLM, no API cost. Use for "
            "broad research/monitoring when token budget matters."
        ),
        cost="minimal",
        content="ml",
        links="ml",
        max_depth=1,
        max_pages=20,
        max_links_per_level=25,
    ),
    "rag_ingest": CrawlPreset(
        name="rag_ingest",
        description=(
            "RAG-ready ingestion: clean Markdown with inline artifact anchors plus "
            "externalized tables/images, no LLM. Use to load pages into a vector "
            "store / RAG pipeline."
        ),
        cost="low",
        content="pure",
        links="pure",
        max_depth=0,
        max_pages=8,
        max_results=8,
        extract_artifacts=True,
        emit_markdown=True,
        markdown_artifact_anchors=True,
    ),
}


def resolve_presets(
    custom: Optional[Dict[str, CrawlPreset]] = None,
    *,
    include_defaults: bool = True,
) -> Dict[str, CrawlPreset]:
    """
    Merge the built-in catalog with developer-supplied presets.

    Parameters
    ----------
    custom : dict[str, CrawlPreset] | None
        Extra presets to add. A custom preset whose key matches a built-in name
        **overrides** it (so callers can retune ``deep_research`` etc.).
    include_defaults : bool
        If False, start from an empty catalog (custom presets only).

    Returns
    -------
    dict[str, CrawlPreset]
        Name -> preset. Each value's ``name`` is normalized to its dict key so a
        mismatched key/name can't desync the catalog.
    """
    merged: Dict[str, CrawlPreset] = dict(DEFAULT_PRESETS) if include_defaults else {}
    for key, preset in (custom or {}).items():
        # Keep name in sync with the lookup key (the key is the source of truth).
        merged[key] = preset if preset.name == key else _renamed(preset, key)
    return merged


def _renamed(preset: CrawlPreset, name: str) -> CrawlPreset:
    return dataclasses.replace(preset, name=name)


__all__ = ["CrawlPreset", "DEFAULT_PRESETS", "resolve_presets"]
