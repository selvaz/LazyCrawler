# LazyCrawler Codebase Analysis

> Review of LazyCrawler v0.14.0. Scope: package structure, tests, LazyBridge tool
> integration, and production-readiness findings — including fixes applied during
> this audit session.

## 1. What it is

LazyCrawler is a generic web crawler and web search package for the LazyBridge
ecosystem. Content extraction and link-following are controlled by two independent
knobs:

| Knob | `pure` | `ml` (zero-token) | `smart` |
|------|--------|-------------------|---------|
| **content** | trafilatura/regex clean text | TextRank + YAKE topics + spaCy entities + VADER | LazyBridge structured extraction |
| **links** | heuristic (first N) | best-first semantic scoring (Model2Vec) | LazyBridge relevance ranking |

`mode=` sets both knobs; `content=` / `links=` can override either independently.
Pure mode remains LLM-free. ML mode uses only local models (zero tokens).

## 2. Architecture

```text
config.py        Dataclass configs for Crawler, HTTP, LLM, Search, DB, ML
models.py        PageResult (public output type) + Artifact
_log.py          Logging setup, set_log_level helper
http.py          HTTP client, retry/backoff, URL normalization, SSRF guard, robots.txt
ratelimit.py     HostRateLimiter — per-host polite delay (sequential + parallel)
text.py          HTML text/link/title/date/canonical extraction
pdf.py           Remote PDF extraction (PyMuPDF → pypdf → pdfplumber)
prompts.py       Domain-agnostic prompts for smart mode
llm.py           LazyBridge wrapper and structured output models
ml.py            MLEngine: semantic link scoring (Model2Vec) + NLP extraction
markdown.py      Optional HTML→Markdown renderer (RAG ingestion)
artifacts.py     Tables/images/charts/SVG extraction (Artifact model)
db.py            SQLite: sessions/pages/crawl_edges/artifacts, TTL cache, FTS5
_pipeline.py     Per-page pipeline (fetch → extract → artifact → enrich → emit)
crawler.py       WebCrawler (pure + ml + smart, sequential + parallel BFS)
async_crawler.py AsyncWebCrawler (aiohttp, pure + ml, high-throughput; reuses _pipeline)
search.py        WebSearch seeded by DuckDuckGo, Brave, Tavily, or Gemini
presets.py       Named preset catalog (CrawlPreset, DEFAULT_PRESETS)
tools.py         LazyBridge ToolProvider (CrawlerTools)
```

The persistence model has a global `pages` cache keyed by URL hash and
session-specific `crawl_edges` for provenance. Dedup works in three layers:
fresh URL cache, content hash reuse, and pure-to-smart enrichment without
re-fetching.

## 3. Verified

- `compileall` over `lazycrawler` and `tests`: passed.
- `ruff check .`: all checks passed (F401/I001 fixed in this audit session).
- `ruff format --check .`: all files comply (fixed in this audit session).
- `tests/decoupled_test.py`: all pass.
- `tests/robots_test.py`: all pass.
- `tests/parallel_test.py`: all pass.
- `tests/tools_test.py` without LazyBridge: all pass (`as_tools()` skipped).
- `tests/tools_test.py` with local LazyBridge/LazyTools on `PYTHONPATH`: all pass.

## 4. Strengths

- **Three-mode design**: `pure` (no deps), `ml` (local ML, zero tokens), `smart`
  (LLM). The ml tier adds structured extraction and semantic best-first traversal
  with no API cost; hybrid patterns (`links="ml"`, `content="smart"`) maximize
  coverage at minimum token spend.
- Clean module boundaries; optional dependencies are imported lazily so missing
  extras degrade gracefully instead of erroring at import time.
- `_pipeline.py` extracts the per-page fetch/extract/enrich/emit logic from
  `crawler.py`, keeping the orchestrators thin. `async_crawler.py` reuses the
  same pipeline unchanged.
- `AsyncWebCrawler` (`async_crawler.py`) provides aiohttp-based high-throughput
  crawling with the SSRF guard enabled by default.
- Robots.txt honored by default; disallowed URLs are emitted as
  `status="robots_blocked"` (never silently dropped).
- Parallel mode uses a bounded BFS worker pool with thread-local HTTP/LLM/browser
  resources and serialized DB writes.
- Named presets (`presets.py`) expose 11 built-in intent-level configs that LLM
  agents select by name — covering quick lookup, deep research, news scan, RAG
  ingest, zero-token ml variants, and hybrid patterns.
- Five tools exposed to the agent: `list_presets`, `search_cached`, `web_search`,
  `web_crawl`, `get_page`.
- SSRF guard validates every redirect hop; enabled by default in `CrawlerTools`
  and `AsyncWebCrawler`.

## 5. Audit Findings (2026-06 session)

### 5.1 Import cleanup (F401 / I001) — fixed

`ruff check` reported unused imports (`F401`) and unsorted import blocks (`I001`)
in `_pipeline.py`, `async_crawler.py`, and `crawler.py`. All were removed and/or
sorted. CI `ruff check .` now passes cleanly.

### 5.2 Code formatting — fixed

`ruff format --check` flagged formatting divergences in the same three files:
multi-argument constructor calls not expanded to one-arg-per-line, boolean chains
not wrapped, and missing blank lines after `try/except` import blocks. All three
files were reformatted with `ruff format` and pushed. CI `ruff format --check .`
now passes cleanly.

### 5.3 SSRF guard — documentation improved

The guard in `http.py` blocks loopback, link-local, RFC-1918, cloud metadata
(`169.254.169.254`), `localhost`, and `*.local`. Redirect chains are followed
manually and every hop is re-validated. The README was updated with a prominent
warning: enabled by default in `CrawlerTools` and `AsyncWebCrawler`; off by
default for direct `WebCrawler` use. The guard also covers canonical-URL
poisoning and refuses `render_js` when enabled (browser subresources bypass
per-hop checks).

### 5.4 PDF fetch path (pre-existing, tracked)

PDF downloads use `urllib` rather than the shared `HTTPClient`, bypassing
per-session retry/backoff, proxy, and SSL configuration. Tracked in ROADMAP.md.

### 5.5 Crawl politeness (pre-existing, tracked)

Per-host rate limiting (`ratelimit.py`) is in place. Broader controls (proxy
rotation, autothrottle, per-domain concurrency limits) remain on the roadmap.

## 6. Bottom Line

LazyCrawler v0.14.0 is a well-structured pre-production crawler/search library
with a solid LazyBridge integration and a credible zero-token ml tier. The CI
pipeline is now clean (lint + format, fixed in this session). The highest-value
remaining work is migrating the PDF fetch path to the shared HTTP client and
adding deeper crawl politeness controls.
