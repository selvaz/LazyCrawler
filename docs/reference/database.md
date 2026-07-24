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
    ttl_hours=24.0,  # cache TTL; page is fresh within this window
    force_refresh=False,  # True = ignore TTL, always re-fetch
    enable_fts=True,  # enable FTS5 full-text search index
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
| `mode` | TEXT | `"pure"` / `"ml"` / `"smart"` |
| `source` | TEXT | `"crawl"`, `"search:duckduckgo"`, etc. |

**`pages`** — global content cache, one row per unique URL (cross-session)

| Column | Type | Notes |
|---|---|---|
| `url_hash` | TEXT PK | SHA-256 of normalized URL |
| `url` | TEXT | original URL |
| `domain` | TEXT | host (indexed) |
| `status` | TEXT | `"done"`, `"fetch_error"`, etc. |
| `mode` | TEXT | extraction mode used (`pure`/`ml`/`smart`) |
| `crawled_at` | TEXT | ISO timestamp of last crawl (drives TTL) |
| `content_hash` | TEXT | SHA-256 of raw text (level-2 dedup) |
| `title` | TEXT | |
| `raw_text` | TEXT | text before LLM/ML processing |
| `clean_text` | TEXT | final text (cleaned) |
| `summary` | TEXT | smart / ml |
| `entities_json` / `topics_json` | TEXT | JSON arrays |
| `sentiment` | TEXT | smart / ml |
| `notes` | TEXT | reserved (smart) |
| `markdown` | TEXT | HTML→Markdown render (`emit_markdown`) |
| `is_pdf` | INTEGER | 0 or 1 |
| `published_iso` | TEXT | |
| `extract_json` | TEXT | custom-schema data (JSON) |
| `links_json` | TEXT | candidate links found at crawl time (for `recurse_from_cache`) |
| `error` | TEXT | error message if failed |

**`crawl_edges`** — which session reached which page (provenance), `UNIQUE(session_id, url_hash)`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `session_id` | TEXT FK → sessions | ON DELETE CASCADE |
| `url_hash` | TEXT FK → pages | ON DELETE CASCADE |
| `source_url` | TEXT | which page linked here |
| `depth` | INTEGER | crawl depth |
| `added_at` | TEXT | ISO timestamp |

**`artifacts`** — non-textual content per page (tables/images/charts/svg), `UNIQUE(url_hash, content_hash)`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `url_hash` | TEXT FK → pages | ON DELETE CASCADE |
| `artifact_type` | TEXT | `table` / `image` / `chart` / `svg` |
| `position` | INTEGER | order on the page |
| `src_url`, `alt`, `caption`, `context` | TEXT | provenance |
| `content`, `content_format` | TEXT | e.g. Markdown table / SVG markup |
| `data_json` | TEXT | structured rows / chart data |
| `summary` | TEXT | vision-LLM enrichment (smart) |
| `mime`, `width`, `height`, `bytes_hash`, `size_bytes` | | image metadata |
| `blob` | BLOB | downloaded image bytes (optional) |
| `content_hash` | TEXT | dedup + anchor join key |

> Schema migrations are gated by `PRAGMA user_version` (forward-only, idempotent).

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

### get_artifacts()

```python
def get_artifacts(
    url_hash: str | None = None,
    session_id: str | None = None,
    artifact_type: str | None = None,
    content_hash: str | None = None,
    include_blob: bool = False,
    limit: int = 0,
) -> list[dict]
```

Retrieve a page's (or a whole session's) artifacts — tables, images, charts, SVG.
`data`/`meta` are deserialized; the raw `blob` is dropped unless `include_blob=True`.
See the [Artifacts guide](../guides/artifacts.md).

`content_hash` filters by the stable content join key (the same hash in
`[[artifact:<hash>]]` anchors and downstream `crawler:<hash>` artifact refs).
It is unique only per page (`UNIQUE(url_hash, content_hash)`), so the same
image/table across several pages matches one row per page — some possibly
without a downloaded `blob`. Pair it with `url_hash` to guarantee a single
row (`session_id` only narrows to that session — a session that crawled the
same content on two pages still returns both). When resolving a bare
`crawler:<hash>`, pick a match that actually carries bytes.

```python
from lazycrawler.http import url_hash

arts = db.get_artifacts(url_hash=url_hash("https://example.com/report"))
tables = db.get_artifacts(session_id="run-1", artifact_type="table")
one = db.get_artifacts(content_hash="9f8e…", include_blob=True)  # by content hash
```

---

### add_artifacts()

```python
def add_artifacts(url_hash: str, artifacts: list[Artifact]) -> int
```

Persist a page's artifacts (idempotent per `(url_hash, content_hash)`). Returns the
number inserted. Called automatically by `WebCrawler` when `extract_artifacts=True`.

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
