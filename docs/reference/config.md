# Configuration Reference

All config classes are dataclasses with sensible defaults — only override what you need.

```python
from lazycrawler.config import CrawlerConfig, HTTPConfig, LLMConfig, MLConfig, SearchConfig, DBConfig
```

Full example combining all configs:

```python
from lazycrawler import WebCrawler, CrawlerDB
from lazycrawler.config import CrawlerConfig, HTTPConfig, LLMConfig, DBConfig

db = CrawlerDB(DBConfig(db_path="my.db"))
crawler = WebCrawler(
    crawler_cfg=CrawlerConfig(max_depth=3, max_pages=50),
    http_cfg=HTTPConfig(link_delay=1.5, verify_ssl=False),
    llm_cfg=LLMConfig(model="gpt-4o-mini"),
    db=db,
)
```

---

## CrawlerConfig

Controls traversal depth, page limits, domain filtering, and blacklists.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `max_depth` | `int` | `2` | Maximum link-hop depth from the seed URL |
| `max_pages` | `int` | `20` | Hard upper limit on total pages collected |
| `max_links_per_level` | `int` | `15` | Branching factor: links to follow **per page** (despite the name, it is enforced once per page, not per depth level) |
| `max_candidate_links` | `int` | `300` | Max raw link candidates extracted per page before selection (the pool `max_links_per_level` is chosen from) |
| `same_domain_only` | `bool` | `True` | Restrict to the seed's registrable *site* (parent/sibling subdomains allowed) |
| `same_host_only` | `bool` | `False` | With `same_domain_only`, restrict to the exact same hostname |
| `max_workers` | `int` | `1` | Thread pool size. `1` = sequential DFS, `N>1` = parallel BFS |
| `respect_robots` | `bool` | `True` | Honour `robots.txt` (including `Crawl-delay`) |
| `strict` | `bool` | `False` | Raise exceptions on errors instead of recording them |
| `recurse_from_cache` | `bool` | `False` | Follow a cached page's stored links instead of stopping (same frontier cold vs warm, no re-fetch) |
| `exclude_patterns` | `list[str] \| None` | `None` | Regex fragments for link exclusion. `None` = built-in default (no longer drops `/about`, `/contact`, `/tag/`, `/category/`, `/author/`) |
| `max_chars_content` | `int` | `100_000` | Hard limit on HTML size processed per page |
| `max_chars_pure` | `int` | `10_000` | Char limit for pure-mode text output |
| `large_doc_threshold` | `int` | `20_000` | Chars above which smart mode uses map-reduce |
| `large_doc_chunk_chars` | `int` | `12_000` | Chunk size for map-reduce summarization |
| `large_doc_max_chunks` | `int` | `12` | Max chunks processed in map-reduce |
| `emit_markdown` | `bool` | `False` | Render each crawled HTML page to Markdown (`PageResult.markdown`). Requires `pip install lazycrawler[markdown]` |
| `extract_artifacts` | `bool` | `False` | Extract tables, images, charts, SVG as structured `Artifact` records (HTML + PDF). See the [Artifacts guide](../guides/artifacts.md) |
| `artifact_types` | `tuple` | `("table","image","chart","svg")` | Which artifact types to collect |
| `download_artifact_bytes` | `bool` | `False` | Download image/chart bytes through the crawler (honors SSL + SSRF guard) → `sha256` + blob in DB |
| `max_artifact_bytes` | `int` | `5_000_000` | Max image size stored as a blob (larger → keep hash/metadata only) |
| `min_image_dim` | `int` | `48` | Drop images whose declared width/height is below this (filters icons/spacers) |
| `artifact_context_chars` | `int` | `200` | Chars of surrounding text captured for images lacking a caption |
| `max_artifacts_per_page` | `int` | `100` | Hard cap on artifacts collected per page |
| `same_domain_images` | `bool` | `False` | Keep only images hosted on the page's own domain |
| `enrich_artifacts` | `bool` | `False` | Vision-LLM enrichment of artifacts (requires `content="smart"`) — captions, chart data, table summaries |
| `max_artifacts_to_enrich` | `int` | `8` | Per-page cap on LLM-enriched artifacts (cost control) |
| `markdown_artifact_anchors` | `bool` | `False` | With `emit_markdown + extract_artifacts`: replace each table/image in Markdown with `[[artifact:<hash>]]` anchors instead of duplicating inline content. Use `render_for_rag()` to recompose |
| `blacklist` | `list[str]` | `[]` | URL prefixes or domain names to skip |
| `blacklist_excel` | `str` | `""` | Path to Excel file with blacklisted URLs |
| `blacklist_excel_sheet` | `str \| None` | `None` | Sheet name (first sheet if None) |
| `blacklist_excel_column` | `str \| None` | `None` | Column name/letter (first column if None) |

