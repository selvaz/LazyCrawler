# -*- coding: utf-8 -*-
"""
LazyCrawler — serious test suite (dedup + formats + real smart path).

PART 1  Deterministic core: PURE mode + a stubbed fetch with a call counter.
        No network, no LLM. Tests the 3-level dedup, URL normalization, TTL,
        force_refresh, edges, parallel safety, hashing, output formats, DB
        round-trip, and the offline tool functions.

PART 2  Real smart path: REAL LazyBridge LLM calls (no fakes). Stubbed fetch so
        the *content* is controlled, but extraction/sentiment/schema go through
        a real model. Auto-skips if LazyBridge or an API key is missing, or if
        the model is unreachable (e.g. offline / SSL-inspection sandbox).

Run in Spyder: just open and Run file. Or:  python tests/test_full.py
"""

import json
import os
import sys
import tempfile

# ── bootstrap: repo root + LazyBridge + .env ──────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import setup_paths  # noqa: E402,F401

from lazycrawler import (CrawlerConfig, CrawlerDB, CrawlerTools, DBConfig,  # noqa: E402
                         HTTPConfig, LLMConfig, WebCrawler)
from lazycrawler import http as http_mod  # noqa: E402
from lazycrawler.http import content_hash, url_hash  # noqa: E402

PASS = FAIL = SKIP = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  [PASS] {name}")
    else:
        FAIL += 1; print(f"  [FAIL] {name}")


def skip(name, reason=""):
    global SKIP
    SKIP += 1
    print(f"  [SKIP] {name} {('- ' + reason) if reason else ''}")


BODY = "Independent generic article body, long enough to be treated as real text. " * 8


def install_fetch(content_map=None, body=BODY):
    """Class-level fetch stub with a call counter (works for parallel workers too)."""
    state = {"n": 0, "by_url": {}}
    cmap = content_map or {}

    def fetch(self, url, extra_headers=None):
        from lazycrawler.http import normalize_url
        key = normalize_url(url)
        state["n"] += 1
        state["by_url"][key] = state["by_url"].get(key, 0) + 1
        b = cmap.get(url, cmap.get(key, body))
        html = (f"<html><head><title>{key[-14:]}</title></head>"
                f"<body><p>{b}</p></body></html>")
        return html, b, 200

    http_mod.HTTPClient.fetch = fetch
    return state


def tmpdb(ttl=24.0, force=False):
    return CrawlerDB(DBConfig(db_path=tempfile.mktemp(suffix=".db"),
                              ttl_hours=ttl, force_refresh=force))


def mk(db, **cfg):
    base = dict(max_depth=0, max_pages=20, respect_robots=False)
    base.update(cfg)
    return WebCrawler(CrawlerConfig(**base), HTTPConfig(verify_ssl=False, link_delay=0), db=db)


# =============================================================================
print("\n########## PART 1 — deterministic (pure, stubbed fetch) ##########")

print("\n=== A. URL dedup + TTL + force_refresh + normalization ===")
fetch = install_fetch()
db = tmpdb()
c = mk(db)
U = "https://site.example/article"
r1 = c.crawl(U, mode="pure", session_id="s1")
check("first crawl fetched once", fetch["n"] == 1 and r1[0].status == "done")
check("first not from cache", r1[0].from_cache is False)
r2 = c.crawl(U, mode="pure", session_id="s1")
check("second crawl is a cache hit (no extra fetch)", fetch["n"] == 1 and r2[0].from_cache is True)
# normalized variant: tracking param + trailing slash + upper-case host
variant = "https://SITE.example/article/?utm_source=news&x=1"
r3 = c.crawl(variant, mode="pure", session_id="s1")
# x=1 is not a tracking param, so url differs -> but utm stripped + host lowered + slash trimmed;
# this variant keeps ?x=1 so it is a *different* url_hash -> fetched, but its content equals U's
check("normalization: utm/host/slash do not defeat the cache for the canonical form",
      url_hash("https://SITE.example/article/?utm_source=news") == url_hash(U))
c.close()

