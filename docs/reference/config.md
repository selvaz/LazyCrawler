# Configuration Reference

All config classes are dataclasses with sensible defaults — only override what you need.

```python
from lazycrawler.config import CrawlerConfig, HTTPConfig, LLMConfig, SearchConfig, DBConfig
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
| `max_links_per_level` | `int` | `15` | Max links to follow per page per depth level |
| `max_candidate_links` | `int` | `300` | Max raw link candidates extracted before selection |
| `same_domain_only` | `bool` | `True` | Restrict crawl to the same domain as the seed |
| `max_workers` | `int` | `1` | Thread pool size. `1` = sequential DFS, `N>1` = parallel BFS |
| `respect_robots` | `bool` | `True` | Honour `robots.txt` |
| `strict` | `bool` | `False` | Raise exceptions on errors instead of recording them |
| `max_chars_content` | `int` | `100_000` | Hard limit on HTML size processed per page |
| `max_chars_pure` | `int` | `10_000` | Char limit for pure-mode text output |
| `large_doc_threshold` | `int` | `20_000` | Chars above which smart mode uses map-reduce |
| `large_doc_chunk_chars` | `int` | `12_000` | Chunk size for map-reduce summarization |
| `large_doc_max_chunks` | `int` | `12` | Max chunks processed in map-reduce |
| `blacklist` | `list[str]` | `[]` | URL prefixes or domain names to skip |
| `blacklist_excel` | `str` | `""` | Path to Excel file with blacklisted URLs |
| `blacklist_excel_sheet` | `str \| None` | `None` | Sheet name (first sheet if None) |
| `blacklist_excel_column` | `str \| None` | `None` | Column name/letter (first column if None) |

---

## HTTPConfig

Controls HTTP client behaviour, timeouts, SSL, polite delays, and JavaScript rendering.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `user_agent` | `str` | Mozilla/5.0 ... | HTTP User-Agent header |
| `timeout_connect` | `int` | `5` | TCP connection timeout (seconds) |
| `timeout_read` | `int` | `25` | Read timeout (seconds) |
| `max_retries` | `int` | `4` | Max retry attempts on transient failures |
| `backoff_base_sec` | `float` | `1.0` | Exponential backoff base (seconds) |
| `link_delay` | `float` | `1.0` | Seconds to wait between page fetches (politeness) |
| `pdf_timeout` | `int` | `60` | Timeout for PDF downloads (seconds) |
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

## SearchConfig

Controls web search behaviour.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `engine` | `str` | `"duckduckgo"` | Search engine: `"duckduckgo"` or `"gemini"` |
| `n_results` | `int` | `10` | Number of search results to fetch |
| `crawl_depth` | `int` | `0` | Depth to crawl each result page (0 = just the page itself) |
| `same_domain_only` | `bool` | `False` | Stay on the result domain when `crawl_depth > 0` |
| `expand_topic` | `bool` | `True` | Use LLM to expand the query into a rich topic description |
| `gemini_model` | `str` | `"gemini-3-flash-preview"` | Model for Gemini grounded search |

---

## DBConfig

Controls SQLite persistence.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `db_path` | `str` | `"lazycrawler.db"` | Path to SQLite file. `":memory:"` for in-memory (tests) |
| `ttl_hours` | `float` | `24.0` | Cache TTL — pages older than this are considered stale |
| `force_refresh` | `bool` | `False` | Ignore TTL; always re-fetch every URL |
| `enable_fts` | `bool` | `True` | Enable FTS5 full-text search index |