!!! tip "Per-call overrides & presets"
    Most of these fields can be overridden for a **single call** via
    `WebCrawler.crawl(..., overrides={...})` without mutating the shared config —
    the mechanism behind named **presets** (`CrawlPreset`). See the
    [Presets guide](../guides/presets.md).

---

## HTTPConfig

Controls HTTP client behaviour, timeouts, SSL, polite delays, and JavaScript rendering.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `user_agent` | `str` | `"LazyCrawler/0.12 (+...)"` | HTTP User-Agent header (dedicated, not a spoofed browser) |
| `timeout_connect` | `int` | `5` | TCP connection timeout (seconds) |
| `timeout_read` | `int` | `25` | Read timeout (seconds) |
| `max_retries` | `int` | `4` | Max retry attempts on transient failures |
| `backoff_base_sec` | `float` | `1.0` | Exponential backoff base (seconds) |
| `link_delay` | `float` | `1.0` | Seconds between fetches in **sequential** mode (politeness) |
| `per_host_delay` | `float` | `0.0` | Min seconds between fetches to the **same host**, in both sequential and parallel mode. robots `Crawl-delay` is honored on top (effective = larger of the two). `0` disables |
| `min_text_chars` | `int` | `50` | Minimum extracted-text length to accept (shorter pages no longer become `no_text`; was a hardcoded 200) |
| `pdf_timeout` | `int` | `60` | Timeout for PDF downloads (seconds) |
| `max_redirects` | `int` | `5` | Max redirect hops; each hop is re-validated by the SSRF guard |
| `max_html_bytes` / `max_pdf_bytes` / `max_asset_bytes` | `int` | 5MB / 50MB / 5MB | Streamed download caps (memory safety) |
| `verify_ssl` | `bool` | `True` | Verify SSL certificates. Set `False` for Avast/Zscaler MITM |
| `ca_bundle` | `str` | `""` | Path to custom CA certificate bundle (PEM) |
| `render_js` | `bool` | `False` | Use Playwright for JS/SPA rendering |
| `browser_headless` | `bool` | `True` | Headless browser. Set `False` to watch the browser during debug |
| `browser_wait_until` | `str` | `"domcontentloaded"` | Playwright wait event: `domcontentloaded`, `load`, `networkidle` |
| `browser_timeout_ms` | `int` | `30000` | Playwright page load timeout (milliseconds) |

---

## LLMConfig

Controls LLM model selection and request parameters for smart mode.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `model` | `str` | `"gpt-4o-mini"` | Model string — provider inferred by LazyBridge |
| `large_doc_model` | `str` | `""` | Model for large-doc map-reduce. `""` = use `model` |
| `vision_model` | `str` | `""` | Vision model for artifact enrichment (image caption / chart data). `""` = use `model` |
| `temperature` | `float` | `0.0` | LLM sampling temperature |
| `request_timeout` | `float` | `120.0` | Max seconds per LLM request |
| `max_links_excerpt_chars` | `int` | `3_000` | Page excerpt chars sent to LLM for link selection |
| `max_candidates_to_llm` | `int` | `80` | Max candidate links shown to LLM for selection |

### Model string examples

| Provider | Example model strings |
|---|---|
| OpenAI | `"gpt-4o-mini"`, `"gpt-4o"`, `"gpt-4-turbo"` |
| Anthropic | `"claude-haiku-4-5"`, `"claude-sonnet-4-6"`, `"claude-opus-4-8"` |
| Google | `"gemini-3-flash-preview"`, `"gemini-2-pro"` |
| DeepSeek | `"deepseek-chat"`, `"deepseek-coder"` |

### Cost-saving pattern

```python
LLMConfig(
    model="gpt-4o-mini",          # cheap model for per-page extraction
    large_doc_model="gpt-4o-mini", # same model for large docs (or omit)
)
```

For maximum quality on research tasks:

```python
LLMConfig(
    model="claude-sonnet-4-6",        # good extraction quality
    large_doc_model="claude-haiku-4-5",  # cheaper for long docs
)
```

---

## MLConfig

Configuration for **`ml` mode** — the no-LLM, zero-token engine (semantic link
scoring + local structured extraction). See the [ML Mode guide](../guides/ml-mode.md).

