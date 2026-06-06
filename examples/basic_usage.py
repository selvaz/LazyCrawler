# -*- coding: utf-8 -*-
"""
LazyCrawler usage examples.

Run the cells in Spyder (# %%) or: python examples/basic_usage.py
Note: in environments with SSL inspection (Avast/proxy) set verify_ssl=False
      or ca_bundle in the HTTPConfig.
"""

# %% Setup — ecosystem path + .env (for smart mode)
import os
import sys
from pathlib import Path

ROOT = Path(r"D:\serious_tests\ecosystemv0.9.1")
if (ROOT / "LazyBridge").exists() and str(ROOT / "LazyBridge") not in sys.path:
    sys.path.insert(0, str(ROOT / "LazyBridge"))
env = ROOT / ".env"
if env.exists():
    for line in env.read_text().splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from lazycrawler import (
    CrawlerConfig,
    CrawlerDB,
    DBConfig,
    HTTPConfig,
    LLMConfig,
    SearchConfig,
    WebCrawler,
    WebSearch,
)

HTTP = HTTPConfig(link_delay=0.5, verify_ssl=False)  # verify_ssl: see README


# %% 1. PURE mode — no LLM, no cost (context-manager form auto-closes resources)
with WebCrawler(CrawlerConfig(max_depth=0, max_pages=3), HTTP) as crawler:
    results = crawler.crawl("https://en.wikipedia.org/wiki/Web_crawler", mode="pure")
for r in results:
    print(f"[{r.status}] {r.title}  ({len(r.text or '')} chars)")


# %% 2. SMART mode + DB — structured extraction + persistence
db = CrawlerDB(DBConfig(db_path="example_crawl.db", ttl_hours=24))
crawler = WebCrawler(
    CrawlerConfig(max_depth=1, max_pages=8),
    HTTP,
    llm_cfg=LLMConfig(model="gpt-4o-mini"),  # switch provider = change the string
    db=db,
)
results = crawler.crawl(
    "https://en.wikipedia.org/wiki/James_Webb_Space_Telescope",
    mode="smart",
    topic="space telescopes, astronomy, exoplanets",
    session_id="demo_smart",
)
crawler.close()
for r in results:
    if r.status == "done":
        print(f"\n{r.title}")
        print(f"  summary : {(r.summary or '')[:120]}")
        print(f"  entities: {r.entities}")
        print(f"  topics  : {r.topics}")

# query the DB
print("\n— DB query —")
for p in db.search_text("telescope"):
    print(" ", p["title"])
print("stats:", db.stats())
db.close()


# %% 3. WEB SEARCH — crawler seeded from DuckDuckGo
search = WebSearch(
    SearchConfig(engine="duckduckgo", n_results=6, crawl_depth=0),
    http_cfg=HTTP,
)
out = search.run("electric vehicle battery breakthroughs 2026", mode="pure")
print(f"\n{out['pages_found']} pages found for: {out['query']}")
for r in out["results"]:
    print(f"  [{r.status}] {r.url[:80]}")


# %% 4. INDEPENDENT LLM KNOBS — content vs links
# Use the LLM to pick relevant links, but keep page content cheap (no summary):
crawler = WebCrawler(
    CrawlerConfig(max_depth=1, max_pages=6, max_links_per_level=3),
    HTTP,
    llm_cfg=LLMConfig(model="gpt-4o-mini"),
)
results = crawler.crawl(
    "https://en.wikipedia.org/wiki/Artificial_intelligence",
    content="pure",
    links="smart",  # LLM only for link selection
    topic="machine learning, neural networks",
)
crawler.close()
print(
    f"\ncontent=pure/links=smart -> {len(results)} pages, "
    f"summaries: {sum(r.summary is not None for r in results)}"
)

# Inverse: LLM summary on a single page, no link selection (depth 0):
crawler = WebCrawler(
    CrawlerConfig(max_depth=0, max_pages=1), HTTP, llm_cfg=LLMConfig(model="gpt-4o-mini")
)
results = crawler.crawl(
    "https://en.wikipedia.org/wiki/Photosynthesis", content="smart", links="pure"
)
crawler.close()
print(f"content=smart/links=pure -> summary: {(results[0].summary or '')[:100]}")


# %% 5. NATIVE PARALLEL MODE — bounded thread pool
crawler = WebCrawler(
    CrawlerConfig(max_depth=1, max_pages=30, max_workers=8),  # 8 concurrent workers
    HTTP,
)
results = crawler.crawl("https://en.wikipedia.org/wiki/Climate_change", mode="pure")
crawler.close()
print(f"\nparallel -> {sum(r.status == 'done' for r in results)} pages")


