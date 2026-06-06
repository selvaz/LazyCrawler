# LazyCrawler roadmap

Derived from the evaluation in [docs/COMPARISON.md](docs/COMPARISON.md). Ordered
by impact.

## Done (v0.2)

- [x] **1. JavaScript rendering** — optional Playwright fetch backend
      (`HTTPConfig(render_js=True)`). Browser/context reuse per HTTP client;
      thread-local renderer ownership in parallel mode. Graceful fallback to
      `requests` if Playwright is absent.
- [x] **2. Pluggable output schema** — pass any Pydantic model as
      `crawl(..., schema=MyModel)` (smart content). Falls back to the built-in
      `PageExtract`. The full structured object is available on `PageResult.data`.
- [x] **3. Native parallel mode** — `CrawlerConfig(max_workers=N)`. Bounded
      thread-pool, level-by-level BFS, thread-safe shared state, thread-local
      HTTP/LLM resources, thread-safe DB (`check_same_thread=False` + lock).
      `max_workers=1` keeps the original sequential DFS.

## Done (v0.5)

- [x] **Test suite + CI** — idiomatic `pytest` (fixtures, `monkeypatch`, markers)
      replacing the diagnostic scripts; `.github/workflows/ci.yml` runs lint
      (ruff), tests on 3.10/3.11/3.12, and a build + `twine check`.
- [x] **Per-host rate limiting** — `HostRateLimiter` enforces
      `HTTPConfig.per_host_delay` in **both** sequential and parallel mode, and
      honors robots.txt **`Crawl-delay`** on top of it.
- [x] **Single PDF download** — `HTTPClient.fetch` detects PDFs (Content-Type /
      extension / magic bytes) and returns the bytes once; the PDF pipeline
      extracts from those bytes (no second download).
- [x] **Configurable, less-aggressive link exclusion** —
      `CrawlerConfig.exclude_patterns`; the default no longer drops `/about`,
      `/contact`, `/tag/`, `/category/`, `/author/`.
- [x] **Dedicated User-Agent** — `LazyCrawler/<version>` instead of a spoofed
      browser string.
- [x] **Configurable short-page threshold** — `HTTPConfig.min_text_chars`
      (default 50) instead of a hardcoded 200.
- [x] **Cache recursion** — candidate links are stored per page;
      `CrawlerConfig(recurse_from_cache=True)` keeps following them from a warm
      cache (same frontier cold vs warm, no re-fetch).
- [x] **Thread-safe tools + richer returns** — `web_crawl` no longer mutates
      shared config (per-call `max_depth` override); tool results carry
      `session_id` / `source_url` / `from_cache` / `depth`; new
      `get_session_pages` tool.
- [x] **DuckDuckGo params** — `region` / `timelimit` / `safesearch` / `backend`
      on `SearchConfig`. **Gemini** results are flagged synthetic (no verifiable
      source URLs) rather than presented as navigable pages.
- [x] **Packaging** — physical `LICENSE`, `dev` extra, ruff/pytest config,
      version 0.5.0; the Spyder `setup_paths` bootstrap moved to
      `examples/spyder_setup.py` (out of the package and tests).

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

- [ ] **4. Politeness (rest)** — autothrottle and optional proxy rotation.
      (Per-host rate limiting and robots `Crawl-delay` shipped in v0.5.)
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
- **Politeness scope**: the crawler is intended for authorized crawling. As of
  v0.5 it sends a dedicated `LazyCrawler/<version>` User-Agent and honors
  `link_delay`, per-host `per_host_delay`, and robots.txt `Crawl-delay`;
  autothrottle and proxy rotation are still future work (item 4).
