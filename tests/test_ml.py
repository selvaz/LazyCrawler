# -*- coding: utf-8 -*-
"""ML mode (no-LLM): link scoring + best-first frontier (sequential & parallel).

These run with Model2Vec ABSENT (the offline CI case): get_embedder degrades to
None and scoring falls back to lexical + structural — still topic-aware. A
fake-embedder test exercises the semantic path without downloading a model.
"""

from __future__ import annotations

import pytest

from lazycrawler import CrawlerConfig, HTTPConfig, MLConfig, WebCrawler
from lazycrawler.ml import MLEngine, _LinkScorer

U = "https://e.org/seed"


# -- scorer units ----------------------------------------------------------


def test_scorer_heuristic_ranks_topic_match_first():
    sc = _LinkScorer("lithium battery technology", MLConfig(), embedder=None)
    ranked = sc.rank(
        [
            ("contact us", "https://e.org/contact"),
            ("about", "https://e.org/about"),
            ("lithium battery cells breakthrough", "https://e.org/lithium-battery-cells"),
        ]
    )
    assert ranked  # [(score, anchor, url)] sorted desc
    assert "lithium-battery" in ranked[0][2]
    assert ranked[0][0] >= ranked[-1][0]


def test_scorer_no_topic_uses_structure_only():
    sc = _LinkScorer("", MLConfig(), embedder=None)
    ranked = sc.rank([("deep one", "https://e.org/a/b/c/d/e"), ("shallow", "https://e.org/x")])
    # shallower URL with a descriptive anchor should not rank below the deep one
    assert ranked[0][2].endswith("/x")


def test_mlengine_select_links_truncates_and_scores():
    eng = MLEngine(MLConfig(), embedder=None)
    sel = eng.build_link_selector("solar power", max_links=2)
    out = eng.select_links(
        sel, "", [("a", "https://e.org/1"), ("b", "https://e.org/2"), ("c", "https://e.org/3")], 2
    )
    assert len(out) == 2
    assert all(len(t) == 3 for t in out)  # (score, anchor, url)


def test_fake_embedder_semantic_path():
    np = pytest.importorskip("numpy")

    class Fake:
        VOCAB = ["solar", "wind", "battery", "sport", "contact"]

        def encode(self, texts):
            rows = []
            for t in texts:
                low = t.lower()
                v = np.array([1.0 if w in low else 0.0 for w in self.VOCAB], dtype="float32")
                n = np.linalg.norm(v) or 1.0
                rows.append(v / n)
            return np.vstack(rows)

    sc = _LinkScorer("solar battery", MLConfig(), embedder=Fake())
    ranked = sc.rank(
        [("sports scores", "https://e.org/sport"), ("solar battery guide", "https://e.org/solar")]
    )
    assert "solar" in ranked[0][2]


# -- end-to-end: best-first frontier ---------------------------------------

_LINKS = (
    '<a href="https://e.org/lithium-battery-breakthrough">lithium battery breakthrough research</a>'
    '<a href="https://e.org/contact">contact us</a>'
    '<a href="https://e.org/sports-news">sports news today</a>'
    '<a href="https://e.org/about">about</a>'
)


def test_best_first_follows_relevant_link_first(stub_fetch):
    stub_fetch(links_map={U: _LINKS})
    with WebCrawler(
        CrawlerConfig(max_depth=1, max_pages=2, respect_robots=False),
        HTTPConfig(verify_ssl=False, link_delay=0),
    ) as c:
        results = c.crawl(U, links="ml", topic="lithium battery technology")
    urls = {r.url for r in results}
    assert any("lithium-battery" in u for u in urls)  # best-scoring child crawled
    assert not any("sports-news" in u for u in urls)  # low-score child skipped under the cap


def test_best_first_parallel(stub_fetch):
    stub_fetch(links_map={U: _LINKS})
    with WebCrawler(
        CrawlerConfig(max_depth=1, max_pages=5, max_workers=2, respect_robots=False),
        HTTPConfig(verify_ssl=False, link_delay=0),
    ) as c:
        results = c.crawl(U, links="ml", topic="lithium battery technology")
    done = [r for r in results if r.status == "done"]
    assert len(done) <= 5  # respects the cap in parallel
    assert any("lithium-battery" in r.url for r in done)


