# -*- coding: utf-8 -*-
"""
Deterministic test of the LazyBridge tool layer (CrawlerTools) + the new
sentiment/notes fields. No network (HTTPClient.fetch and search_ddg_urls
stubbed). as_tools() is checked only if LazyBridge is importable.
Run: python tests/tools_test.py
      PYTHONPATH=...\\LazyBridge python tests/tools_test.py   # also checks as_tools()
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lazycrawler import CrawlerDB, CrawlerConfig, CrawlerTools, DBConfig, HTTPConfig
from lazycrawler import http as http_mod
from lazycrawler import search as search_mod
from lazycrawler.http import url_hash
from lazycrawler.llm import PageExtract

PASS = FAIL = 0
def check(name, cond):
    global PASS, FAIL
    PASS, FAIL = PASS + bool(cond), FAIL + (not cond)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")

BODY = "Independent generic content about a subject, sufficiently long to count. " * 20


def fake_fetch(self, url, extra_headers=None):
    return f"<html><head><title>Page {url[-5:]}</title></head><body><p>{BODY}</p></body></html>", BODY, 200

http_mod.HTTPClient.fetch = fake_fetch
search_mod.search_ddg_urls = lambda q, n, bl=None: [f"https://site.example/r{i}" for i in range(min(n, 3))]


print("\n=== 0. PageExtract has sentiment + notes ===")
fields = PageExtract.model_fields
check("sentiment field present", "sentiment" in fields)
check("notes field present", "notes" in fields)
import typing
check("sentiment is 3-way literal",
      set(typing.get_args(fields["sentiment"].annotation)) == {"negative", "neutral", "positive"})


def make_tools():
    db = CrawlerDB(DBConfig(db_path=os.path.join(tempfile.mkdtemp(), "tools.db")))
    ct = CrawlerTools(
        db=db,
        crawler_cfg=CrawlerConfig(max_depth=1, max_pages=10, respect_robots=False),
        http_cfg=HTTPConfig(verify_ssl=False, link_delay=0),
        content="pure",   # offline: no LLM
    )
    return ct, db


print("\n=== 1. web_crawl returns LLM-friendly JSON ===")
ct, db = make_tools()
res = json.loads(ct.web_crawl("https://site.example/start", depth=0))
check("valid JSON with 'pages'", "pages" in res and isinstance(res["pages"], list))
check("page has url/title/snippet/sentiment keys",
      res["pages"] and all(k in res["pages"][0] for k in ("url", "title", "snippet", "sentiment", "status")))
check("found >= 1", res["found"] >= 1)

print("\n=== 2. get_page returns full cached text ===")
full = json.loads(ct.get_page("https://site.example/start"))
check("full text present (longer than snippet)", full.get("text") and len(full["text"]) > 500)
check("has sentiment/notes keys", "sentiment" in full and "notes" in full)
miss = json.loads(ct.get_page("https://nope.example/x"))
check("missing page -> error+hint", "error" in miss and "hint" in miss)

print("\n=== 3. search_cached (free, no network) ===")
sc = json.loads(ct.search_cached("subject"))
check("finds cached page", sc["found"] >= 1 and "/start" in (sc["pages"][0]["url"] or ""))

print("\n=== 4. web_search (stubbed engine) ===")
ws = json.loads(ct.web_search("anything", max_results=3))
check("web_search returns pages", ws["found"] >= 1 and len(ws["pages"]) >= 1)
ct.close()

print("\n=== 5. sentiment/notes round-trip through the DB ===")
ct2, db2 = make_tools()
u = "https://site.example/sent"
db2.upsert_page({"url": u, "url_hash": url_hash(u), "status": "done", "mode": "smart",
                 "clean_text": "x" * 600, "sentiment": "positive", "notes": "tag:research"})
got = json.loads(ct2.get_page(u))
check("sentiment persisted", got["sentiment"] == "positive")
check("notes persisted", got["notes"] == "tag:research")
ct2.close()

print("\n=== 6. as_tools() (requires LazyBridge) ===")
try:
    import lazybridge  # noqa: F401
    ct3, _ = make_tools()
    tools = ct3.as_tools()
    names = {getattr(t, "name", None) for t in tools}
    check("4 tools exposed", len(tools) == 4)
    check("expected tool names", {"web_search", "web_crawl", "get_page", "search_cached"} <= names)
    ct3.close()
except ImportError:
    print("  [SKIP] LazyBridge not on path - run with PYTHONPATH=...\\LazyBridge to check as_tools()")

print(f"\n=== RESULT: {PASS} PASS, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
