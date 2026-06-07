# Database & Caching

`CrawlerDB` provides SQLite-backed persistence: URL+TTL cache, content-hash dedup, FTS5 search, and session tracking. It is optional — the crawler works without it, but without a DB, every URL is re-fetched on every run.

---

## Why use a DB?

- **No re-fetching**: pages cached within `ttl_hours` are returned instantly
- **No duplicate LLM calls**: same content (different URL) is not re-processed
- **Cross-run continuity**: crawl today, pick up tomorrow from the cache
- **Search your corpus**: `db.search_text("query")` across all collected pages
- **Session tracking**: query pages by crawl run, compare runs

---

## Basic setup

```python
from lazycrawler import WebCrawler, CrawlerDB
from lazycrawler.config import CrawlerConfig, DBConfig

db = CrawlerDB(DBConfig(db_path="my_crawl.db", ttl_hours=24.0))

crawler = WebCrawler(
    crawler_cfg=CrawlerConfig(max_depth=2, max_pages=30),
    db=db,
)
results = crawler.crawl("https://example.com", mode="pure", session_id="run-1")
crawler.close()
db.close()
```

---

## TTL caching

`ttl_hours` controls how long a cached page is considered fresh. The default is 24 hours.

```python
# Pages cached for 7 days
db = CrawlerDB(DBConfig(db_path="my.db", ttl_hours=168.0))

# Check freshness manually
if db.is_fresh("https://example.com/page"):
    print("Cached and fresh — no HTTP needed")
else:
    print("Stale or not crawled yet")
```

### Force refresh (ignore TTL)

```python
db = CrawlerDB(DBConfig(db_path="my.db", force_refresh=True))
# All URLs will be re-fetched regardless of cache age
```

---

## 3-level deduplication

```
Request URL
    │
    ▼
Level 1: is URL in DB and fresh (within TTL)?
    ├── YES → return cached PageResult (no HTTP)
    └── NO  → fetch page
                │
                ▼
           Level 2: is content_hash already in DB?
                ├── YES → reuse existing extraction (skip LLM)
                └── NO  → extract (pure/ml/smart)
                               │
                               ▼
                          Level 3: was this page pure before, now smart?
                               ├── YES → run LLM on cached text (no re-fetch)
                               └── NO  → store and return
```

### Seeing dedup in action

```python
# Run 1: 4 pages fetched and stored
results1 = crawler.crawl("https://example.com", mode="pure", session_id="run-1")
print([r.from_cache for r in results1])  # [False, False, False, False]

# Run 2 (within TTL): all from cache
results2 = crawler.crawl("https://example.com", mode="pure", session_id="run-2")
print([r.from_cache for r in results2])  # [True, True, True, True]
```

---

## Session management

Each crawl gets a `session_id`. Provide one to query later:

```python
results = crawler.crawl("https://example.com", session_id="weekly-news-2024-01-15")

# Later: retrieve pages from that session
pages = db.get_pages(session_id="weekly-news-2024-01-15")
for p in pages:
    print(p["url"], p["title"])
```

### List all sessions

```python
import sqlite3

with sqlite3.connect("my.db") as con:
    rows = con.execute("SELECT session_id, created_at, seed FROM sessions ORDER BY created_at DESC").fetchall()
for row in rows:
    print(f"  {row[0]}: {row[2]} @ {row[1]}")
```

---

## Full-text search

Requires `enable_fts=True` (default). Uses SQLite FTS5; falls back to `LIKE` if FTS is disabled.

```python
hits = db.search_text("machine learning", limit=10)
for h in hits:
    print(f"\n{h['url']}")
    print(f"  {h['title']}")
    print(f"  {(h.get('clean_text') or '')[:200]}")
```

FTS5 supports phrase search and prefix search:

```python
db.search_text('"climate change"')          # exact phrase
db.search_text('climate change policy')     # all three words
db.search_text('clim*')                     # prefix match
```

---

## Stats

```python
s = db.stats()
print(f"Sessions:   {s['sessions']}")
print(f"Pages:      {s['pages']} total, {s['pages_done']} done")
print(f"Edges:      {s['edges']}")
```

---

## Multiple crawl runs on the same DB

Runs accumulate — pages are not deleted between runs. The DB grows as you crawl more sites. Dedup prevents duplicates within the TTL window.

```python
# Day 1
results = crawler.crawl("https://site-a.com", session_id="day1-a")
results = crawler.crawl("https://site-b.com", session_id="day1-b")

# Day 2 (site-a still fresh, site-b stale)
results = crawler.crawl("https://site-a.com", session_id="day2-a")  # from cache
results = crawler.crawl("https://site-c.com", session_id="day2-c")  # new fetch
```

---

## Opening DB externally

The SQLite file is a standard database. Open it with any SQLite tool:

```bash
# CLI
sqlite3 my_crawl.db
sqlite> SELECT url, title, status FROM pages WHERE status='done' LIMIT 5;

# Python
import sqlite3, pandas as pd
df = pd.read_sql("SELECT url, title, clean_text FROM pages WHERE status='done'",
                 sqlite3.connect("my_crawl.db"))
```

Or use [DB Browser for SQLite](https://sqlitebrowser.org/) for a GUI.
