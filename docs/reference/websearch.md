# WebSearch

Seeds a crawl from a search engine — DuckDuckGo or Gemini — then optionally crawls the result pages.

```python
from lazycrawler import WebSearch, search_ddg_urls
from lazycrawler.config import SearchConfig, CrawlerConfig, LLMConfig
```

---

## Constructor

```python
WebSearch(
    search_cfg: SearchConfig = SearchConfig(),
    crawler_cfg: CrawlerConfig = CrawlerConfig(),
    http_cfg: HTTPConfig = HTTPConfig(),
    llm_cfg: LLMConfig | None = None,
    db: CrawlerDB | None = None,
)
```

All parameters are optional. `WebSearch` creates an internal `WebCrawler` with the provided configs.

---

## run()

```python
def run(
    query: str,
    *,
    mode: Literal["pure", "smart"] = "pure",
    content: Literal["pure", "smart"] | None = None,
    links: Literal["pure", "smart"] | None = None,
    session_id: str | None = None,
    max_results: int | None = None,
) -> dict
```

Run a web search and optionally crawl the result pages.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `query` | `str` | required | Search query |
| `mode` | `"pure"` or `"smart"` | `"pure"` | Extraction mode for crawled pages |
| `content` | `"pure"` or `"smart"` or `None` | `None` | Override content mode |
| `links` | `"pure"` or `"smart"` or `None` | `None` | Override link mode |
| `session_id` | `str \| None` | `None` | DB session ID |
| `max_results` | `int \| None` | `None` | Override `SearchConfig.n_results` |

### Return value

```python
{
    "query": str,        # original query
    "topic": str,        # expanded topic (if expand_topic=True)
    "engine": str,       # "duckduckgo" or "gemini"
    "pages_found": int,  # number of PageResult objects
    "results": list[PageResult],
}
```

The `results` list contains `PageResult` objects (see [PageResult reference](pageresult.md)).

---

## SearchConfig

```python
from lazycrawler.config import SearchConfig

cfg = SearchConfig(
    engine="duckduckgo",        # or "gemini"
    n_results=10,               # how many search results to fetch/crawl
    crawl_depth=0,              # depth to crawl each result page (0=just the page itself)
    same_domain_only=False,     # when crawl_depth>0, cross-domain is usually wanted
    expand_topic=True,          # use LLM to expand query into a topic description
    gemini_model="gemini-3-flash-preview",  # model for Gemini grounded search
)
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `engine` | `str` | `"duckduckgo"` | Search engine: `"duckduckgo"` or `"gemini"` |
| `n_results` | `int` | `10` | Number of search result URLs to process |
| `crawl_depth` | `int` | `0` | Crawl depth for each result page (0 = fetch only the result page) |
| `same_domain_only` | `bool` | `False` | When `crawl_depth > 0`, whether to stay on the result domain |
| `expand_topic` | `bool` | `True` | Use LLM to expand query into a richer topic description for link selection |
| `gemini_model` | `str` | `"gemini-3-flash-preview"` | Model for Gemini grounded search (requires Google API key) |

### Engine comparison

| | DuckDuckGo | Gemini |
|---|---|---|
| **Requires API key** | No | Yes (Google API key) |
| **Result type** | URLs from DDG SERP | AI-grounded, includes synthesis |
| **Best for** | General search, no account needed | Research with grounded AI answers |
| **Rate limits** | Yes (unofficial API) | Per Google quota |

---

## search_ddg_urls()

```python
from lazycrawler import search_ddg_urls

urls = search_ddg_urls(
    query: str,
    max_results: int,
    blacklist: list[str] | None = None,
) -> list[str]
```

Standalone function — returns a list of URLs from DuckDuckGo without crawling. Useful when you want to search and then process results yourself.

| Parameter | Type | Description |
|---|---|---|
| `query` | `str` | Search query |
| `max_results` | `int` | Max number of URLs to return |
| `blacklist` | `list[str] \| None` | URL patterns to exclude |

---

## Examples

### Basic DDG search

```python
from lazycrawler import WebSearch
from lazycrawler.config import SearchConfig

search = WebSearch(search_cfg=SearchConfig(n_results=5))
result = search.run("python asyncio best practices")
search.close()

print(f"Found {result['pages_found']} pages")
for r in result["results"]:
    print(f"  {r.url}: {len(r.text or '')} chars")
```

### Smart mode research

```python
from lazycrawler import WebSearch
from lazycrawler.config import SearchConfig, LLMConfig

search = WebSearch(
    search_cfg=SearchConfig(n_results=8, crawl_depth=1, expand_topic=True),
    llm_cfg=LLMConfig(model="gpt-4o-mini"),
)
result = search.run("transformer architecture explained", mode="smart")
search.close()

for r in result["results"]:
    if r.summary:
        print(f"\n{r.url}")
        print(f"  {r.summary}")
        print(f"  Topics: {r.topics}")
```

### Gemini grounded search

```python
from lazycrawler.config import SearchConfig

# Requires GOOGLE_API_KEY environment variable
search_cfg = SearchConfig(engine="gemini", n_results=5)
search = WebSearch(search_cfg=search_cfg, llm_cfg=LLMConfig(model="gemini-3-flash-preview"))
result = search.run("latest AI safety research 2024", mode="smart")
```

### With persistent DB (no re-fetch on second run)

```python
from lazycrawler import WebSearch, CrawlerDB
from lazycrawler.config import SearchConfig, DBConfig

db = CrawlerDB(DBConfig(db_path="search.db", ttl_hours=12.0))
search = WebSearch(search_cfg=SearchConfig(n_results=10), db=db)

# First run: fetches pages
result1 = search.run("climate change news", session_id="s1")

# Second run within 12 hours: returns cached results
result2 = search.run("climate change news", session_id="s2")
cache_hits = sum(1 for r in result2["results"] if r.from_cache)
print(f"Cache hits: {cache_hits}")

search.close()
db.close()
```

### search_ddg_urls standalone

```python
from lazycrawler import search_ddg_urls

urls = search_ddg_urls("machine learning tutorials", max_results=10)
print(f"Got {len(urls)} URLs")
for url in urls:
    print(f"  {url}")
```

### Deep crawl from search

```python
# Search returns 5 URLs, then crawls each 2 levels deep
from lazycrawler.config import SearchConfig, CrawlerConfig

search = WebSearch(
    search_cfg=SearchConfig(n_results=5, crawl_depth=2, same_domain_only=True),
    crawler_cfg=CrawlerConfig(max_pages=20),
)
result = search.run("python web crawling libraries")
```
