# PageResult

Pydantic model returned by every crawl operation. One `PageResult` per page visited.

```python
from lazycrawler import WebCrawler

results: list[PageResult] = crawler.crawl("https://example.com")
```

---

## Fields

| Field | Type | Mode | Description |
|---|---|---|---|
| `url` | `str` | always | Final URL of the page (after redirects) |
| `url_hash` | `str` | always | SHA-256 of `url` — used as DB primary key |
| `status` | `str` | always | Result status (see [Status values](#status-values)) |
| `mode` | `"pure"` or `"smart"` | always | Extraction mode that was used |
| `title` | `str \| None` | always | Page title (from `<title>` tag or LLM) |
| `text` | `str \| None` | always | Cleaned main text content |
| `summary` | `str \| None` | smart only | 1–3 sentence LLM summary |
| `entities` | `list[str]` | smart only | People, orgs, places, products |
| `topics` | `list[str]` | smart only | Main topics/themes of the page |
| `sentiment` | `str \| None` | smart only | `"negative"`, `"neutral"`, or `"positive"` |
| `notes` | `str \| None` | smart only | Reserved for research tags; usually empty |
| `data` | `dict \| None` | smart + schema | Structured data from a custom Pydantic schema |
| `published_iso` | `str \| None` | always | Publication date (ISO 8601) if found in metadata |
| `is_pdf` | `bool` | always | `True` if the page was a PDF file |
| `depth` | `int` | always | Crawl depth from the seed URL (0 = seed) |
| `source_url` | `str \| None` | always | URL of the page that linked to this one |
| `error` | `str \| None` | always | Error message if `status != "done"` |
| `from_cache` | `bool` | always | `True` if returned from the DB cache (no HTTP fetch) |

---

## Status values

| Status | Meaning |
|---|---|
| `"done"` | Page fetched and processed successfully |
| `"fetch_error"` | HTTP error, connection timeout, or network failure |
| `"no_text"` | Page fetched but no extractable text (blank page, pure JS, image) |
| `"llm_error"` | Smart mode LLM call failed (API error, timeout) |
| `"robots_blocked"` | URL disallowed by `robots.txt` |
| `"blacklisted"` | URL matched the blacklist |

---

## Working with results

### Filter by status

```python
results = crawler.crawl("https://example.com")

done = [r for r in results if r.status == "done"]
errors = [r for r in results if r.status not in ("done",)]
no_text = [r for r in results if r.status == "no_text"]

print(f"Done: {len(done)}, Errors: {len(errors)}, No text: {len(no_text)}")
```

### Access text content

```python
for r in results:
    if r.status == "done" and r.text:
        word_count = len(r.text.split())
        print(f"{r.url}: {word_count} words")
```

### Access smart fields

```python
for r in results:
    if r.summary:  # smart mode
        print(f"\n=== {r.title} ===")
        print(r.summary)
        print(f"Entities:  {', '.join(r.entities)}")
        print(f"Topics:    {', '.join(r.topics)}")
        print(f"Sentiment: {r.sentiment}")
```

### Export to JSON

```python
import json

data = [r.model_dump() for r in results if r.status == "done"]
with open("results.json", "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
```

### Export to CSV

```python
import csv

with open("results.csv", "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=["url", "title", "depth", "status", "text"])
    writer.writeheader()
    for r in results:
        writer.writerow({
            "url": r.url,
            "title": r.title or "",
            "depth": r.depth,
            "status": r.status,
            "text": (r.text or "")[:500],
        })
```

### Custom schema — accessing data

```python
from pydantic import BaseModel, Field

class Product(BaseModel):
    name: str = Field(description="Product name")
    price: str = Field(description="Price with currency symbol")
    description: str = Field(description="Short product description")

results = crawler.crawl("https://shop.example.com", content="smart", schema=Product)

for r in results:
    if r.data:
        p = Product(**r.data)
        print(f"{p.name} — {p.price}")
```

### Check cache hits

```python
cache_hits = sum(1 for r in results if r.from_cache)
live_fetches = sum(1 for r in results if not r.from_cache)
print(f"Cache: {cache_hits}, Live: {live_fetches}")
```

### Group by depth

```python
from collections import defaultdict

by_depth = defaultdict(list)
for r in results:
    by_depth[r.depth].append(r)

for depth, pages in sorted(by_depth.items()):
    done = sum(1 for p in pages if p.status == "done")
    print(f"Depth {depth}: {len(pages)} pages, {done} done")
```
