# Core Concepts

## Pure / ML / Smart mode

LazyCrawler has two independent knobs: **content extraction** and **link
selection**. Each takes one of **three values** — `"pure"`, `"ml"`, or `"smart"` —
independently, or set both at once with `mode=`.

```
mode="pure"   ≡  content="pure",  links="pure"    # no LLM
mode="ml"     ≡  content="ml",    links="ml"      # local ML, no LLM, zero tokens
mode="smart"  ≡  content="smart", links="smart"   # LLM (via LazyBridge)
```

| | `content="pure"` | `content="ml"` | `content="smart"` |
|---|---|---|---|
| How it works | trafilatura + HTML strip | TextRank + YAKE + spaCy + VADER | LLM structured extraction |
| Output | `text` only | `text`, `summary`, `entities`, `topics`, `sentiment` | same + reasoned/abstractive |
| Token cost | 0 | **0** | ~300–1000 tokens/page |

| | `links="pure"` | `links="ml"` | `links="smart"` |
|---|---|---|---|
| How it works | first-N heuristic | best-first **semantic** (Model2Vec) | LLM picks from candidate list |
| Topic-aware | No | **Yes** (`topic=`) | Yes (`topic=`) |
| Token cost | 0 | **0** | ~200–500 tokens/page |

`ml` is a *smart-but-zero-token* tier: best for breadth/triage and topic-guided
crawling at no API cost; reserve `smart` for the few pages that deserve an LLM's
abstractive summary and reasoned topics. See the [ML Mode guide](guides/ml-mode.md).

**Mixed mode** is the most common real-world pattern:

```python
# Research: smart content, cheap link selection
crawler.crawl(url, content="smart", links="pure")

# Topic-guided traversal: LLM picks links, cheap content
crawler.crawl(url, content="pure", links="smart", topic="climate change")
```

---

## Crawl depth and page cap

Two parameters control how far the crawler goes:

- **`max_depth`** (default `2`): how many link-hops from the seed URL. Depth 0 = seed only, depth 1 = seed + its links, etc.
- **`max_pages`** (default `20`): hard upper limit on total pages collected, regardless of depth.

```
Seed (depth 0)
├── Page A (depth 1)
│   ├── Page C (depth 2)  ← max_depth=2 stops here
│   └── Page D (depth 2)
└── Page B (depth 1)
    └── Page E (depth 2)
```

The crawler stops as soon as either limit is hit.

### Sequential DFS vs parallel BFS

- **`max_workers=1`** (default): **sequential DFS** — follows links one at a time, depth-first. Polite, predictable.
- **`max_workers=N`** (N > 1): **parallel BFS** — a thread pool processes links from each level concurrently. Much faster but applies more load to the target server.

---

## Domain filtering

By default, `same_domain_only=True` restricts the crawler to the **same domain** as the seed URL. Domain matching uses the full `netloc` (host), so `www.example.com` and `example.com` are treated as different domains.

```python
# Only follows links within docs.example.com
crawler.crawl("https://docs.example.com", same_domain_only=True)

# Follows any link found
crawler.crawl("https://example.com", same_domain_only=False)
```

---

## Link pipeline

For each crawled page, candidate links are built through this pipeline:

```
HTML source
    │
    ▼
BeautifulSoup — extract all <a href="..."> tags
    │  n_raw links
    │
    ▼
Domain filter — remove off-domain links (if same_domain_only=True)
    │  -n_offdom
    │
    ▼
Exclusion regex — remove /tag/, /author/, /login, /about, /contact, ...
    │  -n_excluded
    │
    ▼
Visited filter — remove already-crawled or blacklisted URLs
    │  -n_dup
    │
    ▼
Candidate pool
    │
    ▼
Link selection (pure: score-ranked top N  |  smart: LLM picks from list)
    │
    ▼
Next URLs to visit
```

The verbose DEBUG output shows counts at each step:

```
links: 55 <a> tags | -2 off-domain | -51 excluded | -0 dup -> 2 candidates
```

**Diagnosing "no links found":**

- `0 <a> tags` → JavaScript-rendered site; use `render_js=True`
- `-N off-domain` → cross-domain links; use `same_domain_only=False`
- `-N excluded` → exclusion regex too aggressive; site uses filtered URL patterns

---

## 3-level deduplication

Dedup requires a `CrawlerDB` instance passed to `WebCrawler`. Without a DB, no dedup occurs.

**Level 1 — URL + TTL cache**

Before fetching, check if the URL was crawled within `ttl_hours` (default: 24h). If fresh, return the cached result immediately — no HTTP request.

**Level 2 — Content hash**

After fetching, hash the raw text. If the same hash exists in the DB (e.g., a redirect to a page already crawled under a different URL), skip LLM processing.

**Level 3 — Pure → Smart upgrade**

If a page was previously crawled in pure mode and is now requested in smart mode, LazyCrawler can re-process the cached text through the LLM without re-fetching.

```
DB hit (URL + TTL)  →  return cached result  (no HTTP, no LLM)
DB miss             →  fetch page
  content_hash hit  →  skip LLM, reuse existing extraction
  content_hash miss →  extract (pure or smart)
```

---

## Large document handling

When a page's text exceeds `large_doc_threshold` (default: 20,000 chars), smart mode uses **map-reduce**:

1. Split text into chunks of `large_doc_chunk_chars` (default: 12,000 chars), up to `large_doc_max_chunks` chunks (default: 12)
2. Summarize each chunk independently (optionally with a cheaper `large_doc_model`)
3. Merge the partial summaries into a final synthesis

This avoids context-window limits and controls token cost on large pages (Wikipedia articles, long reports, etc.).

Pure mode always truncates to `max_chars_pure` (default: 10,000 chars).

---

## Verbose logging

Enable DEBUG logging to see every step of every page:

```python
import logging
from lazycrawler import set_log_level

set_log_level(logging.DEBUG)
```

Sample output for one page:

```
INFO  [d1 | p2/10] https://quotes.toscrape.com/page/2/
DEBUG   fetch: HTTP 200 | html=11234 chars | text=1842 chars
DEBUG   text: trafilatura -> 1842 chars
DEBUG   title: 'Quotes to Scrape'
DEBUG   links: 32 <a> tags | -0 off-domain | -28 excluded | -2 dup -> 2 candidates
DEBUG   content [pure]: 1842 chars (preclean=1842, limit=10000)
DEBUG   candidates: 2 -> -0 visited/blacklisted -> 2 to explore
```

In smart mode, additional lines appear:

```
DEBUG   content [smart]: LLM extraction (preclean=1842 chars)...
DEBUG   content [smart]: title='...' | summary=120 chars | 3 entities | 4 topics | sentiment=neutral
DEBUG   LLM selector: 5 candidates -> indices [1, 3] -> 2 valid
```