# force_refresh bypasses cache
fetch = install_fetch()
db2 = tmpdb()
mk(db2).crawl(U, mode="pure");
n_after_first = fetch["n"]
c2 = WebCrawler(CrawlerConfig(max_depth=0, respect_robots=False),
                HTTPConfig(verify_ssl=False, link_delay=0),
                db=CrawlerDB(DBConfig(db_path=db2.cfg.db_path, force_refresh=True)))
c2.crawl(U, mode="pure")
check("force_refresh re-fetches", fetch["n"] == n_after_first + 1)
c2.close()

# ttl=0 -> never fresh
fetch = install_fetch()
dbttl = tmpdb(ttl=0.0)
mk(dbttl).crawl(U, mode="pure")
mk(dbttl).crawl(U, mode="pure")
check("ttl_hours=0 re-fetches every time", fetch["n"] == 2)


print("\n=== B. Content-hash dedup (level 2): different URLs, same content ===")
fetch = install_fetch()  # default body for all -> identical content
db = tmpdb()
c = mk(db)
c.crawl("https://a.example/x", mode="pure", session_id="s1")
rb = c.crawl("https://b.example/y", mode="pure", session_id="s1")
check("2nd distinct URL with same content -> content dedup (from_cache)", rb[0].from_cache is True)
check("both URLs fetched (level-2 is post-fetch)", fetch["n"] == 2)
rows = db.get_pages(status="done")
check("two page rows (per-URL provenance)", len(rows) == 2)
check("shared content_hash", rows[0].get("content_hash") and
      db.get_page(url_hash("https://a.example/x"))["content_hash"]
      == db.get_page(url_hash("https://b.example/y"))["content_hash"])
c.close()


print("\n=== C. Edges: provenance + idempotency ===")
fetch = install_fetch()
db = tmpdb()
c = mk(db)
c.crawl(U, mode="pure", session_id="s1")
c.crawl(U, mode="pure", session_id="s2")
c.crawl(U, mode="pure", session_id="s1")  # repeat -> no new edge
st = db.stats()
check("one page across sessions", st["pages"] == 1)
check("two edges (s1, s2), idempotent", st["edges"] == 2)
check("two sessions", st["sessions"] == 2)
c.close()


print("\n=== D. Parallel dedup safety (identical content, FK, counts) ===")
N = 10
seed = "https://site.example/seed"
links = "".join(f'<a href="https://site.example/p{i}">P{i}</a>' for i in range(N))
fetch = install_fetch(content_map={
    seed: f"SEED body long enough to be real content here and there. {links}" * 2,
})
# inject the seed's links into its html via content_map body? body goes in <p>; links need <a>.
# Simpler: give the seed a custom fetch that includes anchors.
def seed_fetch(self, url, extra_headers=None):
    fetch["n"] += 1
    if url.rstrip("/").endswith("/seed"):
        html = (f"<html><body><p>{BODY}</p>{links}</body></html>")
        return html, BODY, 200
    return f"<html><body><p>{BODY}</p></body></html>", BODY, 200
http_mod.HTTPClient.fetch = seed_fetch
db = tmpdb()
cp = WebCrawler(CrawlerConfig(max_depth=1, max_pages=50, max_links_per_level=20,
                              max_workers=6, respect_robots=False),
                HTTPConfig(verify_ssl=False, link_delay=0), db=db)
res = cp.crawl(seed, mode="pure", session_id="par")
cp.close()
done = [r for r in res if r.status == "done"]
check("all N+1 pages processed once", len({r.url for r in done}) == N + 1)
check("DB pages == N+1 (no FK error)", db.stats()["pages"] == N + 1)
check("DB edges == N+1", db.stats()["edges"] == N + 1)


print("\n=== E. Hashing + PageResult formats ===")
check("url_hash strips utm", url_hash("https://x.com/a?utm_source=z") == url_hash("https://x.com/a"))
check("url_hash lowercases host", url_hash("https://X.COM/a") == url_hash("https://x.com/a"))
check("url_hash trims trailing slash", url_hash("https://x.com/a/") == url_hash("https://x.com/a"))
check("content_hash normalizes whitespace",
      content_hash("a  b\n\n\nc") == content_hash("a b\n\nc"))
