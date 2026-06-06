# -*- coding: utf-8 -*-
"""
Deterministic test of robots.txt enforcement (default on, disableable) and
strict error mode. No network: HTTPClient.fetch and HTTPClient.get_text are
stubbed at class level.
Run: python tests/robots_test.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lazycrawler import CrawlerConfig, HTTPConfig, WebCrawler
from lazycrawler import http as http_mod

PASS = FAIL = 0
def check(name, cond):
    global PASS, FAIL
    PASS, FAIL = PASS + bool(cond), FAIL + (not cond)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")

BODY = "Relevant body content here, " * 15
ROBOTS = "User-agent: *\nDisallow: /private\n"


def fake_get_text(self, url):
    """robots.txt stub."""
    if url.endswith("/robots.txt"):
        return ROBOTS
    return None


def fake_fetch(self, url, extra_headers=None):
    if url.rstrip("/").endswith("/start"):
        html = ('<html><body><p>' + BODY + '</p>'
                '<a href="https://site.example/public/a">public</a>'
                '<a href="https://site.example/private/secret">private</a>'
                '</body></html>')
        return html, BODY, 200
    return f"<html><body><p>{BODY}</p></body></html>", BODY, 200


http_mod.HTTPClient.get_text = fake_get_text
http_mod.HTTPClient.fetch = fake_fetch
SEED = "https://site.example/start"


print("\n=== 1. robots.txt enforced by default ===")
crawler = WebCrawler(CrawlerConfig(max_depth=1, max_pages=20, max_links_per_level=10))
results = crawler.crawl(SEED, mode="pure")
crawler.close()
by_status = {}
for r in results:
    by_status.setdefault(r.status, []).append(r.url)
blocked = [u for u in by_status.get("robots_blocked", [])]
done = by_status.get("done", [])
check("private URL blocked by robots", any("/private/" in u for u in blocked))
check("public URL crawled", any("/public/" in u for u in done))
check("seed crawled", any(u.rstrip('/').endswith('/start') for u in done))

print("\n=== 2. robots can be disabled ===")
crawler = WebCrawler(CrawlerConfig(max_depth=1, max_pages=20, max_links_per_level=10,
                                   respect_robots=False))
results = crawler.crawl(SEED, mode="pure")
crawler.close()
urls_done = {r.url for r in results if r.status == "done"}
check("private URL now crawled (robots off)", any("/private/" in u for u in urls_done))
check("no robots_blocked entries", not any(r.status == "robots_blocked" for r in results))

print("\n=== 3. robots also enforced in parallel mode ===")
crawler = WebCrawler(CrawlerConfig(max_depth=1, max_pages=20, max_links_per_level=10,
                                   max_workers=4))
results = crawler.crawl(SEED, mode="pure")
crawler.close()
check("private blocked under parallel",
      any(r.status == "robots_blocked" and "/private/" in r.url for r in results))


# ---- strict mode -----------------------------------------------------------
def raising_fetch(self, url, extra_headers=None):
    if url.rstrip("/").endswith("/boom"):
        raise RuntimeError("simulated fetch crash")
    return f"<html><body><p>{BODY}</p></body></html>", BODY, 200


http_mod.HTTPClient.fetch = raising_fetch

print("\n=== 4. strict=False -> error logged, crawl continues ===")
crawler = WebCrawler(CrawlerConfig(max_depth=0, max_pages=5, respect_robots=False))
try:
    results = crawler.crawl_many(["https://site.example/boom", "https://site.example/ok"],
                                 mode="pure")
    crawler.close()
    check("did not raise", True)
    check("good seed still crawled", any(r.status == "done" for r in results))
except Exception:
    check("did not raise", False)

print("\n=== 5. strict=True -> exception propagates ===")
crawler = WebCrawler(CrawlerConfig(max_depth=0, max_pages=5, respect_robots=False, strict=True))
raised = False
try:
    crawler.crawl("https://site.example/boom", mode="pure")
except RuntimeError:
    raised = True
finally:
    crawler.close()
check("strict re-raised the exception", raised)

print(f"\n=== RESULT: {PASS} PASS, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