| Parameter | Type | Default | Description |
|---|---|---|---|
| `model` | `str` | `"minishlab/potion-retrieval-32M"` | Model2Vec static-embedding model (semantic scoring + TextRank summary). Shared across workers; needs `pip install lazycrawler[ml]` |
| `w_sem` | `float` | `0.55` | Weight of the **semantic** signal in the link score |
| `w_lex` | `float` | `0.20` | Weight of the **lexical** (token overlap) signal |
| `w_struct` | `float` | `0.25` | Weight of the **structural** (URL/anchor) signal |
| `best_first` | `bool` | `True` | `links="ml"` crawls best-first (score-ordered frontier; sequential & parallel). `False` = DFS/BFS with per-page top-N |
| `min_link_score` | `float` | `0.0` | Drop frontier links scoring below this (0 = keep all) |
| `max_candidates_to_embed` | `int` | `400` | Cap on links semantically embedded per page (rest use lexical+structural) |
| `summary_sentences` | `int` | `4` | `content="ml"`: sentences kept in the TextRank summary |
| `keyphrase_topk` | `int` | `8` | `content="ml"`: number of YAKE keyphrases → `topics` |
| `sentiment` | `bool` | `True` | `content="ml"`: compute VADER sentiment (else `"neutral"`) |
| `use_spacy_ner` | `bool` | `True` | `content="ml"`: use spaCy NER for `entities` (else regex fallback) |

```python
from lazycrawler import WebCrawler, MLConfig
crawler = WebCrawler(ml_cfg=MLConfig(model="minishlab/potion-base-8M", w_sem=0.6))
crawler.crawl("https://example.com/", mode="ml", topic="solid-state batteries")
```

Needs `pip install lazycrawler[ml]` (scoring + summary) and `lazycrawler[nlp]`
(YAKE/VADER/spaCy for content). Every layer degrades gracefully if its dep is absent.

---

## SearchConfig

Controls web search behaviour for `WebSearch`.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `engine` | `str` | `"duckduckgo"` | Search engine: `"duckduckgo"`, `"brave"`, `"tavily"`, or `"gemini"` |
| `n_results` | `int` | `10` | Number of search results to fetch |
| `crawl_depth` | `int` | `0` | Depth to crawl each result page (0 = just the page itself) |
| `same_domain_only` | `bool` | `False` | Stay on the result domain when `crawl_depth > 0` |
| `expand_topic` | `bool` | `True` | Use LLM to expand the query into a rich topic description |
| `gemini_model` | `str` | `"gemini-3-flash-preview"` | Model for Gemini grounded search |
| `region` | `str` | `"wt-wt"` | Region code (e.g. `"us-en"`, `"gb-en"`). Used by DuckDuckGo and Brave (`"wt-wt"` = global) |
| `timelimit` | `str \| None` | `None` | Time filter: `"d"` (day), `"w"` (week), `"m"` (month), `"y"` (year). Supported by DuckDuckGo, Brave and Tavily |
| `safesearch` | `str` | `"moderate"` | Safe-search: `"off"`, `"moderate"`, `"strict"`. Supported by DuckDuckGo and Brave |
| `backend` | `str` | `"auto"` | DuckDuckGo backend passed through to ddgs. Ignored by other engines |
| `brave_api_key` | `str` | `""` | Brave Search API key. Falls back to `BRAVE_API_KEY` env var. Required for `engine="brave"` |
| `tavily_api_key` | `str` | `""` | Tavily API key. Falls back to `TAVILY_API_KEY` env var. Required for `engine="tavily"` |
| `tavily_search_depth` | `str` | `"basic"` | Tavily depth: `"basic"` (1 credit/req) or `"advanced"` (2 credits/req, deeper recall) |

### Engine quick reference

| Engine | API key | Free tier | Extra deps |
|--------|---------|-----------|------------|
| `"duckduckgo"` | No | Unlimited (unofficial) | `pip install ddgs` |
| `"brave"` | `BRAVE_API_KEY` | 2 000 req/month | None |
| `"tavily"` | `TAVILY_API_KEY` | 1 000 req/month | None |
| `"gemini"` | `GOOGLE_API_KEY` | Per Google quota | LazyBridge |

---

## DBConfig

Controls SQLite persistence.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `db_path` | `str` | `"lazycrawler.db"` | Path to SQLite file. `":memory:"` for in-memory (tests) |
| `ttl_hours` | `float` | `24.0` | Cache TTL — pages older than this are considered stale |
| `force_refresh` | `bool` | `False` | Ignore TTL; always re-fetch every URL |
| `enable_fts` | `bool` | `True` | Enable FTS5 full-text search index |
