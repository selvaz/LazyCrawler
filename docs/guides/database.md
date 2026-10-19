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
    rows = con.execute(
        "SELECT session_id, created_at, seed FROM sessions ORDER BY created_at DESC"
    ).fetchall()
for row in rows:
    print(f"  {row[0]}: {row[2]} @ {row[1]}")
```

### Session origin and production readers

Every new session records who initiated it in `sessions.origin`. Resolution is:
an explicit `origin=` argument, then `LAZYCRAWLER_SESSION_ORIGIN`, then `"unknown"`.
`None` means no explicit argument. Empty strings are invalid rather than absent.
Labels must contain 1–64 printable characters and no whitespace; invalid selected
values log a warning and become `unknown`, even if a lower-priority value is valid.
Existing session IDs retain their original metadata and origin when reused.

`origin=` is optional on `CrawlerDB.create_session`, `WebCrawler.crawl` / `crawl_many`,
`AsyncWebCrawler.crawl` / `crawl_many`, `WebSearch.run` (all engines, including Gemini),
and `CrawlerTools.web_search` / `web_crawl` / `web_crawl_many`. The label describes
the session's caller; `source` continues to describe the mechanism (`crawl`,
`search:duckduckgo`, etc.). For example:

```python
crawler.crawl("https://example.com", origin="interactive:research", session_id="research-1")
print(db.get_session("research-1")["origin"])
sessions = db.get_sessions()  # full dicts including origin, newest first
```

Production launchers must set `LAZYCRAWLER_SESSION_ORIGIN` to a lowercase `job:`
label, for example `job:news`, `job:calendar`, or `job:committee-search`. Interactive
research should use another prefix, for example `interactive:research`.
**The production contract for readers is `origin LIKE 'job:%'`.** The exported helper
returns that SQL predicate, following SQLite's LIKE semantics (ASCII case-insensitive
by default). NULL historical origins and `unknown` are excluded.

Pages are shared across sessions: filter through `crawl_edges` and `sessions`,
using `EXISTS` to avoid duplicate pages. A page reached by both an interactive and
a production session qualifies via its production edge:

```python
from lazycrawler import production_session_filter

rows = db.conn.execute(f"""
    SELECT p.* FROM pages p
    WHERE p.status = 'done' AND EXISTS (
        SELECT 1 FROM crawl_edges e JOIN sessions s USING (session_id)
        WHERE e.url_hash = p.url_hash AND {production_session_filter()}
    )
""").fetchall()
```

### Migration and older readers

Schema version 3 adds nullable `sessions.origin TEXT` with no default using the
existing `PRAGMA user_version` forward-migration mechanism. The regular writer
opens old DBs and migrates them in place; existing rows remain NULL. Fresh DBs
have the same schema. `DBConfig(read_only=True)` skips migrations; the new session
read methods return `origin=None` when opening a pre-v3 file read-only.

The previous version does not reject higher schema versions or downgrade
`user_version`. Its queries and named-column inserts keep working on a v3 DB;
older writers leave origin NULL. External SQL readers selecting existing columns
also keep working. Consumers that unpack `SELECT *` positionally must account for
the extra column. The package version is unchanged.

### Classifying historical sessions

Use an explicit JSON rules file to classify only sessions whose origin is NULL:

```json
[
  {
    "origin": "job:news",
    "source": "crawl",
    "start": "2026-10-01T00:00:00Z",
    "end": "2026-10-10T00:00:00Z"
  },
  {"origin": "interactive:research", "source": "search:duckduckgo"}
]
```

Each rule requires a valid origin and at least one of `source`, `start`, `end`.
Source matches exactly. Time bounds must explicitly specify UTC (`Z` or `+00:00`);
start is inclusive, end exclusive, and either bound can be omitted. All selectors
within a rule must match; the first matching rule wins. Rules with unknown fields,
invalid origins, or invalid/reversed bounds fail before opening the DB. No rules
are built in. Stored timestamps are compared as instants, including offset-aware
timestamps; legacy naive stored timestamps are interpreted as UTC. Unparseable
stored timestamps cannot match time rules, but can match source-only rules.

```bash
python -m lazycrawler.backfill_session_origin --db /path/to/news.db --rules rules.json
python -m lazycrawler.backfill_session_origin --db /path/to/news.db --rules rules.json --apply
```

The default previews classifications and prints JSON counts per matched origin
plus `unmatched_null`. `--apply` writes the same classification in one transaction.
Unmatched rows stay NULL; any non-NULL origin (including `unknown`) is preserved.
Repeating an applied run reports no further matches for already classified rows.
The target file must already exist. Both modes use `CrawlerDB`'s normal writer,
WAL mode, shared connection lock, and 60-second SQLite busy timeout; a
`BEGIN IMMEDIATE` reservation protects selection and updates from competing
writers. **Dry-run does not write classifications, but opening an old file through
the normal writer can migrate its schema and enable WAL.**

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
db.search_text('"climate change"')  # exact phrase
db.search_text("climate change policy")  # all three words
db.search_text("clim*")  # prefix match
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
