# LazyCrawler

A **generic** web crawler + search with **two modes** and SQLite persistence,
built for the [LazyBridge](https://github.com/selvaz/LazyBridge) ecosystem.
Works on any kind of web content — not tied to any domain.

There are **two independent LLM knobs**, toggled separately:

| Knob | `pure` | `smart` |
|------|--------|---------|
| **content** (page text) | trafilatura/regex, raw clean text | LLM structured extraction (title, summary, entities, topics) |
| **links** (which to follow) | heuristic (first N, filtered) | LLM relevance ranking against the topic |

`mode` is a shortcut that sets both; `content=` / `links=` override either one:

```python
crawl(url, mode="smart")                     # content=smart, links=smart
crawl(url, mode="pure")                       # content=pure,  links=pure   (no LLM)
crawl(url, content="smart", links="pure")     # LLM summary, heuristic links
crawl(url, content="pure",  links="smart")    # no summary, LLM picks the links
```

**WebSearch is a derivation of WebCrawler**: it seeds itself from a search
engine's results (DuckDuckGo or Gemini grounded) and then crawls.

> **Status**: standalone development. When production-ready it will migrate into
> `lazytools.connectors.web` (client + tools pattern, like gmail/telegram).

---

## Install

```bash
# Core — enough for PURE mode (no LLM)
pip install -e .

# With every extra (smart, pdf, search, excel, dates)
pip install -e ".[all]"

# Or selectively:
pip install -e ".[smart]"   # LazyBridge (LLM)
pip install -e ".[pdf]"     # pymupdf, pypdf, pdfplumber
pip install -e ".[search]"  # ddgs (DuckDuckGo)
```

**Smart mode** requires LazyBridge on the path and an API key for the chosen
provider (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`,
`DEEPSEEK_API_KEY`). In the ecosystem, `spyder_startup.py` adds LazyBridge to the
path and loads `.env`.

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

search = WebSearch(SearchConfig(engine="duckduckgo", n_results=8, crawl_depth=0))
out = search.run("james webb telescope discoveries", mode="pure")
print(out["pages_found"], "pages")
for r in out["results"]:
    print(r.title, "—", r.url)
```

---

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

## Architecture

```
lazycrawler/
├── config.py    configuration dataclasses (Crawler/HTTP/LLM/Search/DB)
├── http.py      HTTPClient + URL utils + hashing + blacklist
├── text.py      preprocessing + link/date/canonical/title extraction
├── pdf.py       remote PDF extraction (PyMuPDF → pypdf → pdfplumber)
├── prompts.py   LLM prompts (smart mode only, domain-agnostic)
├── llm.py       LazyBridge wrapper (structured output via output=PydanticModel)
├── db.py        SQLite: sessions + pages + crawl_edges, dedup, TTL, FTS5
├── crawler.py   WebCrawler (pure + smart)
└── search.py    WebSearch (a derivation of the crawler)
```

### DB schema

| Table | Role |
|-------|------|
| `sessions` | one row per run (topic, seed, mode, source) |
| `pages` | global content cache, keyed by `url_hash` (cross-session) |
| `crawl_edges` | which session reached which page, from where, at what depth |

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

> Cached hits are terminal (no link recursion: HTML is not stored). To follow
> links freshly, use `force_refresh` or a shorter TTL.

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

> This covers the crawler's own fetches. For smart-mode LLM calls, TLS is handled
> by LazyBridge / the provider SDK.

---

## Notes

- **PyMuPDF absent** → PDFs degrade (pypdf, then no text). Install
  `pip install pymupdf` for best quality.
- **Pure mode = zero LLM**: no LazyBridge agent is ever built.
- **WebSearch engine="gemini"**: runs in *answer mode* (the grounded answer as a
  single result). Grounding source URLs do not surface through LazyBridge's Agent
  layer; crawling Gemini seed URLs awaits a grounding passthrough in LazyBridge.
  To crawl search results, use `engine="duckduckgo"`.
```
