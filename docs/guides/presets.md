# Crawl Presets

Presets let an agent (or you) pick a crawl configuration **by intent** instead of
wiring raw knobs. A preset bundles content/link modes, depth, page/result caps,
branching, artifact extraction, Markdown output and search recency — plus a
coarse `cost` hint — behind a single name like `quick_lookup` or `deep_research`.

This keeps the agent-facing tool schema simple: the LLM reasons about *what it
wants to do*, never about cost knobs. It calls `list_presets()` to discover the
catalog, then passes `preset="…"` to `web_search` / `web_crawl`.

```python
from lazycrawler.tools import CrawlerTools

tools = CrawlerTools(db=db)
agent = Agent(engine=engine, tools=tools.as_tools())
# the model: list_presets() -> web_search("solid-state batteries", preset="deep_research")
```

---

## Built-in catalog

| Preset | Intent | content / links | depth | links/page | max_pages | Extra | Recency | Cost |
|--------|--------|-----------------|:---:|:---:|:---:|-------|:---:|:---:|
| `quick_lookup` | Fast factual check / grab a page's text | pure / pure | 0 | — | 5 | — | — | minimal |
| `deep_research` | Thorough multi-source research | smart / smart | 1 | 25 | 20 | topic-driven | — | high |
| `news_scan` | Current events / monitoring | smart / pure | 0 | — | 15 | sentiment + date | last week | medium |
| `extract_data` | Pull tables/images off a page | pure / pure | 0 | — | 5 | artifacts (table/image/chart) | — | low |
| `rag_ingest` | Load pages into a RAG pipeline | pure / pure | 0 | — | 8 | Markdown + artifact anchors | — | low |
| `research_ml` | Zero-token research (local ML) | ml / ml | 1 | 25 | 20 | best-first frontier | — | minimal |
| `news_scan_ml` | Zero-token news monitoring | ml / pure | 0 | — | 15 | sentiment+entities (local) | last week | minimal |
| `topic_explore_ml` | Map a topic via semantic frontier | pure / ml | 2 | 20 | 30 | best-first, deep · gate 0.35 | — | low |
| `triage_ml` | Cheap relevance triage / shortlist sources | pure / ml | 1 | 10 | 10 | strong-only links · gate 0.5 | — | minimal |
| `rag_ingest_ml` | RAG ingestion + local enrichment | ml / pure | 0 | — | 8 | Markdown anchors + ML summary/topics | — | low |
| `hybrid_research` | Semantic frontier (free) + LLM content | smart / ml | 1 | 25 | 20 | LLM only on reached pages | — | medium |

Notes:

- The link-following presets (`depth > 0`) are `deep_research`, `research_ml`,
  `topic_explore_ml`, `triage_ml` and `hybrid_research`; for them the **branching
  factor** (`links/page`, i.e. `max_links_per_level`) matters. The single-page
  presets (`depth 0`) ignore it.
- **gate** is `min_link_score` — the `ml`-mode relevance threshold. A frontier
  link scoring below it is pruned, so `topic_explore_ml` (0.35) explores broadly
  while `triage_ml` (0.5) keeps only clearly on-topic links. It only applies to
  `links="ml"` presets and is passed per call as an `MLConfig` override (the
  shared config is untouched).
- A preset applies **per call** — the shared `CrawlerConfig` / `MLConfig` is
  never mutated, so concurrent tool calls stay isolated.
- An explicit `depth` / `max_results` on the tool call **overrides** the preset.

---

## Using presets from the agent

`list_presets()` returns the catalog so the model can choose:

```json
{
  "presets": [
    {"name": "quick_lookup", "intent": "Fast, cheap lookup…", "cost": "minimal",
     "content": "pure", "follows_links": false, "link_mode": "pure", "depth": 0,
     "links_per_page": null, "min_link_score": null, "artifacts": false,
     "markdown": false, "recency": null},
    {"name": "topic_explore_ml", "intent": "Map a topic via semantic frontier…",
     "cost": "low", "content": "pure", "follows_links": true, "link_mode": "ml",
     "depth": 2, "links_per_page": 20, "min_link_score": 0.35, "artifacts": false,
     "markdown": false, "recency": null}
  ]
}
```

Then on a tool call:

```python
tools.web_search("EU AI Act enforcement 2026", preset="news_scan")
tools.web_crawl("https://example.com/report", preset="extract_data")
tools.web_crawl("https://example.com/article", preset="rag_ingest")
```

An unknown preset name returns an error JSON listing the valid names (so the
model can self-correct):

```json
{"error": "unknown preset 'foo'", "available": ["quick_lookup", "deep_research", …],
 "hint": "call list_presets() to see each preset's intent and cost"}
```

---

## Custom presets

Add or override presets at construction. A key matching a built-in name replaces
it; new keys extend the catalog:

```python
from lazycrawler import CrawlPreset
from lazycrawler.tools import CrawlerTools

tools = CrawlerTools(
    db=db,
    presets={
        # new intent
        "headlines": CrawlPreset(
            name="headlines",
            description="Front-page scan: smart extraction, last 24h, no link-following.",
            content="smart", links="pure", max_depth=0, max_results=20,
            timelimit="d", cost="medium",
        ),
        # retune a built-in (same key overrides it)
        "deep_research": CrawlPreset(
            name="deep_research",
            description="Deeper research: follow more links from each source.",
            content="smart", links="smart", max_depth=2, max_pages=40,
            max_links_per_level=40, cost="high",
        ),
    },
)
```

`list_presets()` now includes `headlines` and the retuned `deep_research`.

### CrawlPreset fields

| Field | Type | Default | Notes |
|---|---|---|---|
| `name` | `str` | required | Stable identifier passed as `preset=` (kept in sync with the catalog key) |
| `description` | `str` | required | One-line intent shown to the LLM (when to pick it) |
| `cost` | `str` | `"low"` | Coarse hint: `"minimal"` / `"low"` / `"medium"` / `"high"` |
| `content` | `str` | `"pure"` | `"pure"` (no LLM) or `"smart"` (LLM extraction) |
| `links` | `str` | `"pure"` | `"pure"` (heuristic) or `"smart"` (LLM ranking) |
| `max_depth` | `int` | `0` | Crawl depth (0 = only the seed/result URLs) |
| `max_pages` | `int` | `5` | Hard cap on extracted pages for the run |
| `max_links_per_level` | `int \| None` | `None` | Branching factor (links followed **per page**). `None` = keep the crawler default |
| `max_results` | `int` | `8` | Default search results (`web_search` only) |
| `extract_artifacts` | `bool` | `False` | Extract tables/images/charts as artifacts |
| `artifact_types` | `tuple` | all | Which artifact types to collect |
| `emit_markdown` | `bool` | `False` | Render each HTML page to Markdown |
| `markdown_artifact_anchors` | `bool` | `False` | Externalize artifacts as `[[artifact:<hash>]]` anchors |
| `timelimit` | `str \| None` | `None` | Search recency: `"d"` / `"w"` / `"m"` / `"y"` (`web_search` only) |

---

## How it works (per-call overrides)

A preset maps to a per-call `CrawlerConfig` override applied on top of the
crawler's base config — `WebCrawler.crawl(..., overrides=...)` builds a per-run
effective config without mutating `self.cfg`. Content/links/depth and search
recency are passed alongside (they are not `CrawlerConfig` fields). This is why
presets are concurrency-safe: two overlapping tool calls each get their own
effective config.

You can use the same mechanism directly, without presets:

```python
crawler.crawl(
    "https://example.com/report",
    content="pure",
    max_depth=0,
    overrides={"extract_artifacts": True, "max_pages": 3},
)
```
