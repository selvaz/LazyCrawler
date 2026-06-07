# WebSearch

Seeds a crawl from a search engine — DuckDuckGo, Brave, Tavily, or Gemini — then optionally crawls the result pages.

```python
from lazycrawler import WebSearch, search_ddg_urls, search_brave_urls, search_tavily_urls
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
    ml_cfg: MLConfig | None = None,
)
```

All parameters are optional. `WebSearch` creates an internal `WebCrawler` with the
provided configs (`ml_cfg` enables `mode="ml"` for the crawl of the results).

---

## run()

```python
def run(
    query: str,
    *,
    mode: Literal["pure", "ml", "smart"] = "pure",
    content: Literal["pure", "ml", "smart"] | None = None,
    links: Literal["pure", "ml", "smart"] | None = None,
    session_id: str | None = None,
    max_results: int | None = None,
    overrides: dict | None = None,
    timelimit: str | None = None,
) -> dict
```

Run a web search and optionally crawl the result pages.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `query` | `str` | required | Search query |
| `mode` | `"pure"` / `"ml"` / `"smart"` | `"pure"` | Extraction mode for crawled pages |
| `content` | `"pure"` / `"ml"` / `"smart"` / `None` | `None` | Override content mode |
| `links` | `"pure"` / `"ml"` / `"smart"` / `None` | `None` | Override link mode |
| `session_id` | `str \| None` | `None` | DB session ID |
| `max_results` | `int \| None` | `None` | Override `SearchConfig.n_results` |
| `overrides` | `dict \| None` | `None` | Per-call `CrawlerConfig` overrides applied to the crawl (the preset mechanism) |
| `timelimit` | `str \| None` | `None` | Per-call recency override: `"d"`/`"w"`/`"m"`/`"y"` |

### Return value

```python
{
    "query": str,        # original query
    "topic": str,        # expanded topic (if expand_topic=True)
    "engine": str,       # "duckduckgo", "brave", "tavily", or "gemini"
    "pages_found": int,  # number of PageResult objects with status="done"
    "results": list[PageResult],
}
```

The `results` list contains `PageResult` objects (see [PageResult reference](pageresult.md)).

---

## SearchConfig

```python
from lazycrawler.config import SearchConfig

# DuckDuckGo (no API key required)
cfg = SearchConfig(
    engine="duckduckgo",
    n_results=10,
    region="us-en",
    timelimit="w",      # past week
    safesearch="moderate",
)

# Brave Search
cfg = SearchConfig(
    engine="brave",
    n_results=10,
    brave_api_key="YOUR_KEY",   # or set BRAVE_API_KEY env var
    region="us-en",
    timelimit="w",
)

# Tavily Search
cfg = SearchConfig(
    engine="tavily",
    n_results=10,
    tavily_api_key="YOUR_KEY",  # or set TAVILY_API_KEY env var
    tavily_search_depth="advanced",
    timelimit="m",
)

# Gemini grounded answer
cfg = SearchConfig(
    engine="gemini",
    gemini_model="gemini-3-flash-preview",
)
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `engine` | `str` | `"duckduckgo"` | Search engine: `"duckduckgo"`, `"brave"`, `"tavily"`, or `"gemini"` |
| `n_results` | `int` | `10` | Number of search result URLs to process |
| `crawl_depth` | `int` | `0` | Crawl depth for each result page (0 = fetch only the result page) |
| `same_domain_only` | `bool` | `False` | When `crawl_depth > 0`, whether to stay on the result domain |
| `expand_topic` | `bool` | `True` | Use LLM to expand query into a richer topic description for link selection |
| `gemini_model` | `str` | `"gemini-3-flash-preview"` | Model for Gemini grounded search (requires Google API key) |
| `region` | `str` | `"wt-wt"` | Region code: `"us-en"`, `"gb-en"`, `"wt-wt"` (global). Used by DuckDuckGo and Brave |
| `timelimit` | `str \| None` | `None` | Time filter: `"d"` (day), `"w"` (week), `"m"` (month), `"y"` (year). Supported by DuckDuckGo, Brave and Tavily |
| `safesearch` | `str` | `"moderate"` | Safe-search: `"off"`, `"moderate"`, `"strict"`. Supported by DuckDuckGo and Brave |
| `backend` | `str` | `"auto"` | DuckDuckGo backend passed through to ddgs. Ignored by other engines |
| `brave_api_key` | `str` | `""` | Brave Search API key. Falls back to `BRAVE_API_KEY` env var. Required for `engine="brave"` |
| `tavily_api_key` | `str` | `""` | Tavily API key. Falls back to `TAVILY_API_KEY` env var. Required for `engine="tavily"` |
| `tavily_search_depth` | `str` | `"basic"` | Tavily depth: `"basic"` (faster, 1 credit) or `"advanced"` (deeper, 2 credits) |

### Engine comparison

| | DuckDuckGo | Brave | Tavily | Gemini |
|---|---|---|---|---|
| **Requires API key** | No | Yes | Yes | Yes (Google) |
| **Free tier** | Unlimited (unofficial) | 2 000 req/month | 1 000 req/month | Per Google quota |
| **Index** | DDG index | Own index (not Google/Bing) | Web-optimised for LLM agents | Google Search grounding |
| **Result type** | URLs from SERP | URLs from SERP | URLs + pre-cleaned snippets | AI-grounded answer (single result) |
| **timelimit support** | Yes | Yes | Yes | No |
| **region support** | Yes | Yes (country code) | No | No |
| **Best for** | Quick search, no setup | Privacy-first, own index | RAG pipelines, LLM agents | Research with grounded AI answers |
| **Extra Python deps** | `ddgs` | None (`requests`) | None (`requests`) | LazyBridge |

---

## Standalone search functions

### search_ddg_urls()

```python
from lazycrawler import search_ddg_urls

