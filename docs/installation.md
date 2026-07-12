# Installation

## Requirements

- Python **3.10** or later
- pip

## Install

LazyCrawler is distributed from GitHub (only LazyBridge is on PyPI). Every
install uses a `git+` direct reference pulling the current `main` — append
`@vX.Y.Z` to pin a release tag; add an extra in the brackets as needed.

=== "Pure mode only"

    ```bash
    pip install "lazycrawler @ git+https://github.com/selvaz/LazyCrawler.git"
    ```

    Includes: httpx, beautifulsoup4, trafilatura, pydantic, robotparser.
    No LLM, no API key needed.

=== "With LLM (smart mode)"

    ```bash
    pip install "lazycrawler[smart] @ git+https://github.com/selvaz/LazyCrawler.git"
    ```

    Adds: `lazybridge` (multi-provider LLM abstraction).

=== "Everything"

    ```bash
    pip install "lazycrawler[all] @ git+https://github.com/selvaz/LazyCrawler.git"
    ```

    Adds: smart + pdf + search + js + excel + dates.

## Optional extras

| Extra | Adds | When to use |
|---|---|---|
| `smart` | `lazybridge` (Python ≥3.11) | LLM extraction / link selection (`mode="smart"`) |
| `ml` | `model2vec`, `numpy` | **No-LLM** `mode="ml"`: semantic link scoring + TextRank summary |
| `nlp` | `yake`, `vaderSentiment`, `spacy` | `content="ml"` topics/entities/sentiment (local, no tokens) |
| `pdf` | `pymupdf`, `pypdf`, `pdfplumber` | Crawl and extract PDF files (+ PDF artifacts) |
| `search` | `ddgs` | `WebSearch` with the DuckDuckGo engine |
| `js` | `playwright` | Render JavaScript/SPA sites |
| `markdown` | `markdownify` | `emit_markdown` HTML→Markdown for RAG |
| `image` | `pillow` | Artifact image dimensions / format sniffing |
| `excel` | `openpyxl` | Load URL blacklist from Excel |
| `dates` | `python-dateutil` | Parse `published_iso` from page metadata |
| `async` | `aiohttp` | Async/parallel crawling (see the parallel-crawl guide) |
| `domains` | `tldextract` | Accurate registered-domain extraction for same-site scoping (falls back to a heuristic when absent) |
| `all` | all of the above | Full feature set |

Brave and Tavily search need **no extra** (they use `requests`); just set
`BRAVE_API_KEY` / `TAVILY_API_KEY`.

### After installing `js`

The Playwright browsers must be installed separately:

```bash
playwright install chromium
```

### After installing `nlp`

The spaCy entity model is downloaded separately (else entities fall back to a
regex extractor):

```bash
python -m spacy download en_core_web_sm
```

## API key setup

Smart mode uses LazyBridge, which infers the provider from the model string. Set the API key as an environment variable:

=== "OpenAI"

    ```bash
    # Windows
    set OPENAI_API_KEY=sk-...
    # Linux/macOS
    export OPENAI_API_KEY=sk-...
    ```

=== "Anthropic"

    ```bash
    set ANTHROPIC_API_KEY=sk-ant-...
    ```

=== "Google"

    ```bash
    set GOOGLE_API_KEY=...
    ```

=== "DeepSeek"

    ```bash
    set DEEPSEEK_API_KEY=...
    ```

LazyBridge supports `.env` files — place them in the working directory and they are loaded automatically.

## SSL / Corporate proxy (Avast, Zscaler, etc.)

Some antivirus or proxy tools intercept HTTPS and replace certificates. This causes `SSLCertVerificationError`.

**Quick fix — disable verification:**

```python
from lazycrawler.config import HTTPConfig

http_cfg = HTTPConfig(verify_ssl=False)
crawler = WebCrawler(http_cfg=http_cfg)
```

**Better fix — provide the CA bundle:**

```python
http_cfg = HTTPConfig(ca_bundle="C:/path/to/corporate-ca.pem")
```

Export your proxy CA certificate in PEM format and pass the path to `ca_bundle`.

## Verify the installation

```python
from lazycrawler import WebCrawler

crawler = WebCrawler()
results = crawler.crawl("https://quotes.toscrape.com", mode="pure")
crawler.close()

print(f"Crawled {len(results)} pages")
for r in results[:3]:
    print(f"  {r.status} | {r.url}")
```

Expected output (approx.):
```
Crawled 10 pages
  done | https://quotes.toscrape.com
  done | https://quotes.toscrape.com/page/2/
  done | https://quotes.toscrape.com/page/3/
```
