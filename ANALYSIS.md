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

## 5. Audit Findings (2026-06 deep-read session)

This round was a full re-read of all 21 source modules (~8.6k LOC) looking for
correctness/concurrency bugs rather than lint/format issues (those are clean and
stay clean). Two genuine defects were found and fixed, both with regression
coverage; the remaining observations are low-severity or pre-existing/tracked.

### 5.1 Redirect-target pages emitted twice — **fixed**

**Severity: low (correctness).** When two distinct source URLs redirect (or
normalize) to the **same** final URL, the post-redirect *adoption* step in
`_pipeline.process_fetched` re-keyed the page to the final URL but ignored the
`_mark_visited` result — so the target was processed, emitted, and counted toward
`max_pages` **once per source URL**. The parallel sibling path (canonical-URL
adoption, a few lines below) already guarded against this by checking the
`_mark_visited` return; the redirect path did not.

Reproduced: two seeds both 30x→`https://target.example/final` yielded **two**
`done` rows for the one page (and double-counted the cap). The fix mirrors the
canonical guard: if the adopted final URL was already visited, bail without
re-emitting. Affects both the sync `WebCrawler` and the `AsyncWebCrawler` (both
route through the shared pipeline). Regression test:
`test_redirect_to_shared_target_is_not_emitted_twice`.

### 5.2 Async PDF detection missed query-string PDFs — **fixed**

**Severity: low (parity/efficiency).** The async client (`_AsyncHTTPClient._fetch_once`)
decided "is this a PDF?" with `current.lower().endswith(".pdf")`, while the sync
client strips the query first (`url.split("?")[0]`). A PDF served at
`/doc.pdf?token=…` (common for signed/CDN links) without an `application/pdf`
content-type was therefore mis-capped at `max_html_bytes`, decoded as HTML, and
then re-detected downstream by `looks_like_pdf`, forcing the pipeline's fallback
`extract_pdf` **urllib re-download** (see 5.4). Aligned the async check to strip
the query, so the bytes are grabbed once over aiohttp — which also closes that
urllib re-download path on the async engine.

### 5.3 SSRF guard — best-effort, as documented (no change)

The guard in `http.py` blocks loopback / link-local / RFC-1918 / reserved /
multicast / unspecified IPs, `localhost`, `*.local`, and cloud-metadata hosts,
re-validating **every redirect hop** and unwrapping IPv4-mapped IPv6. It is a
check-time guard and remains vulnerable to DNS-rebinding/TOCTOU (the docstring
and README say so, and recommend OS-level egress control for untrusted targets).
On by default in `CrawlerTools` and `AsyncWebCrawler`; mutually exclusive with
`render_js` (browser subresources bypass it). No defect found this round.

### 5.4 PDF fallback path uses `urllib` (pre-existing, tracked)

The fallback `extract_pdf` downloads via `urllib`, bypassing the shared
`HTTPClient` (retry/backoff, proxy, SSRF). It is byte-capped, and after 5.2 it is
effectively reachable only via the `render_js` path — where the SSRF guard is
already refused — so the residual exposure is minimal. Migrating it onto the
shared client remains the cleanest close; tracked in ROADMAP.md.

### 5.5 Lower-severity observations (no change)

- **Cache-enrich does not recurse.** In `_try_cache`, a pure→smart/ml enrichment
  of a cached page returns no frontier links even when `recurse_from_cache=True`,
  so a warm cache can prune traversal that a cold crawl would have followed. A
  deliberate-looking trade-off, but worth a doc note or alignment.
- **Robots/`get_base_domain` keys still carry the port** (`netloc`), the long-
  standing low item; `same_host_only` mitigates the scope side.
- **Politeness** stays at per-host rate limit + robots `Crawl-delay`; no
  autothrottle/proxy (roadmap).

### 5.6 Verification

`pytest -m "not integration"` → **193 passed, 5 skipped** (was 192; +1 regression
test). `ruff check .` and `ruff format --check .` clean. `python -m build` +
`twine check` unaffected.

## 6. Bottom Line

LazyCrawler v0.14.0 is a well-structured pre-production crawler/search library
with a solid LazyBridge integration and a credible zero-token ml tier. The CI
pipeline is now clean (lint + format, fixed in this session). The highest-value
remaining work is migrating the PDF fetch path to the shared HTTP client and
adding deeper crawl politeness controls.