urls = search_ddg_urls(
    query: str,
    max_results: int,
    blacklist: list[str] | None = None,
    *,
    region: str = "wt-wt",
    safesearch: str = "moderate",
    timelimit: str | None = None,
    backend: str = "auto",
) -> list[str]
```

Returns URLs from DuckDuckGo without crawling. Requires `pip install ddgs`.

### search_brave_urls()

```python
from lazycrawler import search_brave_urls

urls = search_brave_urls(
    query: str,
    max_results: int,
    api_key: str = "",             # or BRAVE_API_KEY env var
    blacklist: list[str] | None = None,
    *,
    safesearch: str = "moderate",
    timelimit: str | None = None,
    region: str = "wt-wt",
) -> list[str]
```

Returns URLs from the Brave Search API. Raises `RuntimeError` if no API key is found.
Get a free key at <https://brave.com/search/api/>.

### search_tavily_urls()

```python
from lazycrawler import search_tavily_urls

urls = search_tavily_urls(
    query: str,
    max_results: int,
    api_key: str = "",             # or TAVILY_API_KEY env var
    blacklist: list[str] | None = None,
    *,
    search_depth: str = "basic",   # "basic" or "advanced"
    timelimit: str | None = None,
) -> list[str]
```

Returns URLs from the Tavily Search API. Raises `RuntimeError` if no API key is found.
Get a free key at <https://tavily.com/>.

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

### Brave Search

```python
from lazycrawler import WebSearch
from lazycrawler.config import SearchConfig

# API key via config or BRAVE_API_KEY env var
search = WebSearch(
    search_cfg=SearchConfig(
        engine="brave",
        n_results=8,
        brave_api_key="YOUR_KEY",
        region="us-en",
        timelimit="w",
    )
)
result = search.run("python web frameworks 2025", mode="pure")
search.close()
```

### Tavily Search (optimised for LLM agents)

```python
from lazycrawler import WebSearch
from lazycrawler.config import SearchConfig

search = WebSearch(
    search_cfg=SearchConfig(
        engine="tavily",
        n_results=5,
        tavily_api_key="YOUR_KEY",
        tavily_search_depth="advanced",
    )
)
result = search.run("LLM agent frameworks comparison", mode="pure")
search.close()

for r in result["results"]:
    print(f"{r.url}: {r.title}")
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
