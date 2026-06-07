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
