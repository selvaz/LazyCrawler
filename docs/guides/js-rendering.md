# JavaScript Rendering

Single-page applications (SPAs) and heavily JavaScript-driven sites render their content client-side. A regular HTTP request receives empty or skeleton HTML — no content, no links.

LazyCrawler optionally uses **Playwright** (headless Chromium) to render these sites.

---

## The problem

```python
# Without JS rendering: gets skeleton HTML, 0 links
crawler = WebCrawler()
results = crawler.crawl("https://react-spa.example.com", mode="pure")

# Verbose output:
# DEBUG   text: trafilatura returned 0 chars (<min_text_chars) -> trying basic strip
# DEBUG   text: no extractable content (<min_text_chars chars from both)
# DEBUG   links: 0 <a> tags | -0 off-domain | -0 excluded | -0 dup -> 0 candidates
```

The tell-tale signs: `0 <a> tags`, `no extractable content`.

---

## Setup

```bash
pip install "lazycrawler[js] @ git+https://github.com/selvaz/LazyCrawler.git"
playwright install chromium
```

---

## Enabling render_js

```python
from lazycrawler import WebCrawler
from lazycrawler.config import HTTPConfig

http_cfg = HTTPConfig(render_js=True)
crawler = WebCrawler(http_cfg=http_cfg)
results = crawler.crawl("https://react-spa.example.com", mode="pure")
crawler.close()
```

When `render_js=True`, LazyCrawler:

1. Launches headless Chromium (once per `WebCrawler` instance, reused across pages)
2. Navigates to the URL in a browser context
3. Waits for `browser_wait_until` event
4. Extracts the fully-rendered HTML
5. Falls back to standard requests if Playwright fails

---

## browser_wait_until

Controls when Playwright considers the page "ready":

| Value | When it fires | Use for |
|---|---|---|
| `"domcontentloaded"` (default) | DOM parsed | Fast sites; most SPAs |
| `"load"` | All resources loaded | Sites with lazy-loaded content |
| `"networkidle"` | No network requests for 500ms | Heavy AJAX sites |

```python
http_cfg = HTTPConfig(
    render_js=True,
    browser_wait_until="networkidle",  # wait for all AJAX to finish
    browser_timeout_ms=45000,           # longer timeout for slow sites
)
```

---

## browser_timeout_ms

Default: 30,000ms (30s). Increase for slow-loading sites:

```python
http_cfg = HTTPConfig(
    render_js=True,
    browser_timeout_ms=60000,  # 60 seconds
)
```

---

## browser_headless

Set to `False` to watch the browser during debugging:

```python
http_cfg = HTTPConfig(
    render_js=True,
    browser_headless=False,  # opens a visible browser window
)
```

!!! note
    `browser_headless=False` requires a display. On a headless server, use `True`.

---

## Performance

JS rendering is significantly slower than plain HTTP:

- Browser startup: ~1–2 seconds (done once per `WebCrawler` instance)
- Per-page overhead: +0.5–3 seconds for Playwright vs ~0.1s for requests

Recommendations:

- Use `max_pages` to cap the crawl
- Use `max_depth=1` or `2` to limit scope
- Cache results with `CrawlerDB` to avoid re-rendering

```python
from lazycrawler import WebCrawler, CrawlerDB
from lazycrawler.config import CrawlerConfig, HTTPConfig, DBConfig

db = CrawlerDB(DBConfig(db_path="spa.db", ttl_hours=48.0))
crawler = WebCrawler(
    crawler_cfg=CrawlerConfig(max_depth=1, max_pages=20),
    http_cfg=HTTPConfig(render_js=True, link_delay=2.0),
    db=db,
)
results = crawler.crawl("https://spa.example.com")
crawler.close()
db.close()
```

---

## Fallback behaviour

If Playwright is installed but fails for a specific page (timeout, crash), LazyCrawler falls back to a standard HTTP request for that page. The crawl continues.

If Playwright is not installed and `render_js=True`, an `ImportError` is raised with instructions.
