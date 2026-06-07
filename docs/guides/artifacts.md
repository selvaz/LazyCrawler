# Artifacts (tables, images, charts)

Beyond clean text, LazyCrawler can extract a page's **non-textual content** as
structured `Artifact` records — **tables, images, figures, charts and inline
SVG** — each kept whole with its caption / surrounding context and provenance.
Artifacts work on **HTML** and (with the `pdf` extra) on **PDFs**, are persisted
in a dedicated `artifacts` table, and can be recomposed with the page text into a
single RAG-ready document.

Everything here is **off by default** — pure mode pays nothing.

```python
from lazycrawler import WebCrawler, CrawlerConfig, CrawlerDB, DBConfig

db = CrawlerDB(DBConfig(db_path="crawl.db"))
crawler = WebCrawler(CrawlerConfig(max_depth=0, extract_artifacts=True), db=db)
r = crawler.crawl("https://example.com/report", mode="pure")[0]

for a in r.artifacts:
    print(a.artifact_type, "—", a.caption or a.alt or a.src_url)
    if a.artifact_type == "table":
        print(a.content)   # Markdown table; a.data = structured rows
```

---

## The layers (each opt-in)

Artifact handling is built as independent layers, so you pay only for what you
turn on:

| Layer | Flag | Cost | What it adds |
|-------|------|------|--------------|
| **Reference** | `extract_artifacts=True` | cheap (regex/BeautifulSoup, no network, no LLM) | tables → Markdown + rows; images/charts → URL + alt + caption + context; SVG markup |
| **Bytes** | `download_artifact_bytes=True` | one HTTP GET per image | downloads image bytes through the crawler's HTTP client (honors SSL + SSRF guard), stores `sha256` + the blob (size-capped) |
| **Vision** | `enrich_artifacts=True` (+ `content="smart"`) | one vision-LLM call per artifact (capped) | image captions, chart trend/data extraction, table summaries via LazyBridge |

```python
CrawlerConfig(extract_artifacts=True)                              # reference only
CrawlerConfig(extract_artifacts=True, download_artifact_bytes=True) # + sha256 + bytes
CrawlerConfig(extract_artifacts=True, enrich_artifacts=True)        # + vision LLM (smart)
```

Pure extraction is deterministic and offline; the *bytes* and *vision* layers are
where network/LLM cost lives.

---

## What each type captures

| Type | Extraction |
|------|------------|
| **table** | Markdown (`content`) **plus** structured rows (`data`), header↔value preserved. Layout/`role="presentation"`/nested tables are skipped |
| **image** | absolute `src_url` + `alt` + `<figcaption>` caption + ±N chars of surrounding context (`artifact_context_chars`) |
| **chart** | images / SVG that *look like* charts (alt/class/markup heuristics, or an SVG with ≥5 drawing primitives) |
| **figure** | `<figure>` blocks (chart candidates) |
| **svg** | inline `<svg>` markup captured (capped length) |

Tiny/spacer/logo/tracking/icon images are filtered out via `min_image_dim` and a
noise pattern; `same_domain_images=True` keeps only images on the page's own host.
Select which types to collect with `artifact_types`.

---

## The `Artifact` model

```python
from lazycrawler import Artifact
```

| Field | Type | Meaning |
|-------|------|---------|
| `artifact_type` | `str` | `"table"` / `"image"` / `"figure"` / `"chart"` / `"svg"` |
| `position` | `int` | order of appearance on the page |
| `src_url` | `str \| None` | absolute image URL |
| `alt` | `str \| None` | `alt` text |
| `caption` | `str \| None` | `<figcaption>` / `<caption>` |
| `context` | `str \| None` | surrounding text when there is no caption |
| `content` | `str \| None` | text representation (Markdown table / SVG markup) |
| `content_format` | `str \| None` | `"markdown"` / `"svg"` / `"url"` / `"bytes"` |
| `data` | `Any` | structured rows (tables) / chart data points (vision) |
| `summary` | `str \| None` | vision/LLM enrichment |
| `mime`, `width`, `height` | | image metadata (Pillow sniff when bytes downloaded) |
| `bytes_hash` | `str \| None` | `sha256` of downloaded image bytes |
| `size_bytes` | `int \| None` | byte size |
| `content_hash` | `str \| None` | per-page dedup key + the anchor join key |
| `meta` | `dict` | extra (`rows`/`cols`, SVG `primitives`, PDF `page`, …) |
| `blob` | `bytes \| None` | raw image bytes — **excluded from serialization** (DB reads it directly, never leaks into agent JSON) |

---

## PDF artifacts

With the `pdf` extra installed, PDFs also yield artifacts:

- **tables** via `pdfplumber` (each as a Markdown table + rows, captioned with the
  page number),
- **embedded images** via PyMuPDF (extracted as `blob` bytes with `mime`/dims).

```python
# pip install lazycrawler[pdf]
crawler = WebCrawler(CrawlerConfig(max_depth=0, extract_artifacts=True), db=db)
r = crawler.crawl("https://example.com/whitepaper.pdf", mode="pure")[0]
tables = [a for a in r.artifacts if a.artifact_type == "table"]
```

---

## Vision enrichment (smart)

With `content="smart"` and `enrich_artifacts=True`, a vision-capable model (via
LazyBridge) captions images, reads chart trends/data points, and summarizes
tables. It is capped by `max_artifacts_to_enrich` and can use a dedicated model:

