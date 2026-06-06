# WebCrawler

The main crawling engine. Fetches pages recursively, extracts text, and optionally uses an LLM for structured extraction and topic-guided link selection.

```python
from lazycrawler import WebCrawler
from lazycrawler.config import CrawlerConfig, HTTPConfig, LLMConfig, DBConfig
from lazycrawler import CrawlerDB
```

---

## Constructor

```python
WebCrawler(
    crawler_cfg: CrawlerConfig = CrawlerConfig(),
    http_cfg: HTTPConfig = HTTPConfig(),
    llm_cfg: LLMConfig | None = None,
    db: CrawlerDB | None = None,
)
```

| Parameter | Type | Description |
|---|---|---|
| `crawler_cfg` | `CrawlerConfig` | Depth, page limits, link limits, domain filtering, blacklist |
| `http_cfg` | `HTTPConfig` | Timeouts, retries, SSL, delay, JS rendering |
| `llm_cfg` | `LLMConfig \| None` | Model config for smart mode. `None` = pure mode only |
| `db` | `CrawlerDB \| None` | Persistent cache/dedup. `None` = no persistence |

All parameters are optional — defaults work out of the box for basic pure-mode crawling.

---

## crawl()

```python
def crawl(
    url: str,
    *,
    mode: Literal["pure", "smart"] = "pure",
    content: Literal["pure", "smart"] | None = None,
    links: Literal["pure", "smart"] | None = None,
    topic: str = "",
    schema: type | None = None,
    session_id: str | None = None,
) -> list[PageResult]
```

Crawl a single seed URL recursively.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `url` | `str` | required | Seed URL to start from |
| `mode` | `"pure"` or `"smart"` | `"pure"` | Sets both content and links mode at once |
| `content` | `"pure"` or `"smart"` or `None` | `None` | Override content extraction mode only |
| `links` | `"pure"` or `"smart"` or `None` | `None` | Override link selection mode only |
| `topic` | `str` | `""` | Topic description for smart link selection and LLM context |
| `schema` | Pydantic `BaseModel` subclass or `None` | `None` | Custom output schema (smart mode only). Result goes into `PageResult.data` |
| `session_id` | `str \| None` | `None` | DB session identifier. Auto-generated if `None` and `db` is set |

**Returns**: `list[PageResult]` — one entry per page visited (including errors).

**Priority**: `content` / `links` override `mode`. So `crawl(url, mode="smart", links="pure")` uses smart content with pure link selection.

---

## crawl_many()

```python
def crawl_many(
    urls: list[str],
    *,
    mode: Literal["pure", "smart"] = "pure",
    content: Literal["pure", "smart"] | None = None,
    links: Literal["pure", "smart"] | None = None,
    topic: str = "",
    schema: type | None = None,
    session_id: str | None = None,
    source: str = "crawl",
) -> list[PageResult]
```

Crawl multiple seed URLs, collecting results from all of them into a single list. The `max_pages` limit is shared across all seeds.

| Parameter | Type | Description |
|---|---|---|
| `urls` | `list[str]` | List of seed URLs |
| `source` | `str` | Label stored in the DB session (default `"crawl"`) |

All other parameters are identical to `crawl()`.

---

## close()

```python
def close() -> None
```

Release HTTP client connections and Playwright browser (if used). Always call this when done, or use a `try/finally` block.

---

## Mode combinations

| `mode=` | `content=` | `links=` | Behavior |
|---|---|---|---|
| `"pure"` | — | — | Trafilatura text, heuristic links |
| `"smart"` | — | — | LLM extraction, LLM link selection |
| any | `"smart"` | `"pure"` | LLM extraction, heuristic links (cheaper) |
| any | `"pure"` | `"smart"` | Plain text, LLM picks next links |
| `"pure"` | `"smart"` | — | LLM extraction, heuristic links |

---

## Examples

### Basic pure mode

