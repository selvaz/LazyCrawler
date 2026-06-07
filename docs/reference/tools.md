# CrawlerTools (Agent Integration)

`CrawlerTools` exposes `WebCrawler` and `WebSearch` capabilities as **LazyBridge tools**, making them available to AI agents.

```python
from lazycrawler.tools import CrawlerTools
```

---

## Constructor

```python
CrawlerTools(
    db: CrawlerDB | None = None,
    llm_cfg: LLMConfig | None = None,
    crawler_cfg: CrawlerConfig | None = None,
    http_cfg: HTTPConfig | None = None,
    content: Literal["pure", "smart"] = "smart",
    links: Literal["pure", "smart"] = "pure",
    topic: str = "",
    verbose: bool = False,
)
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `db` | `CrawlerDB \| None` | `None` | Persistent cache. Strongly recommended — agents call tools repeatedly |
| `llm_cfg` | `LLMConfig \| None` | `None` | LLM config for smart-mode extraction |
| `crawler_cfg` | `CrawlerConfig \| None` | `None` | Crawler limits (depth, pages, etc.) |
| `http_cfg` | `HTTPConfig \| None` | `None` | HTTP settings |
| `content` | `"pure"` or `"smart"` | `"smart"` | Content extraction mode for `web_crawl` and `get_page` |
| `links` | `"pure"` or `"smart"` | `"pure"` | Link selection mode for `web_crawl` |
| `topic` | `str` | `""` | Topic passed to the crawler for context |
| `verbose` | `bool` | `False` | Print tool call details to stdout |

---

## as_tools()

```python
def as_tools() -> list
```

Returns a list of LazyBridge `Tool` objects that can be passed to an `Agent`:

```python
tools = CrawlerTools(db=db).as_tools()
agent = Agent(engine=engine, tools=tools)
```

---

## Tool descriptions (as the LLM sees them)

### list_presets

```
list_presets() -> str
```

List the named presets the agent can pass as `preset=` to `web_search` / `web_crawl`.
Each preset is an *intent* (e.g. `quick_lookup`, `deep_research`, `news_scan`,
`extract_data`, `rag_ingest`) bundling a ready-made configuration — content mode,
link-following, depth, artifact extraction, Markdown output, search recency — plus
a coarse `cost` hint. Returns JSON: `{"presets": [{name, intent, cost, content,
follows_links, link_mode, depth, artifacts, markdown, recency}]}`.

The catalog can be extended/overridden per `CrawlerTools(presets={...})` with
`CrawlPreset` objects (a key matching a built-in name overrides it).

### web_search

```
web_search(query: str, max_results: int | None = None, preset: str = "") -> str
```

Search the web and return a JSON list of page results for the top matches.
`preset` selects a named configuration (see `list_presets`); omit it for the
default behavior. `max_results` defaults to the preset's value (or 15).

Returns JSON: `{"query", "found", "session_id", "pages": [...]}`

### web_crawl

```
web_crawl(url: str, depth: int | None = None, preset: str = "") -> str
```

Crawl a URL and its linked pages up to `depth` levels. Returns a JSON list of
page results. `preset` selects a named configuration; an explicit `depth` still
overrides the preset's depth.

!!! tip
    Keep `depth` small (0 or 1) in agent context — deep crawls are slow and consume many tokens.

### search_cached

```
search_cached(query: str, limit: int = 10) -> str
```

Full-text search against the local DB cache. Returns JSON list of matching pages. Fast — no HTTP request.

**Cache-first strategy**: always call `search_cached` first. Only call `web_search` if the cache returns no useful results.

### get_page

```
get_page(url: str) -> str
```

Fetch and extract a single URL. Returns JSON with `url`, `title`, `text`, `summary`, and metadata.

---

## close()

```python
def close() -> None
```

Release resources (HTTP client, DB connection).

---

## Examples

### Basic agent with web tools

```python
from lazybridge import Agent, LLMEngine
from lazycrawler.tools import CrawlerTools
from lazycrawler import CrawlerDB
from lazycrawler.config import DBConfig, LLMConfig

db = CrawlerDB(DBConfig(db_path="agent.db"))
tools_provider = CrawlerTools(
    db=db,
    llm_cfg=LLMConfig(model="gpt-4o-mini"),
    content="smart",
    links="pure",
)

engine = LLMEngine("gpt-4o-mini", system="You are a research assistant.")
agent = Agent(engine=engine, tools=tools_provider.as_tools())

response = agent("What are the latest developments in quantum computing?")
print(response.text())

tools_provider.close()
db.close()
```

### Full research agent

```python
from lazybridge import Agent, LLMEngine
from lazycrawler.tools import CrawlerTools
from lazycrawler import CrawlerDB
from lazycrawler.config import DBConfig, LLMConfig, CrawlerConfig

TOPIC = "AI safety and alignment research"

db = CrawlerDB(DBConfig(db_path="research.db", ttl_hours=48.0))

crawler_tools = CrawlerTools(
    db=db,
    llm_cfg=LLMConfig(model="gpt-4o-mini"),
    crawler_cfg=CrawlerConfig(max_depth=1, max_pages=5),
    content="smart",
    links="pure",
    topic=TOPIC,
    verbose=True,
)

system = f"""You are a research assistant specialized in {TOPIC}.

Strategy:
1. First call search_cached to check if relevant pages are already in the cache
2. If cache is empty or insufficient, call web_search
3. For promising pages, call get_page to get full content
4. Synthesize findings into a structured report

Always cite your sources (URLs)."""

engine = LLMEngine("gpt-4o-mini", system=system)
agent = Agent(engine=engine, tools=crawler_tools.as_tools())

result = agent(f"Research: {TOPIC}. Provide a 500-word summary with key findings and sources.")
print(result.text())

crawler_tools.close()
db.close()
```

### Verbose mode (debug tool calls)

```python
tools_provider = CrawlerTools(
    db=db,
    content="pure",
    verbose=True,  # prints each tool call to stdout
)
```

Output:
```
[tool] web_search("python async best practices", max_results=10)
[tool] search_cached("async", limit=5) -> 3 results
[tool] get_page("https://example.com/async-guide")
```

### Pure mode tools (no LLM cost)

```python
# No llm_cfg = pure mode only
tools_provider = CrawlerTools(
    db=db,
    content="pure",
    links="pure",
)
# web_search and web_crawl return text without LLM extraction
# search_cached and get_page still work
```
