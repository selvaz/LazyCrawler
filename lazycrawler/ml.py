# -*- coding: utf-8 -*-
"""
lazycrawler.ml
==============
The **machine-learning engine** — a no-LLM, zero-token analogue of
``lazycrawler.llm.CrawlerLLM``. It implements the same small interface the
crawler already calls (``build_link_selector`` / ``select_links`` /
``extract_content``), so the crawler stays engine-agnostic: ``links="ml"`` /
``content="ml"`` simply swap this engine in for the LLM one.

Phase 1 scope: **intelligent link scoring** (semantic + lexical + structural),
used by the crawler's best-first frontier. Content extraction (summary /
entities / topics / sentiment via local ML & statistics) is filled in by a later
phase; ``extract_content`` currently returns clean text only.

Semantic scoring uses **Model2Vec** static embeddings (numpy-only inference,
~500x faster than a sentence-transformer on CPU). The model is read-only, so a
single embedder instance is shared across all crawl workers. Everything degrades
gracefully: without the ``ml`` extra installed, scoring falls back to the
lexical + structural signals (still far better than "first N").
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from urllib.parse import urlparse

from ._log import log
from .config import MLConfig

# =============================================================================
# SHARED EMBEDDER (Model2Vec; numpy read-only -> safe to share across threads)
# =============================================================================

_EMB_CACHE: dict = {}
_EMB_LOCK = threading.Lock()


class _Embedder:
    """Thin wrapper over a Model2Vec static model returning L2-normalized vectors."""

    def __init__(self, model_name: str):
        from model2vec import StaticModel  # lazy: only when ml extra is installed

        self._m = StaticModel.from_pretrained(model_name)

    def encode(self, texts: List[str]):
        import numpy as np

        if not texts:
            return np.zeros((0, 1), dtype="float32")
        v = np.asarray(self._m.encode(list(texts)), dtype="float32")
        if v.ndim == 1:
            v = v.reshape(1, -1)
        norm = np.linalg.norm(v, axis=1, keepdims=True)
        norm[norm == 0] = 1.0
        return v / norm


def get_embedder(model_name: str) -> Optional["_Embedder"]:
    """Return a process-cached embedder for ``model_name``, or None if Model2Vec
    is unavailable (ml extra not installed / model load failed). Thread-safe."""
    with _EMB_LOCK:
        if model_name in _EMB_CACHE:
            return _EMB_CACHE[model_name]
    emb: Optional[_Embedder]
    try:
        emb = _Embedder(model_name)
        log.debug("ml: loaded static embedder %s", model_name)
    except Exception:
        log.warning(
            "model2vec unavailable - ML semantic link scoring disabled, using "
            "lexical+structural only (pip install lazycrawler[ml])",
            exc_info=True,
        )
        emb = None
    with _EMB_LOCK:
        _EMB_CACHE[model_name] = emb
    return emb


# =============================================================================
# LINK SCORER  (semantic + lexical + structural)
# =============================================================================

_WORD = re.compile(r"[a-z0-9]+")


def _tokens(s: Optional[str]) -> set:
    return set(_WORD.findall((s or "").lower()))


@dataclass
class _LinkScorer:
    """Ranks candidate links by relevance to ``topic`` — no LLM, no tokens.

    Built once per crawl (the topic is embedded a single time). ``rank`` blends:
      - semantic: cosine(topic, anchor + URL-path tokens) via static embeddings
      - lexical:  token overlap of the topic with the anchor + URL path
      - structural: URL depth / query / anchor-quality priors (topic-independent)
    """

    topic: str
    cfg: MLConfig
    embedder: Optional[_Embedder] = None
    topic_tokens: set = field(default_factory=set, init=False)
    topic_vec: object = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.topic_tokens = _tokens(self.topic)
        if self.embedder is not None and self.topic.strip():
            try:
                self.topic_vec = self.embedder.encode([self.topic])[0]
            except Exception:
                log.debug("ml: topic embedding failed - semantic disabled", exc_info=True)
                self.topic_vec = None

    def rank(
        self, candidates: List[Tuple[str, str]], excerpt: str = ""
    ) -> List[Tuple[float, str, str]]:
        """Return ``[(score, anchor, url)]`` sorted by descending relevance."""
        if not candidates:
            return []
        sims = None
        if self.topic_vec is not None:
            try:
                sub = candidates[: self.cfg.max_candidates_to_embed]
                vecs = self.embedder.encode([self._cand_text(a, u) for (a, u) in sub])
                sims = vecs @ self.topic_vec  # both L2-normalized -> cosine in [-1, 1]
            except Exception:
                log.debug("ml: candidate embedding failed - semantic disabled", exc_info=True)
                sims = None
        out: List[Tuple[float, str, str]] = []
        for i, (anchor, url) in enumerate(candidates):
            sem = (float(sims[i]) + 1.0) / 2.0 if (sims is not None and i < len(sims)) else 0.0
            lex = self._lexical(anchor, url)
            struct = self._structural(anchor, url)
            score = self.cfg.w_sem * sem + self.cfg.w_lex * lex + self.cfg.w_struct * struct
            out.append((score, anchor, url))
        out.sort(key=lambda t: t[0], reverse=True)
        return out

    def _cand_text(self, anchor: str, url: str) -> str:
        path_tokens = _WORD.findall(urlparse(url).path.lower())
        return f"{anchor} {' '.join(path_tokens)}".strip()

    def _lexical(self, anchor: str, url: str) -> float:
        if not self.topic_tokens:
            return 0.0
        toks = _tokens(anchor) | _tokens(urlparse(url).path)
        if not toks:
            return 0.0
        return len(self.topic_tokens & toks) / len(self.topic_tokens)

    def _structural(self, anchor: str, url: str) -> float:
        p = urlparse(url)
        depth = p.path.strip("/").count("/")
        s = 1.0 - min(depth, 6) * 0.08
        if p.query:
            s -= 0.2
        n_anchor = len(_tokens(anchor))
        if n_anchor == 0:
            s -= 0.3  # empty / icon links
        elif n_anchor >= 3:
            s += 0.1  # descriptive anchors
        return max(0.0, min(1.0, s))


# =============================================================================
# ML ENGINE  (mirrors CrawlerLLM's interface; no LLM, no tokens)
# =============================================================================

_DEFAULT = object()


class MLEngine:
    """No-LLM extraction engine: builds a link scorer and (later) extracts
    structured content with local ML / statistics."""

    def __init__(self, cfg: Optional[MLConfig] = None, embedder=_DEFAULT):
        self.cfg = cfg or MLConfig()
        self.embedder = get_embedder(self.cfg.model) if embedder is _DEFAULT else embedder

    # -- links ----------------------------------------------------------------

    def build_link_selector(self, topic: str, max_links: int) -> _LinkScorer:
        return _LinkScorer(topic or "", self.cfg, self.embedder)

    def select_links(
        self, selector: _LinkScorer, excerpt: str, candidates: List[Tuple[str, str]], max_links: int
    ) -> List[Tuple[float, str, str]]:
        return selector.rank(candidates, excerpt)[:max_links]

    # -- content (Phase 2 fills summary/entities/topics/sentiment) ------------

    def extract_content(self, url: str, text: str, schema=None):
        """Return a PageExtract. Phase 1: clean text only (no LLM, no tokens)."""
        from .llm import PageExtract  # local import keeps pure-mode free of this

        return PageExtract(clean_text=text)