# %% 6. CUSTOM OUTPUT SCHEMA — extract arbitrary fields
from pydantic import BaseModel, Field


class Article(BaseModel):
    headline: str = Field(default="", description="the main headline")
    author: str = Field(default="", description="author if present")
    key_points: list[str] = Field(default_factory=list, description="3-5 takeaways")


crawler = WebCrawler(
    CrawlerConfig(max_depth=0, max_pages=1), HTTP, llm_cfg=LLMConfig(model="gpt-4o-mini")
)
results = crawler.crawl("https://en.wikipedia.org/wiki/CRISPR", content="smart", schema=Article)
crawler.close()
print(f"\ncustom schema -> {results[0].data}")


# %% 7. JAVASCRIPT RENDERING — for SPAs (requires playwright)
# crawler = WebCrawler(
#     CrawlerConfig(max_depth=0, max_pages=1),
#     HTTPConfig(render_js=True, verify_ssl=False),
# )
# results = crawler.crawl("https://example-spa.com/", mode="pure")
# crawler.close()


# %% 8. AS A LAZYBRIDGE TOOL — LazyCrawler is the tool, you build the agent
from lazybridge import Agent, LLMEngine

from lazycrawler import CrawlerTools

db = CrawlerDB(DBConfig(db_path="research.db"))
crawler_tools = CrawlerTools(
    db=db,
    crawler_cfg=CrawlerConfig(max_depth=1, max_pages=8, respect_robots=True),
    http_cfg=HTTP,
    llm_cfg=LLMConfig(model="gpt-4o-mini"),
    content="smart",  # the agent gets summaries + sentiment per page
    links="pure",
)
agent = Agent(engine=LLMEngine("gpt-4o-mini"), tools=crawler_tools.as_tools())
answer = agent("Find 2-3 recent sources on perovskite solar cells and summarize them.")
print(answer.text())
crawler_tools.close()


# %% 9. SENTIMENT in smart structured output
crawler = WebCrawler(
    CrawlerConfig(max_depth=0, max_pages=1), HTTP, llm_cfg=LLMConfig(model="gpt-4o-mini")
)
r = crawler.crawl("https://en.wikipedia.org/wiki/Renewable_energy", content="smart")[0]
crawler.close()
print(f"sentiment={r.sentiment}  topics={r.topics}  notes={r.notes!r}")


# %% 10. MARKDOWN output — for RAG ingestion (pip install lazycrawler[markdown])
with WebCrawler(CrawlerConfig(max_depth=0, max_pages=1, emit_markdown=True), HTTP) as crawler:
    r = crawler.crawl("https://en.wikipedia.org/wiki/Retrieval-augmented_generation", mode="pure")[
        0
    ]
print(f"\nmarkdown ({len(r.markdown or '')} chars):\n{(r.markdown or '')[:300]}")


# %% 11. ARTIFACTS — tables, images, charts as structured records
db = CrawlerDB(DBConfig(db_path="artifacts_demo.db"))
with WebCrawler(
    CrawlerConfig(
        max_depth=0,
        max_pages=1,
        extract_artifacts=True,
        download_artifact_bytes=True,  # also fetch image bytes (pip install lazycrawler[image])
    ),
    HTTP,
    db=db,
) as crawler:
    r = crawler.crawl("https://en.wikipedia.org/wiki/Solar_power", mode="pure")[0]
print(f"\n{len(r.artifacts)} artifacts:")
for a in r.artifacts[:10]:
    label = a.caption or a.alt or a.src_url or a.content_format
    print(f"  [{a.artifact_type}] {str(label)[:70]}")
    if a.artifact_type == "table" and a.content:
        print("   " + a.content.splitlines()[0][:80])
# reachable later from the DB (or the agent get_artifacts(url) tool)
print("tables in DB:", len(db.get_artifacts(session_id=None, artifact_type="table")))
db.close()


# %% 12. RAG-ready rendering — Markdown anchors + render_for_rag()
from lazycrawler import render_for_rag

with WebCrawler(
    CrawlerConfig(
        max_depth=0,
        max_pages=1,
        emit_markdown=True,
        extract_artifacts=True,
        markdown_artifact_anchors=True,  # tables/images -> [[artifact:<hash>]] in the markdown
    ),
    HTTP,
) as crawler:
    r = crawler.crawl("https://en.wikipedia.org/wiki/Photovoltaics", mode="pure")[0]
print("\nmarkdown has anchors:", "[[artifact:" in (r.markdown or ""))
doc = render_for_rag(r)  # narrative + inline anchors + resolvable Artifacts appendix
print(f"RAG document: {len(doc)} chars; artifacts appendix:", "## Artifacts" in doc)
