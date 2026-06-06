# LazyCrawler Codebase Analysis

> Current review of LazyCrawler v0.4.0. Scope: package structure, tests,
> LazyBridge tool integration, and the main production-readiness risks.

## 1. What it is

LazyCrawler is a generic web crawler and web search package for the LazyBridge
ecosystem. It supports two independent knobs:

| Knob | `pure` | `smart` |
|------|--------|---------|
| content | trafilatura/regex clean text | LazyBridge structured extraction |
| links | heuristic filtered links | LazyBridge relevance ranking |

`mode=` sets both knobs; `content=` and `links=` can override each knob
independently. Pure mode remains LLM-free.

## 2. Architecture

```text
config.py    Dataclass configs for crawler, HTTP, LLM, search, DB
http.py      HTTP client, retry/backoff, URL normalization, robots.txt
text.py      HTML text/link/title/date/canonical extraction
pdf.py       Remote PDF extraction with optional parser stack
prompts.py   Domain-agnostic prompts for smart mode
llm.py       LazyBridge wrapper and structured output models
db.py        SQLite sessions/pages/crawl_edges, TTL cache, FTS5
crawler.py   WebCrawler orchestration, cache/dedup, parallel mode
search.py    WebSearch, seeded by DuckDuckGo or Gemini grounding
tools.py     LazyBridge ToolProvider wrapper
```

The persistence model has a global `pages` cache keyed by URL hash and
session-specific `crawl_edges` for provenance. Dedup works in three layers:
fresh URL cache, content hash reuse, and pure-to-smart enrichment without
refetching.

## 3. Verified

- `compileall` over `lazycrawler` and `tests`: passed.
- `tests/decoupled_test.py`: 12 pass, 0 fail.
- `tests/robots_test.py`: 9 pass, 0 fail.
- `tests/parallel_test.py`: 8 pass, 0 fail.
- `tests/tools_test.py` without LazyBridge: 13 pass, `as_tools()` skipped.
- `tests/tools_test.py` with local LazyBridge/LazyTools on `PYTHONPATH`: 15 pass,
  0 fail.

## 4. Strengths

- Clean module boundaries and lazy imports for optional dependencies.
- Pure mode does not require LazyBridge.
- Robots.txt is honored by default and skipped URLs are emitted with
  `status="robots_blocked"`.
- Parallel mode uses a bounded worker pool with thread-local HTTP/LLM/browser
  resources and serialized DB access.
- The LazyBridge tool layer exposes the expected four tools:
  `web_search`, `web_crawl`, `get_page`, and `search_cached`.

## 5. Current Findings

### 5.1 WebSearch config ownership

Fixed in this pass: `WebSearch` now copies the caller-provided `CrawlerConfig`
before applying `SearchConfig.crawl_depth` and `same_domain_only`, so callers do
not see their config mutated.

### 5.2 Tool `max_results`

Fixed in this pass: `CrawlerTools.web_search(max_results=...)` now clamps and
passes the requested result count through to `WebSearch.run()`.

### 5.3 PDF fetch path

PDF downloads honor `verify_ssl` / `ca_bundle`, but they still use `urllib`
rather than the shared `HTTPClient`. This is workable, but it means PDFs do not
share the same `requests.Session`, retry/backoff behavior, or proxy/session
configuration as HTML fetches.

### 5.4 Browser reuse

Fixed in this pass: `HTTPClient` now owns a reusable `BrowserRenderer` when
`HTTPConfig(render_js=True)`. Sequential crawls reuse one Playwright
browser/context, while parallel crawls reuse one renderer per worker-owned
HTTP client.

### 5.5 Politeness beyond robots.txt

The remaining production concern is crawl politeness: there is a global
`link_delay`, but no per-host concurrency, crawl-delay parsing, autothrottle, or
proxy rotation yet. This is already tracked in `ROADMAP.md`.

### 5.6 Storage trade-off

Content-hash dedup stores alias rows per URL to preserve provenance. This is a
reasonable default, but a dedup-by-reference option may be useful for
storage-sensitive deployments.

## 6. Bottom Line

LazyCrawler v0.4.0 is a coherent pre-production crawler/search library with a
solid LazyBridge integration. The highest-value next work is improving PDF fetch
consistency and adding deeper crawl politeness controls.
