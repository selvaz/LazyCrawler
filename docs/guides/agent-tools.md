# Agent Integration (LazyBridge)

`CrawlerTools` wraps `WebCrawler` and `WebSearch` as LazyBridge tools, making the crawler available to AI agents. The agent decides when to search, when to crawl, and when to use cached results.

---

## The pattern

```
Agent (LLM)
    │
    ├─ list_presets()             ← discover named intents (quick_lookup, deep_research, …)
    │
    ├─ search_cached("query")     ← check cache first (free, instant)
    │
    ├─ web_search("query", preset=…)   ← search the web (if cache miss)
    │
    ├─ web_crawl("url", preset=…)      ← crawl a page (and its links)
    │
    ├─ get_page("url")            ← fetch a single page's full text
    │
    ├─ get_artifacts("url")       ← tables/images/charts extracted from a page
    │
    └─ get_session_pages("sid")   ← list pages from a previous search/crawl run
```

`web_search` / `web_crawl` accept a `preset=` selecting a ready-made
configuration by intent — see the [Presets guide](presets.md). They return a
`session_id`; pass it to `get_session_pages` to list everything reached in that
run. Per-page results also carry `source_url`, `from_cache`, and `depth`.

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
```

!!! tip "No `close()` in the agent path"
    You don't call `close()` here. Each `web_search` / `web_crawl` releases its
    HTTP sockets at the end of the call, and `HTTPClient` / `CrawlerDB` free any
    remainder on GC / interpreter exit. Lifecycle methods are not exposed as
    tools, so the LLM can never call them. See
    [Resource cleanup](#resource-cleanup). `close()` / `with` remain available
    for deterministic teardown.

---

## Presets (pick a config by intent)

Instead of exposing raw knobs, let the agent select a **named preset**. It calls
`list_presets()` to discover them, then passes `preset="…"` to `web_search` /
`web_crawl`:

```python
tools.web_search("EU AI Act enforcement 2026", preset="news_scan")
tools.web_crawl("https://example.com/report", preset="extract_data")
```

| Preset | Intent | Cost |
|--------|--------|------|
| `quick_lookup` | fast factual check, single page, no LLM | minimal |
| `deep_research` | smart extraction + LLM link-following, depth 1 | high |
| `news_scan` | recent news, sentiment + date, last week | medium |
| `extract_data` | tables/images as artifacts | low |
| `rag_ingest` | Markdown + artifact anchors for RAG | low |

Extend or override the catalog with `CrawlerTools(presets={...})`. Full details
in the [Presets guide](presets.md).

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

# No close() needed — HTTP is released per tool call; the DB frees on GC/exit.
# (db.close() remains available if you want a deterministic WAL checkpoint.)
```

---

## Resource cleanup

Cleanup is automatic and you never call `close()` in the agent path:

- **Per tool call** — each `web_search` / `web_crawl` releases its HTTP sockets
  (and browser) when the call returns; the call is a self-contained transaction.
  The shared **DB cache stays open**, and the HTTP session is rebuilt lazily on
  the next call. Release is reference-counted, so it never closes a session a
  concurrent call is still using.
- **Backstop** — `HTTPClient` and `CrawlerDB` arm a `weakref.finalize`, so
  anything left is freed on garbage-collection or at interpreter exit.
- **Not a tool** — `close()` is not exposed via `as_tools()`, so the LLM can only
  call `web_search` / `web_crawl` / `get_page` / ….

`close()` / `with` stay available for deterministic teardown (idempotent — a
second `close()` is a safe no-op).

---

## Tool output format

All tools return JSON **strings**. To keep token cost down (and to keep
retrieved web content clearly marked as untrusted data), the page objects carry
a truncated `snippet`, not full text — call `get_page(url)` for the full body.

### web_search / web_crawl

An envelope object (not a bare array) with a `pages` list:

```json
{
  "query": "solid state battery",
  "found": 3,
  "session_id": "a1b2c3d4",
  "pages": [
    {
      "url": "https://example.com/article",
      "title": "Article Title",
      "snippet": "Brief snippet of retrieved content … (truncated)",
      "content_is_untrusted": true,
      "sentiment": "neutral",
      "published": "2024-05-01",
      "status": "done",
      "source_url": "https://example.com/",
      "from_cache": false,
      "depth": 0,
      "full_text_available": true
    }
  ]
}
```

`web_crawl` returns the same shape with `"url"` in place of `"query"`.
`full_text_available: true` means the snippet was truncated and the complete
text can be fetched with `get_page`.

### get_page

```json
{
  "url": "https://example.com/page",
  "title": "Page Title",
  "untrusted_page_text": "Full extracted text...",
  "found": true
}
```

The full body is returned under `untrusted_page_text` — it is retrieved web
content and must be treated as data, never as instructions.

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
[LazyCrawler] search query='solid state battery breakthrough 2024' preset=- max_results=8 ...
[LazyCrawler] crawl url='https://nature.com/articles/solid-state-batteries' preset=- depth=1 ...
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

The tools still work — the `snippet` is plain extracted text (no summary,
entities, topics or sentiment, which require ml/smart mode).

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

- Prefer **presets** over raw knobs — let the model call `list_presets()` and pick an intent (`quick_lookup` for a fact, `deep_research` for depth); they bundle cost-appropriate defaults
- Keep `depth` small (0 or 1) when you pass it explicitly — deep crawls inside an agent loop consume many tokens
- Use `search_cached` before `web_search` — it's instant and free
- Use a `CrawlerDB` with a long TTL (`ttl_hours=72` or more) — agents often ask similar questions
- Set `topic=` in `CrawlerTools` to improve link selection relevance across all tool calls
- Don't call `close()` — cleanup is automatic (per-call HTTP release + GC/exit backstop)