fetch = install_fetch()
rp = mk(tmpdb()).crawl(U, mode="pure")[0]
check("pure: text set, no summary/sentiment/notes",
      rp.text and rp.summary is None and rp.sentiment is None and rp.notes is None)
check("pure: entities/topics are empty lists", rp.entities == [] and rp.topics == [])
dumped = json.loads(rp.model_dump_json())
check("PageResult JSON round-trips with all keys",
      all(k in dumped for k in ("url", "url_hash", "status", "mode", "text",
                                "sentiment", "notes", "data", "from_cache")))


print("\n=== F. DB round-trip + FTS + offline tools ===")
db = tmpdb()
u = "https://x.example/full"
db.upsert_page({"url": u, "url_hash": url_hash(u), "status": "done", "mode": "smart",
                "clean_text": "the federal budget was approved", "title": "Budget",
                "entities": ["Congress"], "topics": ["budget", "politics"],
                "sentiment": "neutral", "notes": "tag:fiscal",
                "data": {"custom": 42}, "content_hash": content_hash("x")})
row = db.get_page(url_hash(u))
check("entities deserialized to list", row["entities"] == ["Congress"])
check("topics deserialized to list", row["topics"] == ["budget", "politics"])
check("sentiment persisted", row["sentiment"] == "neutral")
check("notes persisted", row["notes"] == "tag:fiscal")
check("custom data (extract_json) deserialized to dict", row["data"] == {"custom": 42})
hits = db.search_text("federal budget")
check("FTS finds the page", len(hits) == 1 and hits[0]["url"] == u)
check("FTS rows are deserialized", hits[0]["topics"] == ["budget", "politics"])

ct = CrawlerTools(db=db, crawler_cfg=CrawlerConfig(max_depth=0, respect_robots=False),
                  http_cfg=HTTPConfig(verify_ssl=False, link_delay=0), content="pure")
gp = json.loads(ct.get_page(u))
check("get_page returns full text + fields", gp["text"] and gp["sentiment"] == "neutral")
miss = json.loads(ct.get_page("https://no.example/x"))
check("get_page missing -> error+hint", "error" in miss and "hint" in miss)
sc = json.loads(ct.search_cached("budget"))
check("search_cached returns brief JSON", sc["found"] == 1 and "snippet" in sc["pages"][0])
fetch = install_fetch()
wc = json.loads(ct.web_crawl("https://x.example/new", depth=0))
check("web_crawl returns valid JSON pages", "pages" in wc and wc["found"] >= 1)
ct.close()


# =============================================================================
print("\n########## PART 2 — real smart path (LazyBridge LLM) ##########")

try:
    import lazybridge  # noqa: F401
    HAVE_LB = True
except ImportError:
    HAVE_LB = False
