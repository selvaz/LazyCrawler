# LazyCrawler roadmap

Derived from the evaluation in [docs/COMPARISON.md](docs/COMPARISON.md). Ordered
by impact.

## Done (v0.2)

- [x] **1. JavaScript rendering** — optional Playwright fetch backend
      (`HTTPConfig(render_js=True)`). Thread-local browser in parallel mode.
      Graceful fallback to `requests` if Playwright is absent.
- [x] **2. Pluggable output schema** — pass any Pydantic model as
      `crawl(..., schema=MyModel)` (smart content). Falls back to the built-in
      `PageExtract`. The full structured object is available on `PageResult.data`.
- [x] **3. Native parallel mode** — `CrawlerConfig(max_workers=N)`. Bounded
      thread-pool, level-by-level BFS, thread-safe shared state, thread-local
      HTTP/LLM resources, thread-safe DB (`check_same_thread=False` + lock).
      `max_workers=1` keeps the original sequential DFS.

## Done (v0.4)

- [x] **LazyBridge tool layer** — `lazycrawler.tools.CrawlerTools` (a
      `ToolProvider`): `as_tools()` returns `web_search` / `web_crawl` /
      `search_cached` / `get_page` for `Agent(tools=...)`. Rich LLM-facing
      docstrings, compact JSON returns, cache-first to save tokens. LazyCrawler
      is the *tool*, not an agent. LazyBridge imported lazily.
- [x] **sentiment + notes** in the smart structured output (`PageExtract`,
      `PageResult`, DB): `sentiment` ∈ {negative, neutral, positive}; `notes`
      reserved for research tags/annotations (empty by default).

## Done (v0.3)

- [x] **robots.txt** honored by default (`CrawlerConfig.respect_robots`, on by
      default, disableable); blocked URLs reported as `status="robots_blocked"`.
- [x] **No swallowed exceptions** — everything routed through the `lazycrawler`
      logger (`set_log_level`); `CrawlerConfig.strict` for fail-fast.
- [x] PDF downloads honor `verify_ssl` / `ca_bundle` (was the §4.1 bug).

## Next

- [ ] **4. Politeness (rest)** — autothrottle, optional proxy rotation,
      per-host concurrency / crawl-delay honoring.
- [ ] **5. Markdown output** — an optional markdown renderer (heading hierarchy,
      tables, link citations) for RAG ingestion.
- [ ] **6. Smarter link frontier** — URL scoring / best-first traversal,
      `sitemap.xml` seeding, global priority frontier (ideas from crawl4ai).

## Later

- [ ] Interactive actions (click / scroll / form-fill) before extraction.
- [ ] WebSearch `gemini` seed-URL crawling once LazyBridge surfaces grounding
      sources on the `Envelope`.
- [ ] Migrate into `lazytools.connectors.web` when production-ready.

## Acknowledged trade-offs (from ANALYSIS.md)

- **Content-alias rows**: level-2 dedup stores identical content reached via
  different URLs once per URL (a `pages` row each, same `content_hash`) to keep
  per-URL provenance. This trades storage for provenance; a dedup-by-reference
  option could be added for storage-sensitive deployments.
- **`same_domain_only`** compares full netloc (incl. port), so
  `example.com:8080` and `example.com` are treated as different hosts.
- **No robots.txt** (see item 4): the crawler is intended for authorized
  crawling only; it sends a browser-like User-Agent and honors only a global
  `link_delay` (no per-domain rate limiting).
