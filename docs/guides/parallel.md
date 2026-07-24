# Parallel Crawling

By default, LazyCrawler uses a single thread (sequential DFS). Setting `max_workers > 1` enables a thread pool for parallel page fetching.

---

## Sequential vs Parallel

| | Sequential (`max_workers=1`) | Parallel (`max_workers=N`) |
|---|---|---|
| Traversal | Depth-first (DFS) | Breadth-first (BFS) |
| Speed | Slower | Faster |
| Server load | Low | Higher |
| `link_delay` | Between every fetch | Between fetches per thread |
| Thread safety | Single-threaded | Shared visited set (thread-safe) |

---

## Basic parallel crawl

```python
from lazycrawler import WebCrawler
from lazycrawler.config import CrawlerConfig, HTTPConfig

crawler = WebCrawler(
    crawler_cfg=CrawlerConfig(
        max_workers=4,  # 4 concurrent fetchers
        max_depth=2,
        max_pages=60,
        max_links_per_level=15,
    ),
    http_cfg=HTTPConfig(
        link_delay=1.0,  # each thread waits 1s between its own fetches
    ),
)
results = crawler.crawl("https://example.com", mode="pure")
crawler.close()

print(f"Fetched {len(results)} pages")
```

---

## Choosing max_workers

| Site type | Recommended `max_workers` |
|---|---|
| Personal/test server | 8–16 |
| Small business site | 2–4 |
| Large news/wiki site | 2–4 with `link_delay >= 1.0` |
| Site with rate limiting | 1–2 |
| robots.txt with `Crawl-delay` | Respect it; use 1 |

!!! warning
    Aggressive parallel crawling can trigger rate limiting, IP bans, or overwhelm small servers. Always set a reasonable `link_delay`.

---

## Politeness in parallel mode

Each worker thread sleeps for `link_delay` seconds between its own requests. With 4 workers and `link_delay=1.0`, you get up to 4 requests/second.

To be conservative:

```python
CrawlerConfig(max_workers=2)
HTTPConfig(link_delay=2.0)  # 2 workers × 0.5 req/s each = 1 req/s
```

---

## Thread safety

The visited URL set, blacklist, and page counter are all protected by locks and are safe to use with any number of workers. HTTP clients are created per-thread to avoid connection sharing issues.

When using a `CrawlerDB`, database writes are serialized with a lock — multiple workers safely write to the same SQLite file.

---

## Parallel with DB

```python
from lazycrawler import WebCrawler, CrawlerDB
from lazycrawler.config import CrawlerConfig, HTTPConfig, DBConfig

db = CrawlerDB(DBConfig(db_path="parallel.db"))
crawler = WebCrawler(
    crawler_cfg=CrawlerConfig(max_workers=4, max_pages=100),
    http_cfg=HTTPConfig(link_delay=1.0),
    db=db,
)
results = crawler.crawl("https://example.com", session_id="parallel-run")
crawler.close()
db.close()
```

---

## Monitoring progress

Enable INFO logging to see each page as it's collected:

```python
import logging
from lazycrawler import set_log_level

set_log_level(logging.INFO)
```

Each page logs: `[d{depth} | p{n}/{max}] {url}` — the `p{n}` counter is shared across all workers.

---

## Parallel + crawl_many

`crawl_many()` is useful when you have multiple independent seed URLs and want them all processed within a single page budget:

```python
seeds = [
    "https://site-a.com",
    "https://site-b.com",
    "https://site-c.com",
]

crawler = WebCrawler(crawler_cfg=CrawlerConfig(max_workers=3, max_pages=90, max_depth=1))
results = crawler.crawl_many(seeds, mode="pure")
crawler.close()

print(f"Total: {len(results)} pages across {len(seeds)} seeds")
```

!!! tip
    `crawl_many` with `max_workers=1` still processes seeds sequentially within a shared page budget. Use `max_workers > 1` to parallelize across seeds.

---

## Async parallel (`AsyncWebCrawler`) — `pure` + `ml`

For I/O-bound crawls, `AsyncWebCrawler` fetches over aiohttp with
`max_workers`-bounded concurrency. It supports both `pure` and `ml` modes
(`smart`/LLM stays on the synchronous `WebCrawler`) and reuses the **exact** same
post-fetch pipeline as the sync crawler, so you get the same content extraction,
semantic best-first link selection, artifacts and DB persistence — the CPU-bound
ML work runs in a thread executor so it never blocks the event loop.

```python
import asyncio
from lazycrawler import CrawlerConfig, HTTPConfig, MLConfig
from lazycrawler.async_crawler import AsyncWebCrawler


async def main():
    cfg = CrawlerConfig(max_depth=2, max_pages=50, max_workers=8)
    async with AsyncWebCrawler(cfg, HTTPConfig(), ml_cfg=MLConfig()) as crawler:
        results = await crawler.crawl(
            "https://example.com/", mode="ml", topic="solid-state batteries"
        )
    return results


asyncio.run(main())
```

- `links="ml"` (or `mode="ml"`) uses a globally score-ordered **best-first**
  frontier, expanded `max_workers` pages at a time.
- `content="ml"` fills `summary` / `topics` / `entities` / `sentiment` locally.
- Pass a `db=CrawlerDB(...)` to persist sessions/pages/edges/artifacts.

Install the extras you need: `pip install "lazycrawler[async,ml,nlp] @ git+https://github.com/selvaz/LazyCrawler.git"`.