```python
from lazycrawler.config import LLMConfig

crawler = WebCrawler(
    CrawlerConfig(extract_artifacts=True, enrich_artifacts=True,
                  max_artifacts_to_enrich=8),
    llm_cfg=LLMConfig(model="gpt-4o-mini", vision_model="gpt-4o"),
    db=db,
)
r = crawler.crawl("https://example.com/report", content="smart")[0]
for a in r.artifacts:
    print(a.artifact_type, a.summary)        # vision caption / table summary
    if a.artifact_type == "chart":
        print(a.data)                        # extracted data points
```

Enrichment uses `blob` when present, else `src_url`. An image the model decides is
a chart is reclassified `image → chart`.

---

## Persistence & retrieval

Artifacts are stored in a dedicated **`artifacts`** table (FK to `pages`), deduped
per `(url_hash, content_hash)`. Retrieve them from the DB or the agent tool:

```python
from lazycrawler.http import url_hash

# by page
arts = db.get_artifacts(url_hash=url_hash("https://example.com/report"))
# whole session
arts = db.get_artifacts(session_id="my_session")
# filter by type, include raw bytes
imgs = db.get_artifacts(url_hash=uh, artifact_type="image", include_blob=True)
```

`db.get_artifacts(...)` drops the `blob` by default (`include_blob=True` to keep
it) and deserializes `data` / `meta`.

**Agent tool** — when a DB is attached, `CrawlerTools` exposes `get_artifacts`:

```python
get_artifacts(url, artifact_type="")   # "table" | "image" | "figure" | "chart" | "svg" | ""
# -> {"url", "found", "artifacts": [{type, caption, summary, src_url, content,
#                                    data, mime, width, height, stored_bytes}]}
```

---

## Multimodal RAG: anchors + `render_for_rag()`

> This is summarized here; the full pipeline (Markdown output → anchors →
> reconstructed document) has its own page: **[Markdown & RAG](markdown-rag.md)**.

By default the Markdown (`emit_markdown`) and the artifacts are **two independent
representations** — tables/images stay inline in the Markdown *and* are copied into
the `artifacts` table. The best-practice RAG layout is instead **inline anchors +
externalized content**: set `markdown_artifact_anchors=True` and each table/image
in the Markdown is replaced by a stable placeholder `[[artifact:<hash>]]` (no
duplication, position + local context preserved), while the heavy/structured
content lives in `artifacts`.

```python
crawler = WebCrawler(
    CrawlerConfig(extract_artifacts=True, emit_markdown=True,
                  markdown_artifact_anchors=True),
    db=db,
)
r = crawler.crawl("https://example.com/report", mode="pure")[0]
# r.markdown -> "...intro [[artifact:ab12cd]] outro..."  (table externalized)
```

`render_for_rag(page, artifacts=None)` recomposes the two into one chunk-ready
document: the narrative with its inline anchors **plus** a resolvable *Artifacts*
appendix pairing each anchor with its Markdown table / image reference / vision
summary.

```python
from lazycrawler import render_for_rag

doc = render_for_rag(r)                       # from a PageResult
# or from the DB later:
row  = db.get_page(url_hash("https://example.com/report"))
doc  = render_for_rag(row, artifacts=db.get_artifacts(url_hash=row["url_hash"]))
```

This is the multi-vector pattern: embed the artifact **summary** for retrieval,
return the **full** table/image to the model — tables kept whole, images carried as
a reference + text surrogate (caption / vision description).

---

## Configuration reference

| Parameter | Default | Description |
|---|---|---|
| `extract_artifacts` | `False` | Master switch — extract artifacts at all |
| `artifact_types` | `("table","image","figure","svg","chart")` | Which types to collect |
| `download_artifact_bytes` | `False` | Download image/chart bytes → `sha256` + blob in DB |
| `max_artifact_bytes` | `5_000_000` | Max image size stored as a blob (larger → keep hash/metadata only) |
| `min_image_dim` | `48` | Drop images whose declared width/height is below this (filters icons) |
| `artifact_context_chars` | `200` | Chars of surrounding text captured for images lacking a caption |
| `max_artifacts_per_page` | `100` | Hard cap on artifacts per page |
| `same_domain_images` | `False` | Keep only images on the page's own domain |
| `enrich_artifacts` | `False` | Vision-LLM enrichment (requires `content="smart"`) |
| `max_artifacts_to_enrich` | `8` | Per-page cap on LLM-enriched artifacts |
| `markdown_artifact_anchors` | `False` | Externalize artifacts as `[[artifact:<hash>]]` anchors in the Markdown |

These can also be set **per call** through a preset's `crawl_overrides()` or
directly via `crawler.crawl(..., overrides={"extract_artifacts": True})`. The
built-in `extract_data` preset turns on table/image extraction; `rag_ingest`
turns on artifacts + Markdown anchors. See the [Presets guide](presets.md).

---

## Dependencies & graceful degradation

| Feature | Needs | Without it |
|---|---|---|
| HTML extraction | `beautifulsoup4` (core) | artifact extraction disabled (logged) |
| Image dimensions / MIME sniff | `pillow` — `pip install lazycrawler[image]` | dims omitted, MIME from magic bytes only |
| PDF tables / images | `pip install lazycrawler[pdf]` | PDFs yield no artifacts |
| Vision enrichment | `lazybridge` + a vision model | `enrich_artifacts` has no effect in pure mode |

Nothing hard-fails: a missing optional dependency degrades the relevant layer and
logs at DEBUG/WARNING.