```python
from lazycrawler import WebCrawler

crawler = WebCrawler()
results = crawler.crawl("https://news.ycombinator.com", mode="pure")
crawler.close()

for r in results:
    if r.status == "done":
        print(f"{r.url}: {len(r.text or '')} chars")
```

### Smart mode — structured extraction

```python
from lazycrawler import WebCrawler
from lazycrawler.config import LLMConfig

llm_cfg = LLMConfig(model="gpt-4o-mini")
crawler = WebCrawler(llm_cfg=llm_cfg)
results = crawler.crawl("https://techcrunch.com", mode="smart", topic="AI startups")
crawler.close()

for r in results:
    if r.status == "done":
        print(f"\n{r.url}")
        print(f"  title:    {r.title}")
        print(f"  summary:  {r.summary}")
        print(f"  entities: {r.entities}")
        print(f"  topics:   {r.topics}")
        print(f"  sentiment:{r.sentiment}")
```

### Smart content, heuristic links (cheaper)

```python
# LLM extracts structured info, but link selection is heuristic
results = crawler.crawl(
    "https://example.com",
    content="smart",
    links="pure",
)
```

### LLM-guided link traversal, pure content

```python
# LLM picks which links to follow based on topic, but no LLM extraction
results = crawler.crawl(
    "https://wikipedia.org/wiki/Machine_learning",
    content="pure",
    links="smart",
    topic="neural networks and deep learning",
)
```

### Custom Pydantic schema

```python
from pydantic import BaseModel, Field
from lazycrawler import WebCrawler
from lazycrawler.config import LLMConfig

class Article(BaseModel):
    headline: str = Field(description="Article headline")
    author: str = Field(description="Author name or empty string")
    published: str = Field(description="Publication date (ISO or empty)")
    body_summary: str = Field(description="2-sentence summary of the article body")

llm_cfg = LLMConfig(model="gpt-4o-mini")
crawler = WebCrawler(llm_cfg=llm_cfg)
results = crawler.crawl(
    "https://techcrunch.com",
    content="smart",
    schema=Article,
)
crawler.close()

for r in results:
    if r.data:
        a = Article(**r.data)
        print(f"{a.headline} — {a.author} ({a.published})")
```

### With persistent database

```python
from lazycrawler import WebCrawler, CrawlerDB
from lazycrawler.config import CrawlerConfig, DBConfig

db = CrawlerDB(DBConfig(db_path="my_crawl.db", ttl_hours=48.0))
crawler = WebCrawler(
    crawler_cfg=CrawlerConfig(max_depth=3, max_pages=50),
    db=db,
)
results = crawler.crawl("https://example.com", session_id="run-001")
crawler.close()

pages = db.get_pages(session_id="run-001")
print(f"DB has {len(pages)} pages for run-001")
db.close()
```

### Parallel crawling

```python
from lazycrawler import WebCrawler
from lazycrawler.config import CrawlerConfig, HTTPConfig

crawler = WebCrawler(
    crawler_cfg=CrawlerConfig(max_workers=4, max_pages=40, max_depth=2),
    http_cfg=HTTPConfig(link_delay=0.5),
)
results = crawler.crawl("https://example.com")
crawler.close()
```

### Blacklist domains or URL patterns

```python
from lazycrawler.config import CrawlerConfig

cfg = CrawlerConfig(
    blacklist=[
        "https://example.com/login",
        "https://example.com/cart",
        "ads.example.com",
    ]
)
```

### Cross-domain crawl

```python
from lazycrawler.config import CrawlerConfig

cfg = CrawlerConfig(same_domain_only=False, max_depth=1, max_pages=30)
crawler = WebCrawler(crawler_cfg=cfg)
results = crawler.crawl("https://example.com")
```

### Multiple seed URLs

```python
seeds = [
    "https://arxiv.org/abs/2301.00001",
    "https://arxiv.org/abs/2301.00002",
    "https://arxiv.org/abs/2301.00003",
]
results = crawler.crawl_many(seeds, mode="pure", session_id="arxiv-batch")
```
