# Pure Mode Guide

Pure mode uses **trafilatura** for text extraction and a **heuristic scorer** for link selection. No LLM, no API key, no cost.

Best for: building text corpora, bulk data collection, checking page availability, quick site surveys.

---

## Minimal example

```python
from lazycrawler import WebCrawler

crawler = WebCrawler()
results = crawler.crawl("https://quotes.toscrape.com", mode="pure")
crawler.close()

for r in results:
    print(r.status, r.url)
```

---

## Controlling depth and page count

```python
from lazycrawler import WebCrawler
from lazycrawler.config import CrawlerConfig

crawler = WebCrawler(
    crawler_cfg=CrawlerConfig(
        max_depth=3,  # follow links 3 hops from seed
        max_pages=100,  # stop after 100 pages total
        max_links_per_level=10,  # follow at most 10 links per page
    )
)
results = crawler.crawl("https://example.com", mode="pure")
crawler.close()

done = [r for r in results if r.status == "done"]
print(f"Collected {len(done)} pages out of {len(results)} visited")
```

---

## Domain filtering

```python
from lazycrawler.config import CrawlerConfig

# Default: same domain only
cfg_same = CrawlerConfig(same_domain_only=True)

# Follow links anywhere
cfg_cross = CrawlerConfig(same_domain_only=False, max_depth=1, max_pages=50)

crawler = WebCrawler(crawler_cfg=cfg_cross)
results = crawler.crawl("https://news.ycombinator.com", mode="pure")
crawler.close()
```

!!! note
    `same_domain_only=True` matches the full `netloc`, so `www.example.com` and `example.com` are treated as different domains.

---

## Politeness: delay between requests

```python
from lazycrawler.config import HTTPConfig

http_cfg = HTTPConfig(
    link_delay=2.0,  # wait 2 seconds between page fetches
    max_retries=3,
)
crawler = WebCrawler(http_cfg=http_cfg)
```

The default `link_delay=1.0` is a reasonable politeness setting. Set to `0.0` only on your own servers.

---

## Blacklisting URLs and domains

```python
from lazycrawler.config import CrawlerConfig

cfg = CrawlerConfig(
    blacklist=[
        "https://example.com/login",
        "https://example.com/cart",
        "https://example.com/checkout",
        "ads.example.com",  # whole domain
        "tracking.example.com",  # whole domain
    ]
)
```

### Blacklist from Excel

```python
# pip install "lazycrawler[excel] @ git+https://github.com/selvaz/LazyCrawler.git"
cfg = CrawlerConfig(
    blacklist_excel="blacklist.xlsx",
    blacklist_excel_sheet="Sheet1",  # None = first sheet
    blacklist_excel_column="A",  # None = first column
)
```

---

## Reading results

```python
results = crawler.crawl("https://example.com", mode="pure")

# Filter successful pages
done = [r for r in results if r.status == "done"]

# Quick summary
status_counts = {}
for r in results:
    status_counts[r.status] = status_counts.get(r.status, 0) + 1
print(status_counts)

# Access text
for r in done:
    print(f"\n--- {r.url} ---")
    print(f"Title: {r.title}")
    print(f"Text ({len(r.text or '')} chars): {(r.text or '')[:200]}")
```

---

## Text quality: trafilatura vs fallback

Pure mode tries text extraction in this order:

1. **trafilatura** — best quality, removes navigation/ads/boilerplate. Returns text only if it is at least `HTTPConfig.min_text_chars` (default 50).
2. **Basic HTML strip** — strips all HTML tags, collapses whitespace. Fallback if trafilatura fails or returns too little.

The verbose log line tells you which was used:

```
DEBUG   text: trafilatura -> 3421 chars
# or:
DEBUG   text: basic HTML strip (fallback) -> 987 chars
# or:
DEBUG   text: no extractable content (<min_text_chars from both)
```

---

## Saving results to file

### JSON

```python
import json

results = crawler.crawl("https://example.com", mode="pure")
output = [
    {
        "url": r.url,
        "title": r.title,
        "depth": r.depth,
        "status": r.status,
        "text": r.text,
    }
    for r in results
    if r.status == "done"
]
with open("results.json", "w", encoding="utf-8") as f:
    json.dump(output, f, ensure_ascii=False, indent=2)
```

### CSV

```python
import csv

with open("results.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["url", "title", "depth", "chars"])
    for r in results:
        if r.status == "done":
            w.writerow([r.url, r.title or "", r.depth, len(r.text or "")])
```

### Plain text corpus

```python
with open("corpus.txt", "w", encoding="utf-8") as f:
    for r in results:
        if r.status == "done" and r.text:
            f.write(f"=== {r.url} ===\n")
            f.write(r.text)
            f.write("\n\n")
```

---

## Verbose logging

```python
import logging
from lazycrawler import WebCrawler, set_log_level

set_log_level(logging.DEBUG)

crawler = WebCrawler()
results = crawler.crawl("https://quotes.toscrape.com", mode="pure")
crawler.close()
```

Sample output:

```
INFO  [d0 | p1/20] https://quotes.toscrape.com
DEBUG   fetch: HTTP 200 | html=11234 chars | text=1842 chars
DEBUG   text: trafilatura -> 1842 chars
DEBUG   title: 'Quotes to Scrape'
DEBUG   links: 32 <a> tags | -0 off-domain | -28 excluded | -2 dup -> 2 candidates
DEBUG   content [pure]: 1842 chars (preclean=1842, limit=10000)
DEBUG   candidates: 2 -> -0 visited/blacklisted -> 2 to explore
```

Diagnosing common issues from the log:

| Log line | Meaning | Fix |
|---|---|---|
| `0 <a> tags` | JS-rendered, no static HTML links | `render_js=True` |
| `-N off-domain` | Links cross domains | `same_domain_only=False` |
| `-N excluded` | Exclusion regex too aggressive | Check site URL patterns |
| `no extractable content` | Page is blank or image-only | Normal; `status="no_text"` |

---

## Crawl many seed URLs

```python
seeds = [
    "https://docs.python.org/3/library/asyncio.html",
    "https://docs.python.org/3/library/threading.html",
    "https://docs.python.org/3/library/concurrent.futures.html",
]

crawler = WebCrawler(crawler_cfg=CrawlerConfig(max_depth=1, max_pages=30))
results = crawler.crawl_many(seeds, mode="pure", session_id="python-docs")
crawler.close()

print(f"Total pages: {len(results)}")
```
