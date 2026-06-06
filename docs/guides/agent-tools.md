# Agent Integration (LazyBridge)

`CrawlerTools` wraps `WebCrawler` and `WebSearch` as LazyBridge tools, making the crawler available to AI agents. The agent decides when to search, when to crawl, and when to use cached results.

---

## The pattern

```
Agent (LLM)
    │
    ├─ search_cached("query")     ← check cache first (free, instant)
    │
    ├─ web_search("query")        ← DuckDuckGo search (if cache miss)
    │
    ├─ web_crawl("url", depth=1)  ← crawl a page and its links
    │
    ├─ get_page("url")            ← fetch a single page
    │
    └─ get_session_pages("sid")   ← list pages from a previous search/crawl run
```

`web_search` / `web_crawl` return a `session_id`; pass it to `get_session_pages`
to list everything reached in that run. Per-page results also carry
`source_url`, `from_cache`, and `depth`.

**Cache-first strategy**: always check `search_cached` before `web_search`. This avoids redundant HTTP requests when the agent asks similar questions in the same session.

---

## Setup

```python
from lazybridge import Agent, LLMEngine
from lazycrawler.tools import CrawlerTools
from lazycrawler import CrawlerDB
from lazycrawler.config import DBConfig, LLMConfig, CrawlerConfig

# DB is strongly recommended — agents call tools repeatedly
db = CrawlerDB(DBConfig(db_path="agent.db", ttl_hours=48.0))

crawler_tools = CrawlerTools(
    db=db,
    llm_cfg=LLMConfig(model="gpt-4o-mini"),
    crawler_cfg=CrawlerConfig(max_depth=1, max_pages=5),
    content="smart",   # LLM extraction for better tool output quality
    links="pure",      # heuristic link selection (cheaper)
    topic="",          # set per-agent if you have a specific domain
)

engine = LLMEngine("gpt-4o-mini", system="You are a research assistant.")
agent = Agent(engine=engine, tools=crawler_tools.as_tools())

response = agent("What is LazyCrawler and how does it work?")
print(response.text())

crawler_tools.close()
db.close()
```

---

## Full research agent example

```python
from lazybridge import Agent, LLMEngine
from lazycrawler.tools import CrawlerTools
from lazycrawler import CrawlerDB
from lazycrawler.config import DBConfig, LLMConfig, CrawlerConfig

TOPIC = "renewable energy storage technologies"

db = CrawlerDB(DBConfig(db_path="energy_research.db", ttl_hours=72.0))

crawler_tools = CrawlerTools(
    db=db,
    llm_cfg=LLMConfig(model="gpt-4o-mini"),
    crawler_cfg=CrawlerConfig(max_depth=1, max_pages=5, max_links_per_level=5),
    content="smart",
    links="pure",
    topic=TOPIC,
)

SYSTEM = f"""You are a research analyst specializing in {TOPIC}.

When researching a topic, follow this strategy:
1. Start with search_cached to check what's already in the local knowledge base
2. If the cache has good coverage, use it; if not, use web_search to find new sources
3. For the most relevant pages, use get_page to get full content
4. Synthesize information from multiple sources
5. Always cite your sources with URLs

Be thorough but efficient — avoid fetching pages you've already seen."""

engine = LLMEngine("gpt-4o-mini", system=SYSTEM)
agent = Agent(engine=engine, tools=crawler_tools.as_tools())

# Run the research session
questions = [
    f"What are the main {TOPIC} solutions available today?",
    "What are the key technical challenges and recent breakthroughs?",
    "Which companies or research groups are leading in this field?",
]

for q in questions:
    print(f"\n{'='*60}")
    print(f"Q: {q}")
    print('='*60)
    response = agent(q)
    print(response.text())

crawler_tools.close()
db.close()
```

---

## Tool output format

All tools return JSON strings. The agent sees structured data:

### web_search / web_crawl / web_crawl

```json
[
  {
    "url": "https://example.com/article",
    "title": "Article Title",
    "summary": "Brief summary...",
    "text": "Full extracted text...",
    "entities": ["OpenAI", "Google"],
    "topics": ["AI", "Machine Learning"],
    "sentiment": "neutral",
    "depth": 0,
    "is_pdf": false
  }
]
```

### get_page

```json
{
  "url": "https://example.com/page",
  "title": "Page Title",
  "text": "Full extracted text...",
  "summary": "...",
  "status": "done"
}
```

---

## Verbose mode

```python
crawler_tools = CrawlerTools(
    db=db,
    verbose=True,   # prints each tool call to stdout
)
```

Output during agent run:

```
[tool] search_cached("renewable energy batteries", limit=10) -> 3 results
[tool] web_search("solid state battery breakthrough 2024", max_results=8)
[tool] get_page("https://nature.com/articles/solid-state-batteries")
```

---

## Pure mode tools (no LLM cost)

For data collection where structured extraction is not needed:

```python
crawler_tools = CrawlerTools(
    db=db,
    # no llm_cfg = pure mode
    content="pure",
    links="pure",
)
```

The tools still work — they return `text` instead of structured fields.

---

## Multi-query research

```python
# First session: builds the cache
response1 = agent("Research quantum computing hardware")

# Second session (same db): hits cache for known pages
response2 = agent("What are the latest quantum computing milestones?")
# Agent will call search_cached first, find pages, avoid re-crawling
```

---

## Recommendations

- Keep `depth` small (0 or 1) in `web_crawl` calls — deep crawls inside an agent loop consume many tokens
- Use `search_cached` before `web_search` — it's instant and free
- Use a `CrawlerDB` with a long TTL (`ttl_hours=72` or more) — agents often ask similar questions
- Set `max_pages=5` in `CrawlerConfig` to bound each `web_crawl` call
- Set `topic=` in `CrawlerTools` to improve link selection relevance across all tool calls
