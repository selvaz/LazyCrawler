# Smart Mode Guide

Smart mode uses an LLM (via LazyBridge) to extract structured information from each page: title, summary, entities, topics, and sentiment. It can also use the LLM to intelligently select which links to follow.

---

## Setup

Install the `smart` extra and set your API key:

```bash
pip install "lazycrawler[smart] @ git+https://github.com/selvaz/LazyCrawler.git@v0.15.0"
export OPENAI_API_KEY=sk-...   # or ANTHROPIC_API_KEY, GOOGLE_API_KEY, etc.
```

LazyBridge infers the provider from the model string — no additional config needed.

---

## Model selection

```python
from lazycrawler.config import LLMConfig

# OpenAI (default)
llm_cfg = LLMConfig(model="gpt-4o-mini")

# Anthropic
llm_cfg = LLMConfig(model="claude-haiku-4-5")

# Google
llm_cfg = LLMConfig(model="gemini-3-flash-preview")

# DeepSeek
llm_cfg = LLMConfig(model="deepseek-chat")
```

| Provider | Env var | Best value model | Best quality model |
|---|---|---|---|
| OpenAI | `OPENAI_API_KEY` | `gpt-4o-mini` | `gpt-4o` |
| Anthropic | `ANTHROPIC_API_KEY` | `claude-haiku-4-5` | `claude-sonnet-4-6` |
| Google | `GOOGLE_API_KEY` | `gemini-3-flash-preview` | `gemini-2-pro` |
| DeepSeek | `DEEPSEEK_API_KEY` | `deepseek-chat` | — |

---

## Full smart mode

```python
from lazycrawler import WebCrawler
from lazycrawler.config import LLMConfig, CrawlerConfig

llm_cfg = LLMConfig(model="gpt-4o-mini")
crawler = WebCrawler(
    crawler_cfg=CrawlerConfig(max_depth=2, max_pages=15),
    llm_cfg=llm_cfg,
)
results = crawler.crawl(
    "https://techcrunch.com",
    mode="smart",
    topic="AI and machine learning startups",
)
crawler.close()

for r in results:
    if r.status == "done":
        print(f"\n{r.url}")
        print(f"  Title:     {r.title}")
        print(f"  Summary:   {r.summary}")
        print(f"  Entities:  {r.entities[:5]}")
        print(f"  Topics:    {r.topics[:5]}")
        print(f"  Sentiment: {r.sentiment}")
```

---

## content="smart" only — cheaper

LLM extracts structured content, but links are selected heuristically. Saves ~40–50% of LLM calls.

```python
results = crawler.crawl(
    "https://arxiv.org",
    content="smart",  # LLM extraction
    links="pure",     # heuristic link selection
    topic="reinforcement learning from human feedback",
)
```

This is the **recommended pattern for most research tasks** — you get the rich structured output without paying for LLM link selection.

---

## links="smart" only — topic-guided traversal

The LLM picks which links to follow based on the topic, but content extraction remains pure (no LLM). Useful when you want to navigate intelligently but don't need structured output.

```python
results = crawler.crawl(
    "https://wikipedia.org/wiki/Machine_learning",
    content="pure",    # plain text extraction
    links="smart",     # LLM picks next pages
    topic="deep learning architectures and transformers",
)

for r in results:
    print(f"[d{r.depth}] {r.url}")
```

---

## Topic parameter

The `topic` parameter is passed to the LLM to guide link selection and content context. Be specific:

```python
# Vague — less effective
results = crawler.crawl(url, mode="smart", topic="technology")

# Specific — much better
results = crawler.crawl(url, mode="smart", topic="electric vehicle battery technology and charging infrastructure")
```

---

## PageResult smart fields

```python
for r in results:
    if r.summary:     # only set in smart mode
        print(r.summary)
    if r.entities:
        for e in r.entities:
            print(f"  entity: {e}")
    if r.topics:
        for t in r.topics:
            print(f"  topic: {t}")
    if r.sentiment:
        print(f"  sentiment: {r.sentiment}")
```

---

## Large document handling

When a page has more than `large_doc_threshold` (default: 20,000 chars), LazyCrawler automatically applies **map-reduce**:

1. Splits the text into chunks of ~12,000 chars
2. Summarizes each chunk independently
3. Merges the partial summaries into a final synthesis

```python
from lazycrawler.config import CrawlerConfig, LLMConfig

llm_cfg = LLMConfig(
    model="claude-sonnet-4-6",
    large_doc_model="claude-haiku-4-5",  # cheaper model for chunked summaries
)
cfg = CrawlerConfig(
    large_doc_threshold=15_000,  # lower threshold for map-reduce
    large_doc_chunk_chars=10_000,
    large_doc_max_chunks=8,
)

crawler = WebCrawler(crawler_cfg=cfg, llm_cfg=llm_cfg)
results = crawler.crawl("https://en.wikipedia.org/wiki/Artificial_intelligence", mode="smart")
```

The verbose log shows:

```
INFO  large document (45231 chars) - map-reduce summarization (4 chunks ~10000 chars ea)
DEBUG   large-doc: summarizing chunk 1/4 (10000 chars)...
DEBUG   large-doc: chunk 1 -> 412 chars output
...
DEBUG   large-doc: merging 4 partials (1634 chars) -> final synthesis...
DEBUG   large-doc: final synthesis -> 891 chars
```

---

## Cost estimation

Approximate token usage per page in smart mode:

| Operation | Tokens (approx.) |
|---|---|
| Content extraction (short page, ~3k chars) | 400–600 |
| Content extraction (long page, ~10k chars) | 1000–1500 |
| Large doc map-reduce (4 chunks) | 2000–4000 |
| Link selection (20 candidates) | 300–500 |

For 20 pages with `content="smart"` and `links="pure"` using `gpt-4o-mini` at $0.15/M tokens:

- Typical cost: **$0.01–$0.05 per crawl**

For high-quality models like `claude-sonnet-4-6`: ~10× more expensive.

---

## Smart mode with DB (avoid re-processing)

```python
from lazycrawler import WebCrawler, CrawlerDB
from lazycrawler.config import DBConfig, LLMConfig

db = CrawlerDB(DBConfig(db_path="smart.db", ttl_hours=72.0))
crawler = WebCrawler(llm_cfg=LLMConfig(model="gpt-4o-mini"), db=db)

# First run: fetches + LLM extraction
results = crawler.crawl("https://example.com", mode="smart")

# Second run within 72 hours: returns cached results, no LLM cost
results2 = crawler.crawl("https://example.com", mode="smart")
print(f"Cache hits: {sum(1 for r in results2 if r.from_cache)}")
```

---

## Enabling verbose output

```python
import logging
from lazycrawler import set_log_level

set_log_level(logging.DEBUG)
```

Smart mode debug output:

```
INFO  [d0 | p1/15] https://techcrunch.com
DEBUG   fetch: HTTP 200 | html=45231 chars | text=8432 chars
DEBUG   text: trafilatura -> 8432 chars
DEBUG   title: 'TechCrunch'
DEBUG   links: 89 <a> tags | -12 off-domain | -61 excluded | -3 dup -> 13 candidates
DEBUG   content [smart]: LLM extraction (preclean=8432 chars)...
DEBUG   content [smart]: title='TechCrunch' | summary=187 chars | 8 entities | 5 topics | sentiment=neutral
DEBUG   LLM selector: 13 candidates -> indices [1, 3, 7] -> 3 valid
```
