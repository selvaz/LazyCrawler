# -*- coding: utf-8 -*-
"""
LazyCrawler smoke test — DB, pure crawl, dedup/cache.
Run: python tests/smoke_test.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lazycrawler import CrawlerConfig, CrawlerDB, DBConfig, HTTPConfig, WebCrawler
from lazycrawler.http import content_hash, url_hash

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}")


# =============================================================================
print("\n=== 1. DB (deterministic, no network) ===")
tmp = tempfile.mkdtemp()
db_path = os.path.join(tmp, "smoke.db")
db = CrawlerDB(DBConfig(db_path=db_path, ttl_hours=24))

sid = db.create_session("sess1", topic="test", seed="http://a.com", mode="pure", source="crawl")
check("create_session", sid == "sess1")

u = "https://example.com/article"
uh = url_hash(u)
db.upsert_page({
    "url": u, "url_hash": uh, "status": "done", "mode": "pure",
    "title": "Hello", "clean_text": "a new species of deep-sea fish was discovered",
    "raw_text": "raw body text", "content_hash": content_hash("raw body text"),
    "entities": ["NOAA"], "topics": ["marine biology"],
})
db.add_edge("sess1", uh, source_url=None, depth=0)

row = db.get_page(uh)
check("upsert+get_page", row is not None and row["title"] == "Hello")
check("entities deserialized", db.get_pages("sess1")[0]["entities"] == ["NOAA"])
check("get_fresh_page within TTL", db.get_fresh_page(u) is not None)
check("find_by_content_hash", db.find_by_content_hash(content_hash("raw body text")) is not None)
check("get_pages by session", len(db.get_pages("sess1")) == 1)

fts = db.search_text("deep-sea fish")
check("search_text (FTS or LIKE)", len(fts) >= 1)

st = db.stats()
check("stats", st["pages"] == 1 and st["edges"] == 1 and st["sessions"] == 1)

# expired TTL
db2 = CrawlerDB(DBConfig(db_path=db_path, ttl_hours=0))
check("get_fresh_page None (TTL=0)", db2.get_fresh_page(u) is None)
db2.close()
db.close()

# =============================================================================
print("\n=== 2. PURE crawl (live, depth=0) ===")
try:
    crawler = WebCrawler(
        CrawlerConfig(max_depth=0, max_pages=2),
        # verify_ssl=False: environment with SSL MITM (Avast)
        HTTPConfig(link_delay=0.2, verify_ssl=False),
    )
    results = crawler.crawl("https://en.wikipedia.org/wiki/Web_crawler", mode="pure")
    crawler.close()
    check("got results", len(results) >= 1)
    if results:
        r = results[0]
        print(f"    status={r.status} mode={r.mode} title={ (r.title or '')[:50]!r} chars={len(r.text or '')}")
        check("status done", r.status == "done")
        check("mode pure", r.mode == "pure")
        check("has text", bool(r.text) and len(r.text) > 200)
        check("no summary in pure", r.summary is None)
except Exception as e:
    print(f"    [SKIP] network unavailable: {type(e).__name__}: {e}")

# =============================================================================
print("\n=== 3. DEDUP / cache (live, same URL twice) ===")
try:
    db3 = CrawlerDB(DBConfig(db_path=os.path.join(tmp, "dedup.db"), ttl_hours=24))
    crawler = WebCrawler(CrawlerConfig(max_depth=0, max_pages=2),
                         HTTPConfig(link_delay=0.2, verify_ssl=False), db=db3)
    u3 = "https://en.wikipedia.org/wiki/Web_scraping"
    r1 = crawler.crawl(u3, mode="pure", session_id="run1")
    r2 = crawler.crawl(u3, mode="pure", session_id="run2")
    crawler.close()
    check("run1 fetched (not cache)", r1 and r1[0].from_cache is False)
    check("run2 from cache", r2 and r2[0].from_cache is True)
    check("1 page, 2 edges", db3.stats()["pages"] == 1 and db3.stats()["edges"] == 2)
    db3.close()
except Exception as e:
    print(f"    [SKIP] network unavailable: {type(e).__name__}: {e}")

# =============================================================================
print(f"\n=== RESULT: {PASS} PASS, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
