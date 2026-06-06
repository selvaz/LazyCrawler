# CrawlerDB

SQLite-backed persistence layer. Enables 3-level deduplication, TTL caching, FTS5 full-text search, and session tracking across crawl runs.

```python
from lazycrawler import CrawlerDB
from lazycrawler.config import DBConfig
```

---

## Constructor

```python
CrawlerDB(cfg: DBConfig = DBConfig())
```

Opens (or creates) the SQLite database. WAL mode and foreign keys are enabled automatically.

---

## DBConfig

```python
from lazycrawler.config import DBConfig

cfg = DBConfig(
    db_path="lazycrawler.db",  # file path; ":memory:" for in-memory
    ttl_hours=24.0,            # cache TTL; page is fresh within this window
    force_refresh=False,       # True = ignore TTL, always re-fetch
    enable_fts=True,           # enable FTS5 full-text search index
)
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `db_path` | `str` | `"lazycrawler.db"` | Path to the SQLite file. Use `":memory:"` for tests |
| `ttl_hours` | `float` | `24.0` | Hours before a cached page is considered stale |
| `force_refresh` | `bool` | `False` | If `True`, ignore the TTL cache and always re-fetch |
| `enable_fts` | `bool` | `True` | Enable FTS5 full-text search index on `clean_text` |

---

## Schema overview

**`sessions`** — one row per crawl session

| Column | Type | Notes |
|---|---|---|
| `session_id` | TEXT PK | user-provided or auto UUID |
| `created_at` | TEXT | ISO timestamp |
| `topic` | TEXT | crawl topic |
| `seed` | TEXT | first seed URL |
| `mode` | TEXT | `"pure"` or `"smart"` |
| `source` | TEXT | `"crawl"`, `"search"`, etc. |

**`pages`** — one row per unique URL

| Column | Type | Notes |
|---|---|---|
| `url_hash` | TEXT PK | SHA-256 of URL |
| `url` | TEXT | original URL |
| `status` | TEXT | `"done"`, `"fetch_error"`, etc. |
| `mode` | TEXT | extraction mode used |
| `fetched_at` | TEXT | ISO timestamp of last fetch |
| `content_hash` | TEXT | SHA-256 of raw text |
| `title` | TEXT | |
| `raw_text` | TEXT | text before LLM processing |
| `clean_text` | TEXT | final text (LLM-cleaned or raw) |
| `summary` | TEXT | smart mode only |
| `entities` | TEXT | JSON array |
| `topics` | TEXT | JSON array |
| `sentiment` | TEXT | |
| `is_pdf` | INTEGER | 0 or 1 |
| `published_iso` | TEXT | |
| `extract_json` | TEXT | custom schema data (JSON) |
| `error` | TEXT | error message if failed |

**`crawl_edges`** — links between pages in a session

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `session_id` | TEXT FK → sessions | |
| `url_hash` | TEXT FK → pages | |
| `source_url` | TEXT | which page linked here |
| `depth` | INTEGER | crawl depth |
| `visited_at` | TEXT | ISO timestamp |

---

## Methods

### create_session()

```python
def create_session(
    session_id: str,
    *,
    topic: str,
    seed: str,
    mode: str,
    source: str,
) -> str
```

Create a new session record. Returns `session_id`. Called automatically by `WebCrawler` when a `db` is provided.

---

### get_fresh_page()

```python
def get_fresh_page(url: str) -> dict | None
```

Return the cached page dict if it exists and was fetched within `ttl_hours`. Returns `None` if not found or stale.

---

### is_fresh()

```python
def is_fresh(url: str) -> bool
```

Return `True` if the URL has a non-stale cache entry.

```python
if db.is_fresh("https://example.com/page"):
    print("Cached — no fetch needed")
```

---

### find_by_content_hash()

```python
def find_by_content_hash(content_hash: str) -> dict | None
```

Level-2 dedup: look up a page by its content hash. Returns the page dict or `None`.

---

### get_page()

```python
def get_page(url_hash: str) -> dict | None
```

Retrieve a page by its URL hash (SHA-256 of the URL). Returns the raw DB row as a dict.

---

### upsert_page()

```python
def upsert_page(page: dict) -> str
```

Insert or update a page record. Returns the `url_hash`. Called automatically by `WebCrawler`.

---

### add_edge()

```python
def add_edge(
    session_id: str,
    url_hash: str,
    *,
    source_url: str,
    depth: int,
) -> None
```

Record a crawl edge (page visited in a session). Called automatically by `WebCrawler`.

---

### get_pages()

```python
def get_pages(
    session_id: str | None = None,
    status: str = "done",
    limit: int = 0,
) -> list[dict]
```

Retrieve pages from the DB.

| Parameter | Description |
|---|---|
| `session_id` | Filter by session. `None` = all sessions |
| `status` | Filter by status. `"done"` returns only successful pages |
| `limit` | Max rows. `0` = no limit |

---

### search_text()

```python
def search_text(query: str, limit: int = 20) -> list[dict]
```

Full-text search across `clean_text`. Uses FTS5 if `enable_fts=True`, falls back to `LIKE` otherwise.

```python
hits = db.search_text("machine learning", limit=10)
for page in hits:
    print(page["url"], page["title"])
```

---

### stats()

```python
def stats() -> dict
```

Return aggregate counts: `{"sessions": int, "pages": int, "pages_done": int, "edges": int}`.

---

### close()

```python
def close() -> None
```

Flush and close the SQLite connection.

---

## Examples

### Crawl with DB

```python
from lazycrawler import WebCrawler, CrawlerDB
from lazycrawler.config import CrawlerConfig, DBConfig

db = CrawlerDB(DBConfig(db_path="my.db", ttl_hours=24.0))
crawler = WebCrawler(
    crawler_cfg=CrawlerConfig(max_pages=20),
    db=db,
)
results = crawler.crawl("https://example.com", session_id="run-1")
crawler.close()

pages = db.get_pages(session_id="run-1")
print(f"Session run-1: {len(pages)} done pages")
db.close()
```

### Check freshness before manual crawl

```python
url = "https://example.com/article/123"
if db.is_fresh(url):
    cached = db.get_fresh_page(url)
    text = cached["clean_text"]
else:
    # fetch and process manually
    ...
```

### Full-text search

```python
hits = db.search_text("climate change policy")
for h in hits:
    print(f"  [{h['url']}] {h['title']}")
    print(f"    {(h.get('clean_text') or '')[:150]}")
```

### Force refresh

```python
db = CrawlerDB(DBConfig(db_path="my.db", force_refresh=True))
# TTL is ignored — every URL is re-fetched
```

### Get all pages across all sessions

```python
all_done = db.get_pages(status="done")
errors = db.get_pages(status="fetch_error")
print(f"Done: {len(all_done)}, Errors: {len(errors)}")
```

### Stats after crawl

```python
s = db.stats()
print(f"Sessions: {s['sessions']}")
print(f"Pages total: {s['pages']} (done: {s['pages_done']})")
print(f"Edges: {s['edges']}")
```

### Use from external tools

The SQLite file is standard and can be opened with:

- [DB Browser for SQLite](https://sqlitebrowser.org/) — GUI viewer
- `sqlite3 my.db .tables` — CLI
- pandas: `pd.read_sql("SELECT * FROM pages WHERE status='done'", con)`
