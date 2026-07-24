# LazyCrawler

A **generic** web crawler + search with **three modes** and SQLite persistence,
built for the [LazyBridge](https://github.com/selvaz/LazyBridge) ecosystem.
Works on any kind of web content — not tied to any domain.

There are **two independent knobs** (content / links), each taking one of
**three values** — `pure`, `ml`, or `smart`:

| Knob | `pure` | `ml` (no-LLM, zero tokens) | `smart` |
|------|--------|----------------------------|---------|
| **content** (page text) | trafilatura/regex clean text | TextRank summary + YAKE topics + spaCy entities + VADER sentiment | LLM structured extraction (title, summary, entities, topics, **sentiment**, notes) |
| **links** (which to follow) | heuristic (first N) | best-first **semantic** scoring (Model2Vec) | LLM relevance ranking against the topic |

`mode` is a shortcut that sets both; `content=` / `links=` override either one:

```python
crawl(url, mode="pure")                       # no LLM
crawl(url, mode="ml",    topic="...")         # local ML, no LLM, zero tokens
crawl(url, mode="smart")                       # LLM (content + links)
crawl(url, content="smart", links="ml")        # LLM extraction, semantic frontier for free
crawl(url, content="pure",  links="smart")     # no summary, LLM picks the links
```

**WebSearch is a derivation of WebCrawler**: it seeds itself from a search
engine's results (DuckDuckGo, Brave, Tavily, or Gemini grounded) and then crawls.

> **Status**: standalone library. LazyCrawler's **LLM-tool interface** is now
> surfaced through LazyTools as `lazytools.connectors.web.WebTools` (install via
> `pip install "lazytoolkit[web] @ git+https://github.com/selvaz/LazyTools.git"`) — a thin pass-through over `CrawlerTools`.
> Only the tool surface is re-exposed; the crawler engine stays here, standalone
> and unchanged.

---

## Install

```bash
# Core — enough for PURE mode (no LLM)
pip install -e .

# With every extra (smart, pdf, search, excel, dates)
pip install -e ".[all]"

# Or selectively:
pip install -e ".[smart]"     # LazyBridge (LLM smart mode; Python >=3.11)
pip install -e ".[ml]"        # model2vec + numpy (no-LLM ml mode: scoring + summary)
pip install -e ".[nlp]"       # yake + vaderSentiment + spacy (ml content extraction)
pip install -e ".[pdf]"       # pymupdf, pypdf, pdfplumber
pip install -e ".[search]"    # ddgs (DuckDuckGo)
pip install -e ".[js]"        # playwright (JS rendering)
pip install -e ".[markdown]"  # markdownify (Markdown output)
pip install -e ".[image]"     # pillow (artifact image dimensions)
pip install -e ".[excel]"     # openpyxl (blacklist from .xlsx)
pip install -e ".[dates]"     # python-dateutil (published_iso)
```

**Smart mode** requires LazyBridge on the path and an API key for the chosen
provider (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`,
`DEEPSEEK_API_KEY`). In the ecosystem, `spyder_startup.py` adds LazyBridge to the
path and loads `.env`.

**Async mode** (high-throughput; `pure` **and** `ml`):

```bash
pip install -e ".[async]"   # aiohttp
```

---

## ⚠️ SSRF Guard — read this before crawling localhost/intranet targets

**Since 0.15.0, `HTTPConfig` blocks private/loopback/link-local networks and
cloud metadata endpoints by default** (`allow_private_networks=False`). This
is the right default whenever URLs may come from an untrusted source — user
input, search results, an LLM agent, a third-party API.

**If you deliberately need to crawl localhost, an intranet, or internal
services**, opt in explicitly:

```python
from lazycrawler import WebCrawler, HTTPConfig

crawler = WebCrawler(
    http_cfg=HTTPConfig(allow_private_networks=True),  # ← only for trusted, developer-controlled URLs
)
```

Without opting in, a crafted URL such as
`http://169.254.169.254/latest/meta-data/` (AWS metadata endpoint) or
`http://192.168.1.1/admin` is refused rather than fetched.

> **Upgrading from &lt; 0.15.0?** The default flipped from *allow* to *block*.
> `HTTPConfig.block_private_addresses` (the old, deprecated field) still
> works but only the pre-0.15.0 default needs migrating: if you relied on
> the implicit "private networks reachable" behavior, pass
> `allow_private_networks=True` explicitly now.

> **`CrawlerTools`** (the LazyBridge agent wrapper) always sets the guard
> explicitly regardless of this default — no action needed when using the
> agent path.

> **`AsyncWebCrawler`** inherits the same default, and (like the sync client)
> re-validates **every redirect hop**, not just the seed URL.

> **⚠️ Best-effort, not isolation.** The guard validates the IPs a host resolves
> to *at check time*, but the actual connection re-resolves the host, so a
> hostile DNS server can return a public IP during the check and a private one at
> connect time (DNS rebinding / TOCTOU). For untrusted input, combine the guard
> with OS/network-level egress restrictions — don't rely on it alone.

See [`lazycrawler/http.py`](lazycrawler/http.py#L-is_blocked_address) for the
full list of blocked address categories (RFC-1918, loopback, link-local, cloud
metadata, `*.local`).

---

## Quick start

### Pure mode (no LLM)

```python
from lazycrawler import WebCrawler, CrawlerConfig

crawler = WebCrawler(CrawlerConfig(max_depth=1, max_pages=10))
results = crawler.crawl("https://en.wikipedia.org/wiki/Web_crawler", mode="pure")
for r in results:
    print(r.status, "|", r.title, "|", len(r.text or ""), "chars")
crawler.close()
```

### Smart mode + DB persistence

```python
from lazycrawler import WebCrawler, CrawlerConfig, CrawlerDB, DBConfig, LLMConfig

db = CrawlerDB(DBConfig(db_path="crawl.db", ttl_hours=12))
crawler = WebCrawler(
    CrawlerConfig(max_depth=2, max_pages=20),
    llm_cfg=LLMConfig(model="claude-haiku-4-5"),   # switch provider = change the string
    db=db,
)
results = crawler.crawl(
    "https://en.wikipedia.org/wiki/Renewable_energy",
    mode="smart",
    topic="renewable energy, solar, wind, storage",
    session_id="energy_2026",
)
# follow-up queries on the DB
pages = db.get_pages(session_id="energy_2026")
hits  = db.search_text("photovoltaic")     # full-text (FTS5)
db.close()
```

### WebSearch (crawler seeded from search)

```python
from lazycrawler import WebSearch, SearchConfig

# DuckDuckGo — no API key required
search = WebSearch(SearchConfig(engine="duckduckgo", n_results=8, crawl_depth=0))
out = search.run("james webb telescope discoveries", mode="pure")
print(out["pages_found"], "pages")
for r in out["results"]:
    print(r.title, "—", r.url)

# Brave Search — free tier: 2 000 req/month (set BRAVE_API_KEY env var)
search = WebSearch(SearchConfig(engine="brave", n_results=5, brave_api_key="YOUR_KEY"))
out = search.run("python async web crawlers", mode="pure")

# Tavily — free tier: 1 000 req/month (set TAVILY_API_KEY env var)
search = WebSearch(SearchConfig(engine="tavily", n_results=5, tavily_api_key="YOUR_KEY"))
out = search.run("LLM agent frameworks 2025", mode="pure")
```

---

## Use as a LazyBridge tool (LLM-friendly)

LazyCrawler **is the tool** — you build the agent. `CrawlerTools` is a LazyBridge
`ToolProvider`: drop its `as_tools()` straight into your own agent.

```python
from lazybridge import Agent, LLMEngine
from lazycrawler import CrawlerDB, DBConfig, LLMConfig
from lazycrawler.tools import CrawlerTools

db = CrawlerDB(DBConfig(db_path="research.db"))
crawler_tools = CrawlerTools(db=db, llm_cfg=LLMConfig(model="claude-haiku-4-5"))

agent = Agent(engine=LLMEngine("claude-haiku-4-5"), tools=crawler_tools.as_tools())
print(agent("Research solid-state batteries and summarize the 3 best sources.").text())
```

The tools the agent gets (rich docstrings = the schema the model reads):

| Tool | What the agent does with it |
|------|------------------------------|
| `list_presets()` | discover the named presets (intent + cost) to pass as `preset=` |
| `search_cached(query)` | search already-crawled pages — **free, no network**; try this first |
| `web_search(query, max_results, preset)` | search the web + crawl results into clean pages |
| `web_crawl(url, depth, preset)` | crawl a specific URL (and optionally its links) |
| `get_page(url)` | full stored text of one page (after the snippets above) |

Tools return compact JSON (truncated snippets + a `get_page` hint), so the agent
pulls full text only when it decides to — keeping token usage low. pure/smart
modes are fixed at construction, so the LLM never reasons about cost knobs.
LazyBridge is imported lazily, so pure-mode use never requires it.

### Presets (intent-level configs the agent picks)

Instead of exposing raw knobs (depth, artifacts, markdown, recency…), the agent
selects a **named preset** — an *intent* that bundles a ready-made configuration
with a coarse cost hint. It calls `list_presets()` to discover them, then passes
`preset="…"` to `web_search` / `web_crawl`. An explicit `depth` / `max_results`
still overrides the preset.

| Preset | Bundle | Cost |
|--------|--------|------|
| `quick_lookup` | pure · depth 0 · ~5 pages · no artifacts | minimal |
| `deep_research` | smart content+links · depth 1 · ~20 pages · wide branching (25 links/page) · topic-driven | high |
| `news_scan` | smart (sentiment+date) · depth 0 · last week · more results | medium |
| `extract_data` | pure · tables/images as artifacts · depth 0 | low |
| `rag_ingest` | pure · Markdown + artifact anchors · depth 0 | low |
| `research_ml` | **ml** content+links · best-first · depth 1 · zero tokens | minimal |
| `news_scan_ml` | **ml** content · last-week monitoring (sentiment+entities) · zero tokens | minimal |
| `topic_explore_ml` | **ml** links · semantic best-first · depth 2 · gate 0.35 · maps a topic | low |
| `triage_ml` | **ml** links · strong-only (gate 0.5) · depth 1 · zero-token source shortlist | minimal |
| `rag_ingest_ml` | **ml** content · Markdown anchors + local summary/topics · zero tokens | low |
| `hybrid_research` | **ml** links (free frontier) + **smart** content (LLM on winners) | medium |

The `*_ml` / `hybrid_*` presets are the **zero-token** (or reduced-token) tier — a
semantic best-first frontier and/or local structured extraction, no LLM. See the
[ML Mode guide](https://github.com/selvaz/lazycrawler/blob/main/docs/guides/ml-mode.md).

A preset can also tune the **branching factor** (`max_links_per_level`: links
followed *per page*) — `deep_research` widens it; `quick_lookup`/`news_scan`
follow no links at all (depth 0). Custom presets set it via
`CrawlPreset(max_links_per_level=...)`.

```python
agent("...")   # the model: list_presets() -> web_search(q, preset="deep_research")
```

Presets apply **per call** (the shared `CrawlerConfig` is never mutated, so
concurrent tool calls stay isolated). Add or override presets at construction:

```python
from lazycrawler import CrawlPreset
from lazycrawler.tools import CrawlerTools

crawler_tools = CrawlerTools(
    db=db,
    presets={  # merged on top of the built-in catalog (same key = override)
        "headlines": CrawlPreset(
            name="headlines", description="Front-page scan, last 24h",
            content="smart", links="pure", max_depth=0, max_results=20,
            timelimit="d", cost="medium",
        ),
    },
)
```

> Don't want the `ToolProvider`? Bound methods work directly too:
> `Agent(tools=[crawler_tools.web_search, crawler_tools.get_page])`.

---

## ML mode (smart, without the LLM)

`ml` is a **third value** for the knobs, next to `pure` and `smart` — intelligent
crawling with **local machine learning, zero LLM tokens**:

```python
crawler = WebCrawler(CrawlerConfig(max_depth=2, max_pages=30),
                     ml_cfg=MLConfig(model="minishlab/potion-retrieval-32M"))
results = crawler.crawl("https://example.com/", links="ml", topic="solid-state batteries")
```

`links="ml"` scores every candidate link against the topic (semantic via
[Model2Vec](https://github.com/MinishLab/model2vec) static embeddings + lexical +
structural) and crawls **best-first** — a score-ordered frontier that works
sequential **and** parallel. Needs `pip install "lazycrawler[ml] @ git+https://github.com/selvaz/LazyCrawler.git"`; without it,
scoring degrades to lexical+structural (still topic-aware). `content="ml"` fills
`summary` (TextRank) / `topics` (YAKE) / `entities` (spaCy) / `sentiment` (VADER)
with **local ML, zero tokens** (`pip install "lazycrawler[nlp] @ git+https://github.com/selvaz/LazyCrawler.git"`). The killer
pattern: `links="ml"` for breadth + `content="smart"` only on the winners. See the
[ML mode guide](https://github.com/selvaz/lazycrawler/blob/main/docs/guides/ml-mode.md).

## Switching LLM provider/model

Every LLM call goes through LazyBridge. To switch provider just change the
`model` string — the provider is inferred automatically:

```python
LLMConfig(model="gpt-4o-mini")             # OpenAI
LLMConfig(model="claude-haiku-4-5")        # Anthropic
LLMConfig(model="gemini-3-flash-preview")  # Google
LLMConfig(model="deepseek-chat")           # DeepSeek

# dedicated (cheaper) model for large-document summarization:
LLMConfig(model="claude-sonnet-4-6", large_doc_model="claude-haiku-4-5")
```

---

## Native parallel mode

Set `max_workers > 1` for a bounded thread pool that crawls level-by-level (BFS).
Shared state is thread-safe; each worker gets its own HTTP/LLM resources; the DB
is thread-safe. `max_workers=1` keeps the original sequential DFS.

```python
crawler = WebCrawler(CrawlerConfig(max_depth=2, max_pages=50, max_workers=8))
results = crawler.crawl("https://example.com/", mode="pure")
```

In a deterministic test (1 seed + 12 leaves, simulated latency) parallel is ~3×
faster than sequential. Note: `link_delay` is not applied in parallel mode, but
the per-host rate limiter (`HTTPConfig.per_host_delay`) and robots `Crawl-delay`
**are** — they keep both sequential and parallel crawls polite per host.

### Async mode (`pure` + `ml`)

`AsyncWebCrawler` fetches over aiohttp (non-blocking I/O, `max_workers`-bounded
concurrency) and now supports `ml` mode with full feature parity to the sync
crawler — semantic best-first link selection (`links="ml"`), local content
extraction (`content="ml"`), artifacts, and DB persistence/reporting. It reuses
the **exact** synchronous post-fetch pipeline in a thread executor, so the
CPU-bound ML work never blocks the event loop. `smart` (LLM) mode stays on the
sync `WebCrawler`.

```python
import asyncio
from lazycrawler import CrawlerConfig, HTTPConfig, MLConfig
from lazycrawler.async_crawler import AsyncWebCrawler

async def main():
    cfg = CrawlerConfig(max_depth=2, max_pages=50, max_workers=8)
    async with AsyncWebCrawler(cfg, HTTPConfig(), ml_cfg=MLConfig()) as crawler:
        # zero-token ML: best-first links + local extraction, in parallel
        results = await crawler.crawl(
            "https://example.com/", mode="ml", topic="solid-state batteries"
        )
    for r in results:
        print(r.status, r.mode, r.url, (r.summary or "")[:80])

asyncio.run(main())
```

## Custom output schema (smart content)

Pass any Pydantic model; the LLM fills it (LazyBridge structured output). The full
object lands on `PageResult.data` (and is persisted to `pages.extract_json`); known
fields (`title`/`summary`/`clean_text`/`entities`/`topics`) are mapped when present.

```python
from pydantic import BaseModel, Field

class Article(BaseModel):
    headline: str = Field(default="", description="the main headline")
    author: str = Field(default="", description="author if present")
    key_points: list[str] = Field(default_factory=list, description="3-5 takeaways")

results = crawler.crawl("https://example.com/post", content="smart", schema=Article)
print(results[0].data)   # {'headline': ..., 'author': ..., 'key_points': [...]}
```

## JavaScript rendering (optional)

For SPAs / client-rendered pages, route fetches through a headless browser:

```python
HTTPConfig(render_js=True)   # requires: pip install playwright && playwright install chromium
```

Falls back to plain requests if Playwright is unavailable. The browser context is
owned by the HTTP client and reused across pages; in parallel mode each worker
keeps its own renderer.

## Markdown output (for RAG)

Set `emit_markdown=True` to also render each crawled HTML page to Markdown (heading
hierarchy, lists, tables, links resolved to absolute URLs) — handy for RAG ingestion.
It lands on `PageResult.markdown` and is persisted alongside the page.

```python
crawler = WebCrawler(CrawlerConfig(max_depth=0, emit_markdown=True))
r = crawler.crawl("https://example.com/article", mode="pure")[0]
print(r.markdown)   # "# Title\n\n- bullet\n\n| col | ... |"
```

Needs the `markdown` extra (`pip install "lazycrawler[markdown] @ git+https://github.com/selvaz/LazyCrawler.git"`); without it the
field degrades to a basic text strip instead of failing. PDFs are skipped (no HTML).
The render is independent of pure/smart — it works in both.

## Artifacts (tables, images, charts)

Beyond clean text, the crawler can extract a page's **non-textual content** as
structured `Artifact` records — tables, images, charts and inline SVG —
each kept whole with its caption / surrounding context and provenance.

```python
crawler = WebCrawler(CrawlerConfig(max_depth=0, extract_artifacts=True), db=db)
r = crawler.crawl("https://example.com/report", mode="pure")[0]
for a in r.artifacts:
    print(a.artifact_type, "—", a.caption or a.alt or a.src_url)
    if a.artifact_type == "table":
        print(a.content)   # Markdown table; a.data = structured rows
```

What you get per type (best-practice driven):

| Type | Extraction |
|------|------------|
| **table** | Markdown (`content`) **+** structured rows (`data`), header↔value preserved |
| **image** | absolute `src_url` + `alt` + caption (`<figcaption>`) + ±N chars of context |
| **chart** | images/SVG that look like charts (alt/class/markup heuristics) |
| **svg** | inline SVG markup (chart candidate) |

`<figure>`/`<figcaption>` are used to enrich the contained image/table's caption;
they are not emitted as a separate artifact type.

Tiny/spacer/logo/tracking images are filtered out (`min_image_dim`,
`same_domain_images`). Artifacts are persisted in a dedicated **`artifacts`**
table (FK to the page, deduped per `content_hash`) and reachable via
`db.get_artifacts(url_hash=...)` / `db.get_artifacts(session_id=...)` or the
agent tool `get_artifacts(url)`.

```python
CrawlerConfig(extract_artifacts=True)                    # reference-only (cheap)
CrawlerConfig(extract_artifacts=True,
              download_artifact_bytes=True)              # also fetch image bytes
                                                         #   -> sha256 + blob in DB
CrawlerConfig(extract_artifacts=True, enrich_artifacts=True)  # + vision LLM (smart)
```

**Optional layers** (off by default — pure mode pays nothing):
- `download_artifact_bytes=True` downloads images through the crawler's HTTP
  client (honors SSL + the SSRF guard; the download is streamed and capped by
  `HTTPConfig.max_asset_bytes`), then stores a `sha256` hash + the bytes (only
  blobs ≤ `max_artifact_bytes` are kept). Needs `pip install "lazycrawler[image] @ git+https://github.com/selvaz/LazyCrawler.git"`
  for dimensions/format sniffing (Pillow).
- `enrich_artifacts=True` with `content="smart"` runs a **vision LLM** (via
  LazyBridge) to caption images, read chart trends/data points, and summarize
  tables — capped by `max_artifacts_to_enrich`. Set `LLMConfig(vision_model=...)`
  to use a dedicated vision model.

**PDFs**: with the `pdf` extra, tables (pdfplumber) and embedded images (PyMuPDF)
are also emitted as artifacts.

### Markdown anchors + `render_for_rag()` (multimodal RAG)

By default the Markdown (`emit_markdown`) and the artifacts are two **independent**
representations — tables/images stay inline in the Markdown *and* are copied into
the `artifacts` table. The best-practice layout for RAG is instead **inline
anchors + externalized content**: set `markdown_artifact_anchors=True` and each
table/image in the Markdown is replaced by a stable placeholder
`[[artifact:<hash>]]` (no duplication, position + local context preserved), while
the heavy/structured content lives in `artifacts`.

```python
crawler = WebCrawler(
    CrawlerConfig(extract_artifacts=True, emit_markdown=True,
                  markdown_artifact_anchors=True),
    db=db,
)
r = crawler.crawl("https://example.com/report", mode="pure")[0]
# r.markdown -> "...intro [[artifact:ab12cd]] outro..."  (table externalized)
```

`render_for_rag(page, artifacts=None)` recomposes the two into one chunk-ready
document: the narrative with its inline anchors **plus** a resolvable *Artifacts*
appendix pairing each anchor with its Markdown table / image reference / vision
summary.

```python
from lazycrawler import render_for_rag

doc = render_for_rag(r)                       # from a PageResult
# or from the DB later:
row  = db.get_page(url_hash("https://example.com/report"))
doc  = render_for_rag(row, artifacts=db.get_artifacts(url_hash=row["url_hash"]))
```

This is the multi-vector pattern: embed the artifact **summary** for retrieval,
return the **full** table/image to the model — tables kept whole, images carried
as a reference + text surrogate (caption / vision description).

## SSRF guard (agent safety)

When the crawler is driven by an LLM agent (`CrawlerTools`), the model can pass
arbitrary URLs. `HTTPConfig(block_private_addresses=True)` refuses fetches that
resolve to loopback / link-local / private (RFC-1918) / reserved addresses, plus
`localhost`, `*.local`, and cloud metadata endpoints (e.g. `169.254.169.254`).
**Redirects are followed manually and every hop is re-validated**, so a public
host that 30x-redirects to a private address is blocked too (bounded by
`HTTPConfig.max_redirects`).

```python
HTTPConfig()   # default ON since 0.15.0, for the library and CrawlerTools alike
```

In `CrawlerTools` (the agent path) it, by default, **cannot be turned off**
via `http_cfg` — pass `CrawlerTools(enforce_ssrf_guard=False)` to crawl
internal hosts (this honors your `HTTPConfig`):

```python
CrawlerTools(http_cfg=HTTPConfig(allow_private_networks=True),
             enforce_ssrf_guard=False)   # opt out, deliberately
```

Downloads are also **streamed and size-capped** (`HTTPConfig.max_html_bytes` /
`max_pdf_bytes` / `max_asset_bytes`), so a hostile or huge resource cannot
exhaust memory.

The guard also covers two subtler vectors:
- **`robots.txt` on the final host** — if a fetch redirects to a *different* host
  whose `robots.txt` disallows the path, the content is dropped (`robots_blocked`),
  not stored.
- **canonical-URL poisoning** — a `<link rel="canonical">` pointing to a private
  address is ignored (the page is not re-keyed under, say, `127.0.0.1/admin`).
- **`render_js` is refused with the guard** — a headless browser's redirects and
  subresources bypass the per-hop check, so `HTTPConfig(render_js=True,
  block_private_addresses=True)` raises. Use one or the other.

## Resource cleanup (automatic — no `close()` in the agent path)

You never call `close()` in the agent/tool path, and nothing lingers between calls:

- **Per tool call**: each `web_search` / `web_crawl` **releases its HTTP sockets
  (and browser) at the end of the call** — the call is a self-contained
  transaction. The shared **DB cache stays open** (it's the persistent store),
  and the HTTP session is rebuilt lazily on the next call. Release is
  reference-counted, so a release never closes a session a concurrent call is
  still using.
- **As a backstop**: `HTTPClient` and `CrawlerDB` also arm a `weakref.finalize`,
  so any remaining session / browser / SQLite connection is freed on
  garbage-collection or at interpreter exit.

Lifecycle methods are **not exposed as tools**, so the LLM can only call
`web_search` / `web_crawl` / `get_page` / …:

```python
crawler_tools = CrawlerTools(db=db, llm_cfg=LLMConfig(model="claude-haiku-4-5"))
agent = Agent(engine=engine, tools=crawler_tools.as_tools())
agent("Research solid-state batteries.")   # no close() anywhere; sockets freed per call
```

`close()` / `with` remain available for **deterministic** full teardown (they
release immediately and are idempotent — a second `close()` is a safe no-op):

```python
with WebCrawler(CrawlerConfig(max_depth=1)) as crawler:   # optional, deterministic
    results = crawler.crawl("https://example.com/", mode="pure")
```

## robots.txt & politeness

`robots.txt` is honored **by default**. URLs disallowed for the configured
User-Agent are skipped and reported with `status="robots_blocked"` (never
silently dropped). robots.txt is fetched once per host through the configured
HTTP client (so it honors `verify_ssl` / `ca_bundle`), and a
missing/unreachable robots.txt means "allow".

```python
CrawlerConfig(respect_robots=True)    # default
CrawlerConfig(respect_robots=False)   # ignore robots.txt (your own/authorized sites)
```

Politeness has three layers: a global `HTTPConfig.link_delay` between sequential
fetches, a **per-host rate limiter** (`HTTPConfig.per_host_delay`, applied in
both sequential and parallel mode), and robots.txt **`Crawl-delay`**, which is
honored on top of `per_host_delay` (the effective gap per host is the larger of
the two) whenever `respect_robots` is on. The crawler also sends a dedicated
`LazyCrawler/<version>` User-Agent instead of masquerading as a browser.

## Logging & error handling

Nothing is silently swallowed. Every caught exception is logged through the
`lazycrawler` logger (with a traceback at WARNING/ERROR); best-effort fallbacks
(optional libs, date parsing, FTS) log at DEBUG. By default the logger emits to
stderr at INFO.

```python
import logging
from lazycrawler import set_log_level

set_log_level(logging.WARNING)   # quieter (errors/warnings only)
set_log_level(logging.DEBUG)     # verbose (best-effort failures too)
logging.getLogger("lazycrawler").handlers.clear()   # take full control
```

For fail-fast instead of resilient crawling, use **strict mode** — per-page /
per-worker exceptions then propagate instead of being logged-and-skipped:

```python
CrawlerConfig(strict=True)   # raise on the first page/worker error
```

---

## Architecture

```
lazycrawler/
├── config.py        configuration dataclasses (Crawler/HTTP/LLM/Search/DB/ML)
├── models.py        PageResult (public output type) + Artifact
├── _log.py          logging setup (set_log_level helper)
├── http.py          HTTPClient + URL utils + hashing + SSRF guard
├── ratelimit.py     HostRateLimiter (per-host polite delay)
├── text.py          preprocessing + link/date/canonical/title extraction
├── pdf.py           remote PDF extraction (PyMuPDF → pypdf → pdfplumber)
├── prompts.py       LLM prompts (smart mode only, domain-agnostic)
├── llm.py           LazyBridge wrapper (structured output via output=PydanticModel)
├── ml.py            MLEngine: semantic scoring (Model2Vec) + NLP extraction
├── markdown.py      optional HTML→Markdown renderer (RAG ingestion)
├── artifacts.py     tables/images/charts/svg extraction (Artifact model)
├── db.py            SQLite: sessions + pages + crawl_edges + artifacts, dedup, TTL, FTS5
├── _pipeline.py     per-page pipeline (fetch → extract → enrich → emit); shared sync/async
├── crawler.py       WebCrawler (pure + ml + smart, sequential + parallel)
├── async_crawler.py AsyncWebCrawler (aiohttp, pure + ml, high-throughput; reuses _pipeline)
├── search.py        WebSearch (DDG / Brave / Tavily / Gemini)
├── presets.py       named preset catalog (CrawlPreset, DEFAULT_PRESETS)
└── tools.py         LazyBridge ToolProvider (CrawlerTools)
```

### DB schema

| Table | Role |
|-------|------|
| `sessions` | one row per run (topic, seed, mode, source) |
| `pages` | global content cache, keyed by `url_hash` (cross-session) |
| `crawl_edges` | which session reached which page, from where, at what depth |
| `artifacts` | non-textual content per page (tables/images/charts), FK to `pages` |

Pages are **no longer** tied to a single session: the content is a shared cache,
and `crawl_edges` record provenance. The same URL crawled in different runs lives
once in `pages` with multiple edges.

### The DB cache (mode-aware)

When the DB is attached, the crawler **checks if the page is already stored**
before fetching. If a fresh copy exists, it is returned **from the DB** (no
re-fetch), and what you get depends on the requested mode:

- **pure** → the stored clean text
- **smart** → the stored summary + structured fields
- **pure cached, smart requested** → the page is **enriched** by running the LLM
  on the stored text — still **no re-fetch** (level-3 dedup)

### 3-level dedup

1. **URL (pre-fetch)** — a `done` page within the TTL → skip fetch, just add the
   edge. *Saves HTTP.*
2. **Content (post-fetch, pre-LLM)** — `content_hash = sha256(raw_text)` already
   present → reuse the row, skip the LLM. *Saves tokens.*
3. **Smart-on-pure** — a `pure` page can be enriched to `smart` without
   re-fetching (the `raw_text` is already stored).

`DBConfig.ttl_hours` controls cache freshness; `force_refresh=True` bypasses it.

> By default a cached hit is **terminal** (no link recursion). The candidate
> links found at crawl time are now stored on the page, so you can set
> `CrawlerConfig(recurse_from_cache=True)` to keep following them from a warm
> cache — the frontier is then the same whether the DB is cold or warm, with no
> re-fetch. Otherwise, use `force_refresh` or a shorter TTL to follow links
> freshly.

---

## Environments with SSL inspection (antivirus / proxy)

Antivirus such as **Avast** or corporate proxies MITM HTTPS with a root cert that
Python does not recognize → `SSLCertVerificationError`. Two options:

```python
# Secure (recommended): point at the antivirus/proxy cert
HTTPConfig(ca_bundle=r"C:\path\to\proxy_root.pem")

# Quick (trusted environments only): disable verification
HTTPConfig(verify_ssl=False)
```

> This covers the crawler's own fetches — HTML, **PDF downloads**, and the
> robots.txt fetch all honor `verify_ssl` / `ca_bundle`. For smart-mode LLM
> calls, TLS is handled by LazyBridge / the provider SDK.

---

## News-monitor pipeline (financial + geopolitical)

A ready-to-run application built on top of the crawler: 3x/day, it pulls a
curated, VPS-accessibility-checked list of ~35 RSS/Atom/RDF feeds (financial
wires, central banks, major geopolitical outlets, and local-language regional
sources), crawls every item, and sends a report to Telegram — no LLM for the
English-language sources (`content="ml"`: TextRank summary, YAKE topics,
spaCy entities, VADER sentiment), DeepSeek (`content="smart"`) only for the
local-language sources where the English-tuned ml pipeline would degrade.

```
news_sources.py                       curated source list (name, feed url, category, region, lang, mode)
run_news_crawl.py                     fetches every feed, crawls each item, persists to a dedicated SQLite DB
make_news_report.py                   builds a full report per geographic region + a DeepSeek executive digest
send_telegram_news_report.py          sends the digest + per-region reports + a per-run cost report to Telegram
run_news_crawl_with_telegram.ps1      wrapper chaining the three scripts above (what the scheduled tasks run)
setup_scheduler.ps1                   registers the 3 daily Windows scheduled tasks
setup_first_run.ps1                   interactive bootstrap: installs deps, prompts for API keys, smoke-tests
```

### First-run setup (new machine)

```powershell
git clone https://github.com/selvaz/LazyCrawler.git
cd LazyCrawler
powershell -ExecutionPolicy Bypass -File .\setup_first_run.ps1 -ConfigureScheduler
```

Prompts for `DEEPSEEK_API_KEY`, `TELEGRAM_BOT_TOKEN`, and `TELEGRAM_CHAT_ID`
(persisted as User environment variables — press Enter to skip any of them),
installs LazyCrawler + the `smart`/`ml`/`nlp`/`news` extras, LazyBridge and
LazyTools (from a local sibling checkout if present, else PyPI/GitHub), the
spaCy English model, runs the test suite, and — if a DeepSeek key is set — a
small 4-article smoke test so you can see real output before trusting the
schedule. `-ConfigureScheduler` registers the 3 daily tasks; see
`setup_scheduler.ps1` for the exact times and the Pacific/local-timezone math
behind them (rerun without it, then run `setup_scheduler.ps1` directly, if
you want to tune the times first).

Each per-run report includes, per region, an **index** (title, source, news
type, and a DeepSeek-written 2-4 sentence English summary for every article
regardless of the source's own language) followed by the **full articles**
in their original language — see [`examples/news_reports/`](examples/news_reports/)
for real sample output (digest, per-region report, cost report).

### Per-run cost

Every LLM call (smart-mode extraction, index summaries, the digest) is
tracked through a `lazybridge.session.Session` and rolled up into
`news_cost_<session>.md` — typically **$0.15-0.30 per run** / **$0.50-0.75
per day** (3 runs) on `deepseek-v4-flash`, dominated by smart-mode
extraction for the local-language sources; see
[`examples/news_reports/news_cost.md`](examples/news_reports/news_cost.md).

---

## Notes

- **PyMuPDF absent** → PDFs degrade (pypdf, then no text). Install
  `pip install pymupdf` for best quality.
- **Pure mode = zero LLM**: no LazyBridge agent is ever built.
- **robots.txt** is honored by default (`respect_robots=False` to disable);
  blocked URLs are reported as `status="robots_blocked"`.
- **Exceptions are never swallowed** — they go through the `lazycrawler` logger;
  use `strict=True` to fail fast instead of logging-and-continuing.
- **WebSearch engines**: `"duckduckgo"` (no key, unofficial API), `"brave"` (free
  2 000 req/month, own index), `"tavily"` (free 1 000 req/month, LLM-optimised),
  `"gemini"`. Brave and Tavily require no extra Python dependencies beyond
  `requests`. **Note:** `"gemini"` is **not a crawl** — it returns a single
  *synthetic* grounded answer with **no verifiable, fetchable source URLs** (the
  result is flagged `notes="synthetic: …"`); treat it as an answer, not as audited
  sources. The other three return real, navigable pages.
