# -*- coding: utf-8 -*-
"""
lazycrawler.llm
===============
All smart-mode LLM calls, built on LazyBridge.

Why LazyBridge:
- to switch provider/model just change ``LLMConfig.model`` (the provider is
  inferred from the string: "gpt-4o-mini", "claude-haiku-4-5",
  "gemini-3-flash-preview", "deepseek-chat", ...)
- structured output is enforced via ``output=<PydanticModel>``: no manual JSON
  parsing, validation and retries handled by LazyBridge

This module is imported ONLY in smart mode. Pure mode does not depend on
LazyBridge.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from pydantic import BaseModel, Field

from ._log import log
from .config import LLMConfig
from .prompts import (
    CONTENT_EXTRACTION_SYSTEM,
    CUSTOM_EXTRACTION_SYSTEM,
    LARGE_DOC_SUMMARY_SYSTEM,
    TOPIC_EXPANSION_SYSTEM,
    build_link_selection_system,
)


# =============================================================================
# STRUCTURED OUTPUT MODELS
# =============================================================================

class PageExtract(BaseModel):
    """Main content extracted from a page (smart mode)."""
    title: str = Field(default="", description="Page or article title")
    summary: str = Field(default="", description="Concise summary, 1-3 sentences")
    clean_text: str = Field(default="", description="Cleaned main content")
    entities: List[str] = Field(default_factory=list, description="People, orgs, places, products")
    topics: List[str] = Field(default_factory=list, description="Main topics/themes")


class LinkSelection(BaseModel):
    """1-based indices of the relevant links selected by the LLM."""
    indices: List[int] = Field(default_factory=list)


# =============================================================================
# CRAWLER LLM (LazyBridge wrapper)
# =============================================================================

class CrawlerLLM:
    """
    Builds and uses LazyBridge agents for smart mode.

    Agents are stateless (no ``memory=``): they can be reused across pages with
    no cross-call contamination. They are built lazily, so you only pay for what
    you use.

    Parameters
    ----------
    cfg : LLMConfig
        Model, large-doc model, temperature, timeout.
    """

    def __init__(self, cfg: Optional[LLMConfig] = None):
        self.cfg = cfg or LLMConfig()
        # Import checked at instantiation (only smart mode reaches here).
        try:
            from lazybridge import Agent, LLMEngine  # noqa: F401
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "Smart mode requires LazyBridge. Install with:\n"
                "    pip install lazybridge\n"
                "or use mode='pure' (no LLM)."
            ) from e
        self._Agent = Agent
        self._LLMEngine = LLMEngine
        self._clean = None
        self._topic = None
        self._summary = None
        self._schema_agents: dict = {}   # custom output schema -> agent

    # -- agent construction (lazy) --------------------------------------------

    def _engine(self, model: str, system: str):
        return self._LLMEngine(
            model,
            system=system,
            temperature=self.cfg.temperature,
            request_timeout=self.cfg.request_timeout,
        )

    def _clean_agent(self):
        if self._clean is None:
            self._clean = self._Agent(
                engine=self._engine(self.cfg.model, CONTENT_EXTRACTION_SYSTEM),
                output=PageExtract,
            )
        return self._clean

    def _schema_agent(self, schema: type):
        """Agent for a user-provided output schema (cached per schema)."""
        agent = self._schema_agents.get(schema)
        if agent is None:
            agent = self._Agent(
                engine=self._engine(self.cfg.model, CUSTOM_EXTRACTION_SYSTEM),
                output=schema,
            )
            self._schema_agents[schema] = agent
        return agent

    def _topic_agent(self):
        if self._topic is None:
            self._topic = self._Agent(engine=self._engine(self.cfg.model, TOPIC_EXPANSION_SYSTEM))
        return self._topic

    def _summary_agent(self):
        if self._summary is None:
            model = self.cfg.large_doc_model or self.cfg.model
            self._summary = self._Agent(engine=self._engine(model, LARGE_DOC_SUMMARY_SYSTEM))
        return self._summary

    def build_link_selector(self, topic: str, max_links: int):
        """
        Build a link-selection agent for a specific topic.
        The topic is fixed for the whole crawl, so this is built once at the
        start of a run.
        """
        sys = build_link_selection_system(topic, max_links)
        return self._Agent(engine=self._engine(self.cfg.model, sys), output=LinkSelection)

    # -- operations -----------------------------------------------------------

    def extract_content(self, url: str, text: str, schema: Optional[type] = None):
        """
        Extract structured content from pre-cleaned text.

        Returns a ``PageExtract`` (default), or an instance of ``schema`` when a
        custom Pydantic model is provided, or None on error (the caller records
        llm_error). The text must already be truncated upstream by the crawler.
        """
        out_type = schema or PageExtract
        agent = self._schema_agent(schema) if schema is not None else self._clean_agent()
        try:
            env = agent(f"SOURCE URL: {url}\n\n{text}")
        except Exception as e:
            log.error("LLM extract_content failed for %s: %s: %s",
                      url, type(e).__name__, e, exc_info=True)
            return None
        if env.ok and isinstance(env.payload, out_type):
            return env.payload
        log.warning("LLM extract_content unexpected output for %s (%s)", url,
                    "error: " + env.error.message if env.error else "no payload")
        return None

    def expand_topic(self, query: str) -> str:
        """Expand the query into a topic description. Fallback: the query."""
        try:
            env = self._topic_agent()(f"Search query: {query}")
            t = (env.text() or "").strip() if env.ok else ""
            return t if len(t) > 5 else query
        except Exception as e:
            log.warning("LLM expand_topic failed (%s: %s) - using raw query",
                        type(e).__name__, e, exc_info=True)
            return query

    def select_links(
        self,
        selector,
        excerpt: str,
        candidates: List[Tuple[str, str]],
        max_links: int,
    ) -> List[Tuple[str, str]]:
        """
        Select relevant links via LLM. Fallback: first max_links candidates.

        Parameters
        ----------
        selector : Agent
            Agent built with build_link_selector().
        excerpt : str
            Page excerpt (context for the selection).
        candidates : list[(anchor, url)]
            Candidate links extracted from the page.
        """
        if not candidates:
            return []
        subset = candidates[: self.cfg.max_candidates_to_llm]
        links_text = "\n".join(
            f"{i + 1}. [{t[:90]}] -> {u[:180]}" for i, (t, u) in enumerate(subset)
        )
        user = (
            f"PAGE EXCERPT (first {self.cfg.max_links_excerpt_chars} chars):\n"
            f"{excerpt[: self.cfg.max_links_excerpt_chars]}\n\n"
            f"CANDIDATE LINKS:\n{links_text}"
        )
        try:
            env = selector(user)
            sel = env.payload if (env.ok and isinstance(env.payload, LinkSelection)) else None
            idxs = sel.indices if sel else []
            out: List[Tuple[str, str]] = []
            for idx in idxs:
                if isinstance(idx, int) and 1 <= idx <= len(subset):
                    out.append(subset[idx - 1])
            return out[:max_links]
        except Exception as e:
            log.warning("LLM select_links failed (%s: %s) - falling back to first %d candidates",
                        type(e).__name__, e, max_links, exc_info=True)
            return subset[:max_links]

    def summarize_large(
        self,
        url: str,
        text: str,
        max_chars_out: int,
        threshold: int,
        chunk_chars: int,
        max_chunks: int,
    ) -> str:
        """
        Compress a large document with map-reduce. If the text is below the
        threshold, return it truncated to max_chars_out.
        """
        if len(text) <= threshold:
            return text[:max_chars_out]

        log.info("large document (%d chars) - map-reduce summarization", len(text))
        agent = self._summary_agent()
        chunks = [text[i:i + chunk_chars] for i in range(0, len(text), chunk_chars)][:max_chunks]

        partials: List[str] = []
        for i, chunk in enumerate(chunks, 1):
            try:
                env = agent(f"URL: {url}\nChunk {i}/{len(chunks)}\n\n{chunk}")
                partials.append((env.text() or "").strip() if env.ok else chunk[:1500])
            except Exception as e:
                log.warning("large-doc chunk %d failed (%s: %s) - keeping raw chunk prefix",
                            i, type(e).__name__, e, exc_info=True)
                partials.append(chunk[:1500])

        merged = "\n\n".join(p for p in partials if p.strip())
        if not merged.strip():
            return text[:max_chars_out]
        try:
            env = agent(
                f"URL: {url}\nMerge and compress the partial summaries into a "
                f"single coherent summary, preserving only the most relevant "
                f"information.\n\n{merged[:50_000]}"
            )
            final = (env.text() or "").strip() if env.ok else ""
            if final:
                return final[:max_chars_out]
        except Exception as e:
            log.warning("large-doc final synthesis failed (%s: %s) - returning merged partials",
                        type(e).__name__, e, exc_info=True)
        return merged[:max_chars_out]
