# -*- coding: utf-8 -*-
"""
Deterministic test of the native parallel mode (CrawlerConfig.max_workers).
No network: HTTPClient.fetch is monkeypatched at class level (so thread-local
worker clients are stubbed too). No LLM (pure mode).
Run: python tests/parallel_test.py
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lazycrawler import CrawlerConfig, CrawlerDB, DBConfig, HTTPConfig, WebCrawler
from lazycrawler import http as http_mod

PASS = FAIL = 0
def check(name, cond):
    global PASS, FAIL
    PASS, FAIL = PASS + bool(cond), FAIL + (not cond)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")

N_CHILDREN = 12
FETCH_DELAY = 0.05
BODY = "Lorem ipsum dolor sit amet, " * 20  # >200 chars so it counts as real text


def fake_fetch(self, url, extra_headers=None):
    """Class-level stub: simulates latency, returns a seed page linking to leaves."""
    time.sleep(FETCH_DELAY)
    if url.rstrip("/").endswith("/seed"):
        links = "".join(
            f'<a href="https://site.example/p{i}">Page {i}</a>' for i in range(N_CHILDREN)
        )
        html = f"<html><head><title>Seed</title></head><body><p>{BODY}</p>{links}</body></html>"
    else:
        html = f"<html><head><title>Leaf</title></head><body><p>{BODY}</p></body></html>"
    return html, BODY, 200


# install the stub for the whole test
http_mod.HTTPClient.fetch = fake_fetch
SEED = "https://site.example/seed"


def run(max_workers, db=None):
    crawler = WebCrawler(
        CrawlerConfig(max_depth=1, max_pages=50, max_links_per_level=20, max_workers=max_workers),
        HTTPConfig(link_delay=0),
        db=db,
    )
    t0 = time.perf_counter()
    results = crawler.crawl(SEED, mode="pure", session_id="run")
    dt = time.perf_counter() - t0
    crawler.close()
    return results, dt


print("\n=== 1. Correctness: parallel crawls the whole tree exactly once ===")
res_par, t_par = run(max_workers=6)
done = [r for r in res_par if r.status == "done"]
urls = {r.url for r in done}
check(f"all {N_CHILDREN + 1} pages crawled", len(done) == N_CHILDREN + 1)
check("no duplicate pages (thread-safe visited)", len(urls) == len(done))
check("seed + leaves present", any(u.endswith("/seed") for u in urls)
      and sum(1 for u in urls if "/p" in u) == N_CHILDREN)

print("\n=== 2. Parallel is faster than sequential ===")
res_seq, t_seq = run(max_workers=1)
done_seq = [r for r in res_seq if r.status == "done"]
print(f"    sequential: {t_seq:.2f}s   parallel(6): {t_par:.2f}s")
check("same page set seq vs parallel", {r.url for r in done_seq} == urls)
check("parallel meaningfully faster", t_par < t_seq * 0.6)

print("\n=== 3. DB thread-safety under parallel ===")
db = CrawlerDB(DBConfig(db_path=os.path.join(tempfile.mkdtemp(), "par.db")))
res_db, _ = run(max_workers=8, db=db)
st = db.stats()
print(f"    stats: {st}")
check("DB pages == crawled", st["pages"] == N_CHILDREN + 1)
check("DB edges == crawled", st["edges"] == N_CHILDREN + 1)
check("one session", st["sessions"] == 1)
db.close()

print(f"\n=== RESULT: {PASS} PASS, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
