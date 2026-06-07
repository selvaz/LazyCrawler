# Markdown output & RAG document assembly

LazyCrawler can turn a crawled page into a **single, chunk-ready Markdown
document** in which the narrative text and the *extracted artifacts* (tables,
images, charts) are **recomposed into one coherent whole** — the form you want
for RAG ingestion.

There are three pieces, each building on the previous:

1. **`emit_markdown`** — render each HTML page to clean Markdown.
2. **`markdown_artifact_anchors`** — externalize tables/images as inline anchors
   instead of duplicating them.
3. **`render_for_rag()`** — **reconstruct** the narrative + the externalized
   artifacts into one resolvable document.

---

## 1. Markdown output (`emit_markdown`)

Set `emit_markdown=True` to also render each crawled HTML page to Markdown —
heading hierarchy, lists, tables, and links resolved to absolute URLs. It lands on
`PageResult.markdown` and is persisted alongside the page.

```python
from lazycrawler import WebCrawler, CrawlerConfig

crawler = WebCrawler(CrawlerConfig(max_depth=0, emit_markdown=True))
r = crawler.crawl("https://example.com/article", mode="pure")[0]
print(r.markdown)   # "# Title\n\n- bullet\n\n| col | ... |"
```

- Needs the `markdown` extra (`pip install lazycrawler[markdown]`); without it the
  field degrades to a basic text strip instead of failing.
- PDFs are skipped (no HTML).
- The render is independent of pure/smart — it works in both.

---

## 2. The reconstruction problem

By default, when you enable **both** `emit_markdown` and `extract_artifacts`, a
table or image lives in **two** places: inline in `r.markdown` *and* as a row in
the `artifacts` table. That duplication is wasteful for RAG (you embed the same
table twice) and loses the link between the narrative position and the structured
artifact.

The fix is the best-practice RAG layout — **inline anchors + externalized
content**:

```python
crawler = WebCrawler(
    CrawlerConfig(
        max_depth=0,
        emit_markdown=True,
        extract_artifacts=True,
        markdown_artifact_anchors=True,   # <- the key flag
    ),
    db=db,
)
r = crawler.crawl("https://example.com/report", mode="pure")[0]
```

Now each table/image in the Markdown is replaced by a stable placeholder
`[[artifact:<hash>]]` — **no duplication, position + local context preserved** —
while the heavy/structured content lives in the `artifacts` table:

```text
r.markdown:
# Q3 Results

Revenue grew across all regions.

[[artifact:ab12cd]]

The chart below shows the trend.

[[artifact:9f3e77]]
```

---

## 3. `render_for_rag()` — reconstruct the document

`render_for_rag(page, artifacts=None)` **recomposes** the two representations into
one chunk-ready Markdown document: the narrative with its inline anchors **plus** a
resolvable *Artifacts* appendix that pairs each anchor with its Markdown table,
image reference, or vision summary.

```python
from lazycrawler import render_for_rag

doc = render_for_rag(r)            # from a PageResult (uses r.artifacts)
```

```text
# Q3 Results

Revenue grew across all regions.

[[artifact:ab12cd]]

The chart below shows the trend.

[[artifact:9f3e77]]

---

## Artifacts

### [[artifact:ab12cd]] · table — Revenue by region
| Region | Q3 | YoY |
| --- | --- | --- |
| EMEA | 4.1M | +12% |
| APAC | 3.3M | +18% |

### [[artifact:9f3e77]] · chart — Revenue trend
Steady upward trend across the last 6 quarters.

![Revenue trend](https://example.com/img/trend.png)

*(image 800×400)*

data: [{'label': 'Q1', 'value': 2.8}, {'label': 'Q2', 'value': 3.0}, ...]
```

The anchors in the body line up with the appendix headings, so a model (or a
chunker) can resolve `[[artifact:ab12cd]]` to the full table on demand.

### Reconstructing later from the database

`page` may be a `PageResult` **or** a stored DB row, so you can rebuild the
document long after the crawl, from the cache:

```python
from lazycrawler.http import url_hash

row  = db.get_page(url_hash("https://example.com/report"))
arts = db.get_artifacts(url_hash=row["url_hash"])
doc  = render_for_rag(row, artifacts=arts)
```

`render_for_rag` works whether or not anchoring was enabled at crawl time — when it
was, the body anchors and the appendix line up; when it wasn't, the appendix is
still produced (just without inline anchors).

---

## Why this shape (the multi-vector pattern)

This is the multi-vector RAG pattern made concrete:

- **Embed** the artifact **summary** (or caption) for retrieval — short, semantic.
- **Return** the **full** table/image to the model at generation time — tables kept
  whole, images carried as a reference + text surrogate (caption / vision
  description).

Tables are never split mid-row; images travel as `src_url` + `alt`/caption (+ a
vision summary when [enrichment](artifacts.md#vision-enrichment-smart) is on), so a
text-only retriever still has something meaningful to match.

---

## End-to-end: crawl → assemble → ingest

```python
from lazycrawler import WebCrawler, CrawlerConfig, CrawlerDB, DBConfig, render_for_rag

db = CrawlerDB(DBConfig(db_path="rag.db"))
crawler = WebCrawler(
    CrawlerConfig(
        max_depth=1, max_pages=20,
        emit_markdown=True,
        extract_artifacts=True,
        markdown_artifact_anchors=True,
    ),
    db=db,
)
results = crawler.crawl("https://example.com/docs", mode="pure", session_id="docs")

docs = [render_for_rag(r, artifacts=r.artifacts) for r in results if r.status == "done"]
# -> hand `docs` to your chunker / embedder / vector store
```

The `rag_ingest` **preset** wires exactly this (pure + `emit_markdown` +
`markdown_artifact_anchors` + `extract_artifacts`) for the agent tool path — see
the [Presets guide](presets.md).

---

## See also

- [Artifacts](artifacts.md) — what gets extracted, the `Artifact` model, vision
  enrichment, persistence and retrieval.
- [Presets](presets.md) — the `rag_ingest` preset.
