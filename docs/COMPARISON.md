# LazyCrawler vs the state of the art (2026)

An honest evaluation of LazyCrawler against the main web-crawling frameworks.

## The field

- **[crawl4ai](https://github.com/unclecode/crawl4ai)** — the open-source "LLM-friendly"
  standard. Playwright/CDP, async, clean markdown, BFS/DFS/best-first deep crawl
  with `url_scorer`, `CacheMode`, `MemoryAdaptiveDispatcher`, 3-tier anti-bot.
- **[Firecrawl](https://www.firecrawl.dev/)** — API-first (+ self-host). Markdown by
  default (~80% fewer tokens than HTML), JS rendering, `/scrape /crawl /map /search`,
  interactive actions, batch/scheduled, MCP server.
- **[ScrapeGraphAI](https://scrapegraphai.com/)** — graph-of-operations pipelines,
  arbitrary Pydantic schema via natural-language prompt, LLM-provider agnostic.
- **[Scrapy](https://docs.scrapy.org/)** — the veteran (2008). HTTP-only, async
  (Twisted), `RFPDupeFilter`, HTTP cache, robots.txt. No JS, no LLM.
- **[LangChain WebBaseLoader](https://python.langchain.com/docs/integrations/document_loaders/web_base)**
  — minimal loader; grabs nav/footer noise, no JS.

## Comparison

| Dimension | LazyCrawler | crawl4ai | Firecrawl | ScrapeGraphAI | Scrapy |
|---|---|---|---|---|---|
| JS rendering | ⚠️ optional (Playwright) | ✅ | ✅ | ✅ | ❌ |
| Async / parallel | ✅ thread pool | ✅ dispatcher | ✅ | ~ | ✅ Twisted |
| No-LLM mode (zero cost) | ✅ `pure` | ~ | ~ | ❌ | ✅ |
| LLM content extraction | ✅ | ✅ | ✅ | ✅ | ❌ |
| LLM *only* for link selection | ✅ separate knob | ~ (scorer) | ❌ | ❌ | ❌ |
| Pluggable output schema | ✅ | ✅ | ✅ | ✅ | n/a |
| Output | Pydantic / text / **markdown** | rich markdown | rich markdown | free schema | raw HTML |
| Content dedup + TTL cache | ✅ 3-level | ~ per-URL | ~ | ❌ | ~ URL+HTTP |
| Persistence + provenance | ✅ relational DB | ❌ | ❌ (API) | ❌ | ~ feed export |
| Native PDF | ✅ | ~ | ~ | ❌ | ❌ |
| robots.txt | ✅ default-on | ~ | ✅ | ~ | ✅ |
| Anti-bot / proxy | ❌ | ✅ | ✅ | ~ | ~ |
| SSRF guard | ✅ (agent path) | ❌ | n/a (hosted) | ❌ | ~ |
| Provider-agnostic LLM | ✅ LazyBridge | ✅ | ~ | ✅ | — |
| Maturity / community | v0.6, solo | high | high | medium | very high |

## Where LazyCrawler is genuinely strong

1. **Two independent LLM knobs (content vs links)** — use the LLM only for link
   selection, or only for content. Fine-grained cost control rarely seen elsewhere.
2. **Token economy by design** — 3-level dedup (URL+TTL → `content_hash` skips the
   LLM when content is unchanged → pure→smart enrich without re-fetch) and a
   content-mode-aware cache. crawl4ai's `CacheMode` caches the HTTP response, not
   the LLM tokens.
3. **Persistence with provenance** (`sessions`+`pages`+`crawl_edges`+FTS5) — the big
   tools are stateless fetchers; you bolt storage on yourself. A built-in relational
   model fits a "monitor over time" use case.
4. **Native PDF** with a real fallback chain — financial reports are PDFs.
5. **Provider-agnostic inside your own ecosystem** (LazyBridge) — composable with
   your agents.

## Real gaps (and status)

1. **JavaScript rendering** — was the #1 gap. *Now addressed* via an optional
   Playwright backend (`HTTPConfig(render_js=True)`).
2. **Sequential / no concurrency** — *now addressed* via a native thread-pool
   parallel mode (`CrawlerConfig(max_workers=N)`).
3. **Fixed extraction schema** — *now addressed* via a pluggable `schema=` (any
   Pydantic model).
4. **robots.txt** — now honored by default (`respect_robots`). Anti-bot, proxy
   rotation, and per-domain rate limiting are still missing.
5. **Markdown output** — *now addressed* via optional `emit_markdown`
   (`html_to_markdown`, markdownify-backed); plain text / Pydantic still available.
6. **Weak link frontier** — pure mode is "first N"; no URL scoring / best-first /
   sitemap seeding.
7. **No interactive actions** (click/scroll/form); immature (v0.1).

## Verdict

As a *general* crawler it loses to crawl4ai/Firecrawl on robustness, scale and
output flexibility — but that is not its purpose. Judged by its goal (a
cost-controlled, persistence-first crawler for an agent ecosystem doing financial
research + portfolio monitoring), its design choices are well aligned: token
economy, provenance DB, PDF, provider-agnostic LLM, granular LLM knobs are exactly
what those tools do *not* emphasize.

See [ROADMAP.md](../ROADMAP.md) for what is implemented and what remains, and
[DEEP_ASSESSMENT.md](DEEP_ASSESSMENT.md) for a full code/architecture review.
