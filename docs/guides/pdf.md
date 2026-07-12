# PDF Extraction

LazyCrawler automatically detects and extracts text from PDF files encountered during a crawl — no special configuration needed.

---

## Setup

```bash
pip install "lazycrawler[pdf] @ git+https://github.com/selvaz/LazyCrawler.git"
```

Installs: `pymupdf` (primary), `pypdf` (fallback), `pdfplumber` (tables).

---

## Auto-detection

A URL is treated as a PDF if:

- The URL path ends with `.pdf` (e.g., `https://example.com/report.pdf`)
- The HTTP response body starts with `%PDF-` (regardless of URL)

The verbose log shows:

```
INFO  [d1 | p3/20] https://example.com/annual-report.pdf
DEBUG   detected as PDF
DEBUG   fetch: HTTP 200 | html=0 chars | text=14532 chars
DEBUG   text: trafilatura -> 0 chars (<200) -> trying basic strip
DEBUG   text: basic HTML strip (fallback) -> 14532 chars
```

---

## No configuration needed

PDF extraction is automatic:

```python
from lazycrawler import WebCrawler

crawler = WebCrawler()
results = crawler.crawl("https://example.com/papers/", mode="pure")
crawler.close()

pdfs = [r for r in results if r.is_pdf]
print(f"Found {len(pdfs)} PDFs")
for r in pdfs:
    print(f"  {r.url}: {len(r.text or '')} chars")
```

---

## Extraction pipeline

1. **PyMuPDF** (`pymupdf`) — best quality, fastest. Extracts text with layout preservation.
2. **pypdf** — fallback if PyMuPDF fails or is not installed.
3. **pdfplumber** — used for table extraction. Tables are embedded as TSV text within the main text.

---

## PageResult for PDFs

| Field | Value |
|---|---|
| `is_pdf` | `True` |
| `text` | Extracted text (may be large) |
| `title` | From PDF metadata, or derived from filename/URL |
| `published_iso` | From PDF metadata if available |
| `summary` | Smart mode only |
| `entities` | Smart mode only |

PDFs do **not** yield candidate links — no `<a>` tags to extract from a PDF.

---

## pdf_timeout

Large PDFs can take a long time to download. Increase the timeout:

```python
from lazycrawler.config import HTTPConfig

http_cfg = HTTPConfig(pdf_timeout=120)  # 120 seconds (default: 60)
crawler = WebCrawler(http_cfg=http_cfg)
```

---

## Smart mode on PDFs

PDFs often contain large bodies of text. Smart mode with large-doc handling works well:

```python
from lazycrawler import WebCrawler
from lazycrawler.config import LLMConfig, CrawlerConfig

crawler = WebCrawler(
    crawler_cfg=CrawlerConfig(
        large_doc_threshold=15_000,  # trigger map-reduce for large PDFs
        large_doc_chunk_chars=10_000,
        large_doc_max_chunks=10,
    ),
    llm_cfg=LLMConfig(model="gpt-4o-mini"),
)
results = crawler.crawl("https://example.com/whitepaper.pdf", mode="smart")
crawler.close()

for r in results:
    if r.is_pdf and r.summary:
        print(f"PDF summary: {r.summary}")
        print(f"Key entities: {r.entities}")
```

---

## Crawl a site that links to PDFs

```python
crawler = WebCrawler(
    crawler_cfg=CrawlerConfig(max_depth=2, max_pages=30)
)
# LazyCrawler follows links normally; when a link points to a PDF, it extracts it
results = crawler.crawl("https://research.example.com/publications/")
crawler.close()

for r in results:
    if r.is_pdf:
        print(f"PDF: {r.url}")
        print(f"  {len(r.text or '')} chars extracted")
```

---

## Limitations

- **Scanned/image PDFs**: contain no extractable text (images of pages). These return `status="no_text"`. OCR is not included.
- **Password-protected PDFs**: fail with `status="fetch_error"`.
- **Very large PDFs** (100+ MB): may exceed `pdf_timeout`. Increase it or use `max_pages` to limit scope.