def test_content_ml_produces_ml_pages(stub_fetch):
    stub_fetch()
    with WebCrawler(
        CrawlerConfig(max_depth=0, respect_robots=False),
        HTTPConfig(verify_ssl=False, link_delay=0),
    ) as c:
        results = c.crawl(U, content="ml")
    assert results[0].mode == "ml"
    assert results[0].text  # clean text filled (no LLM, no tokens)


# -- Phase 2: local content extraction (no LLM) ----------------------------

_ARTICLE = (
    "OpenAI and Google announced new battery research in California. "
    "The lithium breakthrough improves energy density significantly. "
    "Researchers said the technology could reshape electric vehicles. "
    "Analysts welcomed the news as a major step forward. "
    "The companies plan to scale production next year."
)


def test_keyphrases_fallback_is_topic_relevant():
    from lazycrawler.ml import keyphrases

    kp = keyphrases(_ARTICLE, topk=6)
    assert isinstance(kp, list) and kp
    # statistical fallback should surface salient content words
    assert any(w in " ".join(kp).lower() for w in ("battery", "lithium", "research", "energy"))


def test_summarize_lead_without_embedder():
    from lazycrawler.ml import summarize

    s = summarize(_ARTICLE, embedder=None, n_sentences=2)
    assert s and s.startswith("OpenAI and Google")  # lead sentences


def test_regex_entities_extracts_proper_nouns():
    from lazycrawler.ml import _regex_entities

    ents = _regex_entities(_ARTICLE)
    joined = " ".join(ents)
    assert "OpenAI" in joined or "Google" in joined or "California" in joined


def test_sentiment_returns_valid_label():
    from lazycrawler.ml import sentiment

    assert sentiment(_ARTICLE) in ("negative", "neutral", "positive")


def test_extract_content_fills_structured_fields():
    from lazycrawler.config import MLConfig
    from lazycrawler.ml import MLEngine

    eng = MLEngine(MLConfig(), embedder=None)
    ex = eng.extract_content("https://e.org/a", _ARTICLE)
    assert ex.clean_text == _ARTICLE
    assert isinstance(ex.topics, list) and ex.topics
    assert isinstance(ex.entities, list)
    assert ex.sentiment in ("negative", "neutral", "positive")
    assert isinstance(ex.summary, str) and ex.summary


def test_content_ml_end_to_end_fields(stub_fetch):
    stub_fetch(content_map={U: _ARTICLE})
    with WebCrawler(
        CrawlerConfig(max_depth=0, respect_robots=False),
        HTTPConfig(verify_ssl=False, link_delay=0),
    ) as c:
        r = c.crawl(U, content="ml")[0]
    assert r.mode == "ml"
    assert r.topics  # keyphrases filled, no LLM
    assert r.sentiment in ("negative", "neutral", "positive")


def test_research_ml_preset_present():
    from lazycrawler import DEFAULT_PRESETS

    p = DEFAULT_PRESETS["research_ml"]
    assert p.content == "ml" and p.links == "ml"


def test_ml_min_link_score_override_prunes_frontier(stub_fetch, make_crawler):
    # per-call ml_overrides (the MLConfig knob behind presets like topic_explore_ml)
    links = '<a href="https://e.org/a">a</a><a href="https://e.org/b">b</a>'
    stub_fetch(links_map={"https://e.org/seed": links})

    # impossibly high gate -> every child is pruned, only the seed is crawled
    gated = make_crawler(max_depth=1, max_pages=50).crawl(
        "https://e.org/seed", links="ml", topic="x", ml_overrides={"min_link_score": 9.0}
    )
    assert len([r for r in gated if r.status == "done"]) == 1

    # no gate (default 0) -> children are followed
    free = make_crawler(max_depth=1, max_pages=50).crawl(
        "https://e.org/seed", links="ml", topic="x"
    )
    assert len([r for r in free if r.status == "done"]) >= 2


# =============================================================================
# Deep-audit round 4 — link-scoring best-practice fixes
# =============================================================================


def test_lexical_ignores_stopwords():
    """Stopwords in the topic/anchor neither dilute nor spuriously match."""
    sc = _LinkScorer("the storage of energy", MLConfig(), embedder=None)
    ranked = sc.rank(
        [
            ("the of a an", "https://e.org/the-of"),  # stopwords only -> 0 overlap
            ("energy storage report", "https://e.org/energy-storage"),
        ]
    )
    assert ranked[0][2].endswith("/energy-storage")
    # The stopword-only link gets no LEXICAL credit (structural prior is separate).
    assert sc._lexical("the of a an", "https://e.org/the-of") == 0.0
    # "the"/"of" in the topic don't pad the denominator: one content-word overlap
    # counts as 1/2 (topic content tokens = {storage, energy}), not 1/4.
    assert sc._lexical("energy market", "https://e.org/x") == 0.5


