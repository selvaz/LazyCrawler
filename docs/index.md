# LazyCrawler

**Recursive web crawler with three modes — `pure` (no-LLM), `ml` (local ML, no-LLM, zero tokens), and `smart` (LLM-powered).**

LazyCrawler fetches pages, extracts text and follows links. In **smart** mode an LLM extracts structured information (title, summary, entities, topics, sentiment) and ranks which links to follow; in **ml** mode the *same fields* are produced locally — TextRank summary, YAKE topics, spaCy entities, VADER sentiment — and links are followed **best-first by semantic relevance**, all at **zero token cost**.

## Key features

- **Two independent knobs, three values each**: `content=` and `links=` each take `"pure"` / `"ml"` / `"smart"` (set both at once with `mode=`)
- **Pure mode**: fast, free, no API key — trafilatura text extraction + heuristic link selection
- **ML mode (no-LLM, zero tokens)**: best-first **semantic** link scoring (Model2Vec) + local structured extraction (TextRank/YAKE/spaCy/VADER) ([guide](guides/ml-mode.md))
- **Smart mode**: LLM-powered structured extraction via [LazyBridge](https://core.lazybridge.com) — switch provider/model with one string (`gpt-4o-mini`, `claude-haiku-4-5`, `gemini-flash`, `deepseek-chat`)
- **3-level dedup**: URL+TTL cache → content hash → pure→ml/smart upgrade (requires `CrawlerDB`)
- **Markdown + RAG assembly**: render pages to Markdown and **reconstruct one chunk-ready document** that recomposes the narrative with its extracted artifacts ([`render_for_rag`](guides/markdown-rag.md))
- **Artifacts**: extract tables, images, charts and SVG as structured records — text, bytes, and optional vision-LLM enrichment ([guide](guides/artifacts.md))
- **Crawl presets**: intent-level configs (`quick_lookup`, `deep_research`, `rag_ingest`, …) the agent picks by name ([guide](guides/presets.md))
- **PDF support**: auto-detected, extracted with PyMuPDF / pypdf / pdfplumber (tables + images as artifacts too)
- **JS/SPA rendering**: optional Playwright headless browser
- **Agent integration**: expose crawler as LazyBridge tools for AI agents
- **SQLite persistence**: sessions, pages, artifacts, FTS5 full-text search

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

=== "ML mode (no-LLM)"

    ```python
    # pip install "lazycrawler[ml,nlp] @ git+https://github.com/selvaz/LazyCrawler.git"   — local ML, zero tokens
    from lazycrawler import WebCrawler, CrawlerConfig

    crawler = WebCrawler(CrawlerConfig(max_depth=1, max_pages=20))
    results = crawler.crawl(
        "https://example.com", mode="ml", topic="solid-state batteries"
    )  # best-first semantic frontier + local summary/entities/topics/sentiment
    crawler.close()

    for r in results:
        print(r.url, "|", r.summary, "|", r.topics)
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

| | Pure | ML (no-LLM) | Smart |
|---|---|---|---|
| **Text extraction** | trafilatura + HTML strip | TextRank + YAKE + spaCy + VADER | LLM structured extraction |
| **Link selection** | heuristic (first-N) | best-first **semantic** (Model2Vec) | LLM topic-guided |
| **Output fields** | `url`, `text`, `title` | + `summary`, `entities`, `topics`, `sentiment` | + `summary`, `entities`, `topics`, `sentiment` |
| **LLM required** | No | No | Yes (LazyBridge) |
| **Token cost** | Free | **Free** | Per-page LLM tokens |
| **Speed** | Fast | Fast (CPU) | Slower (LLM latency) |
| **Best for** | Bulk crawl, text corpus | Topic-guided research at zero cost | Deep extraction on key pages |

You can mix the knobs: `content="smart", links="pure"` runs LLM extraction with
heuristic links (cuts cost ~50%); the killer combo is **`links="ml"` for breadth +
`content="smart"` on the winners** — point the crawl at a topic for free, spend
tokens only where they matter.

---

## What's next

- [Installation](installation.md) — install extras, set up API keys, SSL notes
- [Concepts](concepts.md) — depth, dedup, link pipeline, large docs
- [WebCrawler reference](reference/webcrawler.md) — full API with examples
- [Pure mode guide](guides/pure-mode.md) — bulk crawl cookbook
- [Smart mode guide](guides/smart-mode.md) — LLM extraction cookbook
- [Markdown & RAG](guides/markdown-rag.md) — render to Markdown and reconstruct one RAG-ready document from text + artifacts
- [Artifacts](guides/artifacts.md) — tables, images and charts as structured records
- [Crawl presets](guides/presets.md) — pick a crawl config by intent
