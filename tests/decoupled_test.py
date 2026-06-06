# -*- coding: utf-8 -*-
"""
Deterministic test of the independent content/links LLM knobs.
No network, no LazyBridge: a fake LLM records which methods are called, and the
HTTP fetch is stubbed.
Run: python tests/decoupled_test.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lazycrawler import CrawlerConfig, HTTPConfig, WebCrawler
from lazycrawler.llm import PageExtract

PASS = FAIL = 0
def check(name, cond):
    global PASS, FAIL
    PASS, FAIL = PASS + bool(cond), FAIL + (not cond)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")


PAGE_HTML = """<html><head><title>Seed Page</title></head><body>
<h1>Seed Page</h1>
<p>This is a substantial paragraph of real content about a general topic, long
enough to pass the minimum-length checks performed by the crawler pipeline.</p>
<a href="https://site.example/a">Link A</a>
<a href="https://site.example/b">Link B</a>
</body></html>"""

CHILD_HTML = """<html><head><title>Child</title></head><body>
<p>A second substantial paragraph of real content on the child page, also long
enough to be extracted and counted as a proper page by the crawler.</p>
</body></html>"""


def fake_fetch(url, extra_headers=None):
    """Return canned HTML so no network is touched."""
    if url.rstrip("/").endswith(("/a", "/b")):
        return CHILD_HTML, "child page text content, sufficiently long to pass", 200
    return PAGE_HTML, "seed page text content, sufficiently long to pass checks", 200


class FakeLLM:
    """Records which LLM operations get invoked."""
    def __init__(self):
        self.calls = {"build_link_selector": 0, "select_links": 0, "extract_content": 0}

    def build_link_selector(self, topic, max_links):
        self.calls["build_link_selector"] += 1
        return "SELECTOR"

    def select_links(self, selector, excerpt, candidates, max_links):
        self.calls["select_links"] += 1
        return candidates[:max_links]

    def extract_content(self, url, text):
        self.calls["extract_content"] += 1
        return PageExtract(title="X", summary="a summary", clean_text=text[:200],
                           entities=["E"], topics=["T"])

    def summarize_large(self, *a, **k):
        return "summary"


def run_case(content, links):
    crawler = WebCrawler(CrawlerConfig(max_depth=1, max_pages=5, max_links_per_level=2),
                         HTTPConfig(link_delay=0))
    crawler._http.fetch = fake_fetch          # stub network
    fake = FakeLLM()
    crawler._llm = fake                        # inject fake LLM (skips _ensure_llm build)
    results = crawler.crawl("https://site.example/seed", content=content, links=links)
    return results, fake


print("\n=== content=smart, links=pure  (LLM for summary, NOT for links) ===")
results, fake = run_case("smart", "pure")
seed = results[0]
check("seed content is smart (has summary)", seed.summary is not None)
check("extract_content WAS called", fake.calls["extract_content"] >= 1)
check("build_link_selector NOT called", fake.calls["build_link_selector"] == 0)
check("select_links NOT called", fake.calls["select_links"] == 0)
check("links still followed (heuristic)", len(results) >= 2)

print("\n=== content=pure, links=smart  (LLM for links, NOT for summary) ===")
results, fake = run_case("pure", "smart")
seed = results[0]
check("seed content is pure (no summary)", seed.summary is None)
check("extract_content NOT called", fake.calls["extract_content"] == 0)
check("build_link_selector WAS called", fake.calls["build_link_selector"] == 1)
check("select_links WAS called", fake.calls["select_links"] >= 1)
check("links followed via LLM selection", len(results) >= 2)

print("\n=== pure/pure  (no LLM at all) ===")
results, fake = run_case("pure", "pure")
check("no LLM calls whatsoever", sum(fake.calls.values()) == 0)
check("pages crawled", len(results) >= 2)

print(f"\n=== RESULT: {PASS} PASS, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
