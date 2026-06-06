# LazyCrawler — Codebase Analysis

> Analysis of LazyCrawler v0.1.0 — findings, observations, and suggestions.
> Scope: read-only review of the full package, tests, and examples, plus
> verification of the LazyBridge integration contract against the LazyBridge
> source. The network-free test suite (`tests/decoupled_test.py`) was run and
> passes 12/12.

---

## 1. What it is

A **generic, domain-agnostic web crawler + search library** (~2,650 LOC across
10 modules, MIT, requires Python ≥ 3.10), built for the **LazyBridge ecosystem**.
It crawls any web content, persists to SQLite, and optionally uses an LLM. The
project is explicitly **pre-production**: when ready it will migrate into
`lazytools.connectors.web`.

The defining design idea is **two independent LLM "knobs"**, toggled separately:

| Knob | `pure` | `smart` |
|------|--------|---------|
| **content** (page text) | trafilatura/regex clean text | LLM structured extraction (title, summary, entities, topics) |
| **links** (which to follow) | heuristic (first N, filtered) | LLM relevance ranking vs. topic |

`mode=` sets both; `content=` / `links=` override either one independently. The
hard rule — confirmed by tests — is that `pure/pure` **never imports or builds an
LLM**, so the core has zero LLM dependency.

---

## 2. Architecture

```
config.py    5 dataclasses (Crawler/HTTP/LLM/Search/DB)
http.py      HTTPClient (retry/backoff) + URL normalize/hash + domain blacklist
text.py      regex preprocessing + link/date/canonical/title extraction (no LLM)
pdf.py       remote PDF extraction: PyMuPDF -> pypdf -> pdfplumber (graceful degrade)
prompts.py   4 system prompts (smart mode only, domain-agnostic)
llm.py       LazyBridge wrapper, structured output via output=PydanticModel
db.py        SQLite: sessions + pages + crawl_edges, 3-level dedup, TTL, FTS5
crawler.py   WebCrawler — recursive engine, the orchestrator
search.py    WebSearch — crawler seeded from DuckDuckGo or Gemini grounding
```

**Data model.** Pages are a *global content cache* keyed by `url_hash`, decoupled
from sessions; `crawl_edges` records provenance (which session reached which page,
from where, at what depth). The same URL across runs lives once in `pages` with
multiple edges.

**3-level dedup** is the standout feature:

1. **URL pre-fetch** (TTL cache) → skip the HTTP fetch entirely. *Saves HTTP.*
2. **Content hash** post-fetch (`sha256` of raw text) → skip the LLM. *Saves tokens.*
3. **Smart-on-pure** → a page cached as `pure` is enriched to `smart` by running
   the LLM on the *stored* text, with no re-fetch.

---

## 3. Strengths

- **Clean separation of concerns** — each module is single-purpose, configs hold
  only their own parameters, pure functions are isolated and testable.
- **Genuine optionality** — every heavy dependency (LazyBridge, PyMuPDF, ddgs,
  openpyxl, dateutil) degrades gracefully if absent; lazy imports mean you only
  pay for what you use.
- **LazyBridge integration is correct** — verified against LazyBridge source:
  `Agent(engine=, output=)`, `LLMEngine(model, system=, temperature=,
  request_timeout=)`, and the `env.ok` / `env.payload` / `env.text()` /
  `env.error.message` envelope contract all match. Provider-switching by changing
  the model string is real.
- **Sensible crawler hygiene** — URL normalization strips tracking params
  (UTM/gclid/fbclid), canonical-URL resolution, an exclusion regex for
  login/social/nav links, exponential backoff on 429/5xx, map-reduce for large
  documents.
- **Tests cover the key invariant** — `decoupled_test.py` proves the four knob
  combinations dispatch correctly with a fake LLM and stubbed network (12/12 pass).

---

## 4. Findings & suggestions

### 4.1 — PDF fetching bypasses the SSL/proxy configuration  *(bug, high priority)*

The README prominently markets `HTTPConfig(ca_bundle=...)` / `verify_ssl=False`
for SSL-inspection environments (Avast, corporate proxies). But
`pdf.py:fetch_pdf_bytes` uses `urllib.request.urlopen` directly instead of the
configured `requests` session — so it honors **neither** `ca_bundle`,
`verify_ssl`, nor proxy settings.

**Impact:** In exactly the MITM environments the README targets, PDF downloads
fail with `SSLCertVerificationError` while HTML pages succeed.

**Suggestion:** Route PDF fetches through `HTTPClient` (or pass `verify`/proxies
into `extract_pdf`). The `HTTPConfig` is already available to the crawler at the
call site in `crawler.py:_crawl_page`.

### 4.2 — No `robots.txt` compliance  *(policy/ethics gap)*

For a tool described as a "generic web crawler," there is no robots.txt checking
or crawl-delay honoring, and it ships a spoofed Chrome User-Agent. Politeness is
limited to a single global `link_delay` (no per-domain rate limiting).

**Suggestion:** Before promoting to production, add opt-in robots.txt support
(e.g. `urllib.robotparser`) and per-domain rate limiting, or at minimum document
that the crawler does not honor robots.txt and is intended for authorized
crawling only.

### 4.3 — Content-hash dedup creates content-alias rows  *(design note)*

Level-2 dedup writes a *new* `pages` row (via `_copy_content`) sharing the same
`content_hash`, so identical content reached through different URLs is stored N
times. This is intentional for per-URL provenance, but it inflates storage and
makes `find_by_content_hash` rely on `LIMIT 1`.

**Suggestion:** Document the trade-off explicitly, or offer a dedup-by-reference
option (store content once, point aliases at it) for storage-sensitive deployments.

### 4.4 — Cosmetic / consistency  *(low priority)*

- `pyproject.toml` and `requirements.txt` carry **Italian** comments while the
  rest of the codebase is English — normalize before publishing.
- Default `LLMConfig.model` is `"gpt-4o-mini"` (OpenAI) even though README
  examples lead with Claude (`claude-haiku-4-5`). Pick one default and make the
  docs/config agree.

### 4.5 — Minor  *(nice-to-have)*

- `same_domain_only` matching uses `get_base_domain` (full netloc incl. port)
  rather than a registered-domain comparison, so `example.com:8080` vs
  `example.com` would not match.
- The `[CRAWL]` log line in `crawl_many` always prints `max_depth` even when
  WebSearch overrides it to `crawl_depth` (the value is correct internally; only
  the log can mislead).
- Smart-mode content extraction and large-doc summarization are sequential per
  page; a bounded concurrency option could materially cut wall-clock time on
  larger crawls.

---

## 5. Bottom line

A **tidy, well-architected v0.1 library** with a genuinely clever cache/dedup
model and a clean LLM-optional design correctly wired to LazyBridge.

- **Most actionable fix:** route PDF downloads through the configured HTTP client
  (§4.1) so the SSL/proxy story the README promises holds end-to-end.
- **Most important pre-production decision:** whether to add robots.txt /
  politeness controls (§4.2).

Everything else is polish.
