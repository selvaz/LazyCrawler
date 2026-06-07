# -*- coding: utf-8 -*-
"""Offline evaluation harness for ML mode.

The canonical focused-crawler metric is **harvest rate**: of the pages actually
fetched (excluding the seed), what fraction are on-topic. A good link scorer
spends a limited page budget on relevant pages; a naive "first N" follower wastes
it on whatever appears first in the HTML.

These tests run with Model2Vec ABSENT (the CI case) — i.e. the lexical+structural
scoring path — and still demonstrate the best-first frontier's advantage. They
double as a regression harness: changes to the scorer can be judged by whether
harvest rate holds or improves, instead of by assertion-free vibes.
"""

from __future__ import annotations

import pytest

from lazycrawler import CrawlerConfig, HTTPConfig, WebCrawler
from lazycrawler.ml import keyphrases_semantic

SEED = "https://e.org/hub"
TOPIC = "lithium battery storage"

# A hub page where the OFF-topic links come first in document order (so "first N"
# burns its budget on them) and one on-topic link has a WEAK anchor ("read more")
# but a topical heading — only reachable if anchor-context back-fill works.
_HUB_LINKS = (
    '<a href="https://e.org/off-1">celebrity gossip and entertainment</a>'
    '<a href="https://e.org/off-2">live sports scores today</a>'
    '<a href="https://e.org/off-3">easy dinner recipes tonight</a>'
    '<a href="https://e.org/off-4">cheap travel deals this weekend</a>'
    '<a href="https://e.org/on-1">lithium battery storage research</a>'
    '<a href="https://e.org/on-2">grid scale battery storage breakthrough</a>'
    "<h3>solid state lithium battery storage</h3>"
    '<a href="https://e.org/on-weak">read more</a>'
)


def harvest_rate(results, seed_url: str) -> float:
    """Fraction of fetched (non-seed) 'done' pages that are on-topic."""
    children = [r for r in results if r.status == "done" and r.url != seed_url]
    if not children:
        return 0.0
    on = sum(1 for r in children if "/on-" in r.url)
    return on / len(children)


def _crawl(stub_fetch, link_mode: str, budget: int):
    stub_fetch(links_map={SEED: _HUB_LINKS})
    with WebCrawler(
        CrawlerConfig(max_depth=1, max_pages=1 + budget, respect_robots=False),
        HTTPConfig(verify_ssl=False, link_delay=0),
    ) as c:
        return c.crawl(SEED, links=link_mode, topic=TOPIC)


@pytest.mark.parametrize("budget", [2, 3])
def test_harvest_rate_best_first_beats_first_n(stub_fetch, budget):
    """Best-first (links='ml') achieves a perfect harvest rate under budget while
    document-order 'first N' (links='pure') harvests nothing."""
    ml = harvest_rate(_crawl(stub_fetch, "ml", budget), SEED)
    pure = harvest_rate(_crawl(stub_fetch, "pure", budget), SEED)
    assert ml == 1.0  # the whole budget was spent on on-topic pages
    assert pure == 0.0  # the budget went to the leading (off-topic) links
    assert ml > pure


def test_anchor_context_lifts_weak_anchor_into_budget(stub_fetch):
    """The on-topic page behind a 'read more' anchor is crawled thanks to
    anchor-context back-fill (its topical heading), not despite it."""
    urls = {r.url for r in _crawl(stub_fetch, "ml", budget=3) if r.status == "done"}
    assert "https://e.org/on-weak" in urls


def test_anchor_context_backfill_enriches_weak_anchor():
    """Unit: a weak/empty anchor inherits nearby descriptive text for scoring."""
    from lazycrawler.text import extract_candidate_links

    html = (
        "<h2>Lithium battery storage breakthrough</h2>"
        '<p><a href="https://e.org/article">read more</a></p>'
        '<a href="https://e.org/strong">explicit descriptive anchor text</a>'
    )
    cands = dict((u, t) for (t, u) in extract_candidate_links(html, "https://e.org/"))
    # weak anchor got the heading folded in; strong anchor left untouched.
    assert "lithium battery storage" in cands["https://e.org/article"].lower()
    assert cands["https://e.org/strong"] == "explicit descriptive anchor text"


def test_keyphrases_semantic_prefers_on_topic_phrases():
    """KeyBERT-style semantic keyphrases (reusing the embedder) surface the
    document's salient phrases and dedup near-duplicates via MMR."""
    np = pytest.importorskip("numpy")

    class FakeEmbedder:
        VOCAB = ["lithium", "battery", "storage", "grid", "solar", "weather", "sport"]

        def encode(self, texts):
            rows = []
            for t in texts:
                low = t.lower()
                v = np.array([1.0 if w in low else 0.0 for w in self.VOCAB], dtype="float32")
                n = np.linalg.norm(v) or 1.0
                rows.append(v / n)
            return np.vstack(rows)

    doc = (
        "Lithium battery storage powers the grid. Battery storage research advances. "
        "Unrelated weather and sport notes appear briefly."
    )
    kp = keyphrases_semantic(doc, FakeEmbedder(), topk=5)
    assert kp and any("battery" in p for p in kp)
    assert len(kp) == len(set(kp))  # MMR returns no duplicates


def test_keyphrases_semantic_falls_back_without_embedder():
    """No embedder -> YAKE/frequency fallback, never an error."""
    kp = keyphrases_semantic("battery storage and lithium research", embedder=None, topk=4)
    assert isinstance(kp, list) and kp