def test_gate_reachable_without_embeddings():
    """F (calibration): with no embedder the score is renormalized to [0,1], so a
    strongly on-topic link can still clear a high min_link_score gate (e.g. 0.5)."""
    sc = _LinkScorer("battery storage", MLConfig(), embedder=None)
    ranked = sc.rank([("battery storage research guide", "https://e.org/battery-storage")])
    # Pre-fix this maxed out at 0.45 (w_lex+w_struct) and a 0.5 gate was unreachable.
    assert ranked[0][0] >= 0.5


def test_context_signal_makes_selection_page_aware():
    """The page excerpt is actually used: with equal topic similarity, the link
    most similar to the current page's content is preferred (focused crawling)."""
    np = pytest.importorskip("numpy")

    class Fake:
        VOCAB = ["solar", "battery", "wind"]

        def encode(self, texts):
            rows = []
            for t in texts:
                low = t.lower()
                v = np.array([1.0 if w in low else 0.0 for w in self.VOCAB], dtype="float32")
                n = np.linalg.norm(v) or 1.0
                rows.append(v / n)
            return np.vstack(rows)

    sc = _LinkScorer("solar", MLConfig(w_context=0.5), embedder=Fake())
    cands = [("battery guide", "https://e.org/battery"), ("wind guide", "https://e.org/wind")]
    # excerpt about batteries -> the battery link wins; about wind -> the wind link wins.
    assert sc.rank(cands, excerpt="battery battery battery")[0][2].endswith("/battery")
    assert sc.rank(cands, excerpt="wind wind wind")[0][2].endswith("/wind")


def test_embed_budget_targets_high_lexical_candidates():
    """The bounded embedding budget goes to candidates a cheap lexical signal likes,
    not whoever appears first in document order."""
    np = pytest.importorskip("numpy")

    class Fake:
        def encode(self, texts):
            return np.ones((len(texts), 2), dtype="float32") / (2**0.5)

    sc = _LinkScorer("battery", MLConfig(max_candidates_to_embed=1), embedder=Fake())
    cands = [("news", "https://e.org/news"), ("battery research", "https://e.org/battery")]
    _topic_sims, _ctx_sims, pos_of = sc._semantic_sims(cands, "")
    assert 1 in pos_of and 0 not in pos_of  # the on-topic (idx 1) link got embedded


# -- offline EVAL: best-first vs first-N within a page budget ----------------


def test_best_first_outperforms_first_n_within_budget(stub_fetch):
    """Eval (no model2vec, lexical+structural only): under a tight page budget the
    semantic/lexical best-first frontier collects the ON-topic children while plain
    'first N' document-order following wastes the budget on the off-topic ones."""
    topic = "lithium battery storage"
    # Off-topic links appear FIRST in document order; on-topic links appear last.
    links = (
        '<a href="https://e.org/off-1">celebrity gossip news</a>'
        '<a href="https://e.org/off-2">sports scores today</a>'
        '<a href="https://e.org/off-3">cooking recipes ideas</a>'
        '<a href="https://e.org/on-1">lithium battery storage research</a>'
        '<a href="https://e.org/on-2">battery storage grid breakthrough</a>'
    )
    stub_fetch(links_map={U: links})

    def crawl(link_mode):
        with WebCrawler(
            CrawlerConfig(max_depth=1, max_pages=3, respect_robots=False),  # seed + 2 children
            HTTPConfig(verify_ssl=False, link_delay=0),
        ) as c:
            return {r.url for r in c.crawl(U, links=link_mode, topic=topic) if r.status == "done"}

    on = lambda urls: sum(1 for u in urls if "/on-" in u)  # noqa: E731
    ml_urls, pure_urls = crawl("ml"), crawl("pure")
    assert on(ml_urls) == 2  # best-first spent the whole budget on-topic
    assert on(pure_urls) == 0  # first-N burned it on the document-order (off-topic) links
    assert on(ml_urls) > on(pure_urls)