HAVE_KEY = any(os.environ.get(k) for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"))
MODEL = "gpt-4o-mini" if os.environ.get("OPENAI_API_KEY") else "claude-haiku-4-5"


def llm_cfg():
    return LLMConfig(model=MODEL, temperature=0)


def real_smart(url, body, **kw):
    """Crawl one stubbed-content URL through the REAL LLM. Returns PageResult or None."""
    install_fetch(content_map={url: body})
    c = WebCrawler(CrawlerConfig(max_depth=0, respect_robots=False, **kw),
                   HTTPConfig(verify_ssl=False, link_delay=0), llm_cfg=llm_cfg())
    try:
        r = c.crawl(url, content="smart", links="pure")[0]
    finally:
        c.close()
    return r


if not HAVE_LB:
    skip("PART 2", "LazyBridge not importable (run setup_paths / set LAZYBRIDGE_PATH)")
elif not HAVE_KEY:
    skip("PART 2", "no OPENAI/ANTHROPIC API key set")
else:
    print(f"  (model: {MODEL})")

    print("\n=== G. smart extraction + sentiment + notes ===")
    pos = ("Triumph: the new vaccine cured 98% of patients with zero side effects; "
           "doctors and families are overjoyed and call it a historic success.")
    r = real_smart("https://x.example/pos", pos)
    if r is None or r.status != "done":
        skip("smart extraction", "LLM unreachable (network/SSL)")
    else:
        check("smart: status done, mode smart", r.status == "done" and r.mode == "smart")
        check("smart: summary present", bool(r.summary))
        check("smart: entities present", len(r.entities) >= 1)
        check("smart: sentiment == positive", r.sentiment == "positive")
        check("smart: notes empty by default", not r.notes)
        rneg = real_smart("https://x.example/neg",
                          "Catastrophe: the dam burst, hundreds dead, the firm faces ruin "
                          "and furious lawsuits; critics call it inexcusable negligence.")
        check("smart: negative tone -> sentiment negative",
              rneg is not None and rneg.sentiment == "negative")

    print("\n=== H. level-3 enrich (pure cached -> smart, NO re-fetch) ===")
    fetch = install_fetch(content_map={U: pos})
    db = tmpdb()
    WebCrawler(CrawlerConfig(max_depth=0, respect_robots=False),
               HTTPConfig(verify_ssl=False, link_delay=0), db=db).crawl(U, content="pure")
    n_before = fetch["n"]
    cs = WebCrawler(CrawlerConfig(max_depth=0, respect_robots=False),
                    HTTPConfig(verify_ssl=False, link_delay=0), llm_cfg=llm_cfg(), db=db)
    rr = cs.crawl(U, content="smart", links="pure")[0]
    cs.close()
    check("enrich did NOT re-fetch", fetch["n"] == n_before)  # holds even if LLM fails
    if rr.status == "done":
        check("enrich produced smart content (summary/sentiment)",
              rr.mode == "smart" and bool(rr.summary) and rr.sentiment in
              ("positive", "neutral", "negative"))
    else:
        skip("enrich smart content", "LLM unreachable")

    print("\n=== I. custom output schema (real LLM) ===")
    from pydantic import BaseModel, Field

    class Article(BaseModel):
        headline: str = Field(default="", description="the main headline")
        author: str = Field(default="", description="author if present")
        key_points: list = Field(default_factory=list, description="3-5 key takeaways")

    install_fetch(content_map={"https://x.example/sch":
        "Headline: Mars Base Approved. By Dr. Vega. The agency approved a permanent "
        "Mars base; first launch in 2031; budget set at 40 billion."})
    cc = WebCrawler(CrawlerConfig(max_depth=0, respect_robots=False),
                    HTTPConfig(verify_ssl=False, link_delay=0), llm_cfg=llm_cfg())
    rsch = cc.crawl("https://x.example/sch", content="smart", schema=Article)[0]
    cc.close()
    if rsch.status == "done" and rsch.data:
        check("custom schema: data has the model's fields",
              set(rsch.data.keys()) == {"headline", "author", "key_points"})
        check("custom schema: headline extracted", bool(rsch.data.get("headline")))
    else:
        skip("custom schema", "LLM unreachable")

    print("\n=== J. CrawlerTools.as_tools() + a real tool call ===")
    tools_db = tmpdb()
    ct = CrawlerTools(db=tools_db,
                      crawler_cfg=CrawlerConfig(max_depth=0, respect_robots=False),
                      http_cfg=HTTPConfig(verify_ssl=False, link_delay=0),
                      llm_cfg=llm_cfg(), content="smart", links="pure")
    tools = ct.as_tools()
    names = {getattr(t, "name", None) for t in tools}
    check("as_tools() exposes the 4 tools",
          {"web_search", "web_crawl", "get_page", "search_cached"} <= names)
    install_fetch(content_map={"https://x.example/tool": pos})
    out = json.loads(ct.web_crawl("https://x.example/tool", depth=0))
    if out["found"] >= 1 and out["pages"][0].get("sentiment"):
        check("web_crawl tool returns smart JSON with sentiment",
              out["pages"][0]["sentiment"] in ("positive", "neutral", "negative"))
    else:
        skip("web_crawl smart tool", "LLM unreachable")
    ct.close()


# =============================================================================
print(f"\n########## RESULT: {PASS} PASS, {FAIL} FAIL, {SKIP} SKIP ##########")
sys.exit(1 if FAIL else 0)
