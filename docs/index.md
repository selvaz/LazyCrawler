# LazyCrawler

**Recursive web crawler with pure (no-LLM) and smart (LLM-powered) modes.**

LazyCrawler fetches pages, extracts text, follows links, and — in smart mode — uses an LLM to extract structured information (title, summary, entities, topics, sentiment) and intelligently select which links to follow next.

## Key features

- **Two independent knobs**: `content=` (how text is extracted) and `links=` (how next links are chosen)
- **Pure mode**: fast, free, no API key — trafilatura text extraction + heuristic link selection
- **Smart mode**: LLM-powered structured extraction via [LazyBridge](https://core.lazybridge.com) — switch provider/model with one string (`gpt-4o-mini`, `claude-haiku-4-5`, `gemini-flash`, `deepseek-chat`)
- **3-level dedup**: URL+TTL cache → content hash → pure→smart upgrade (requires `CrawlerDB`)
- **PDF support**: auto-detected, extracted with PyMuPDF / pypdf / pdfplumber
- **JS/SPA rendering**: optional Playwright headless browser
- **Agent integration**: expose crawler as LazyBridge tools for AI agents
- **SQLite persistence**: sessions, pages, FTS5 full-text search

---

## Quick start

=== "Pure mode"

    ```python
    from lazycrawler import WebCrawler

    crawler = WebCrawler()
    results = crawler.crawl("https://example.com", mode="pure")
    crawler.close()

    for r in results:
        print(r.url, r.status, len(r.text or ""), "chars")
    ```

=== "Smart mode"

    ```python
    from lazycrawler import WebCrawler
    from lazycrawler.config import LLMConfig

    llm_cfg = LLMConfig(model="gpt-4o-mini")
    crawler = WebCrawler(llm_cfg=llm_cfg)
    results = crawler.crawl("https://example.com", mode="smart")
    crawler.close()

    for r in results:
        print(r.url)
        print("  summary:", r.summary)
        print("  entities:", r.entities)
        print("  topics:", r.topics)
    ```

=== "Web search"

    ```python
    from lazycrawler import WebSearch

    search = WebSearch()
    result = search.run("python web scraping best practices", mode="pure")
    search.close()

    print(f"Found {result['pages_found']} pages")
    for r in result["results"]:
        print(r.url, r.title)
    ```

---

## Modes at a glance

| | Pure | Smart |
|---|---|---|
| **Text extraction** | trafilatura + HTML strip | LLM structured extraction |
| **Link selection** | heuristic (score-ranked) | LLM topic-guided |
| **Output fields** | `url`, `text`, `title` | + `summary`, `entities`, `topics`, `sentiment` |
| **LLM required** | No | Yes (LazyBridge) |
| **Cost** | Free | Per-page LLM tokens |
| **Speed** | Fast | Slower (LLM latency) |
| **Best for** | Bulk crawl, text corpus | Research, structured data |

You can mix: `content="smart", links="pure"` runs LLM extraction but uses heuristic link selection (cuts cost by ~50%).

---

## What's next

- [Installation](installation.md) — install extras, set up API keys, SSL notes
- [Concepts](concepts.md) — depth, dedup, link pipeline, large docs
- [WebCrawler reference](reference/webcrawler.md) — full API with examples
- [Pure mode guide](guides/pure-mode.md) — bulk crawl cookbook
- [Smart mode guide](guides/smart-mode.md) — LLM extraction cookbook
