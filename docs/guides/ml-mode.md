# ML mode — smart, without the LLM

`ml` is a **third value** for the content/link knobs, alongside `pure` and
`smart`. It does intelligent crawling with **local machine-learning and
statistics — zero LLM tokens, zero API calls**:

> For the theory behind the models (Model2Vec, TextRank, YAKE, spaCy, VADER) —
> how they work, their efficacy and limits, with paper links — see
> **[ML Models & Theory](ml-models.md)**.

```python
crawl(url, links="ml",  topic="...")   # best-first frontier scored by relevance
crawl(url, content="ml")               # structured extraction without an LLM
crawl(url, mode="ml")                  # both
```

It sits in the same two-knob design as `smart`, so you can mix tiers — e.g. use
`links="ml"` for the breadth of the crawl and `content="smart"` only on the few
winners.

Both knobs are implemented: `links="ml"` (relevance scoring + best-first
frontier) and `content="ml"` (structured extraction with local ML / statistics).
Near-duplicate detection and relevance-gated early-stop are the remaining phase.

---

## Intelligent link scoring (`links="ml"`)

Instead of "first N" (pure) or an LLM ranking (smart), `links="ml"` scores every
candidate link against the crawl `topic` and follows the best ones first. The
score blends three signals:

- **semantic** — cosine similarity between the topic and the anchor (+ URL
  tokens), via **Model2Vec** static embeddings (numpy-only, ~500× faster than a
  sentence-transformer on CPU, microseconds per link);
- **lexical** — topic↔anchor/URL token overlap;
- **structural** — URL depth / query / anchor-quality priors (topic-independent).

```python
from lazycrawler import WebCrawler, CrawlerConfig, MLConfig

crawler = WebCrawler(
    CrawlerConfig(max_depth=2, max_pages=30),
    ml_cfg=MLConfig(model="minishlab/potion-retrieval-32M", w_sem=0.55),
)
results = crawler.crawl("https://example.com/", links="ml", topic="solid-state batteries")
```

### Best-first frontier (sequential **and** parallel)

With `links="ml"` the crawler uses a **best-first** frontier: a score-ordered
global queue, processed in waves of `max_workers` — the W globally
highest-scoring links at a time, then re-prioritized. Workers are pure functions
(URL → scored children) and the driver alone owns the queue, so it is
thread-safe by construction:

- `max_workers=1` → pure best-first;
- `max_workers>1` → parallel best-first (the W best at a time).

Set `MLConfig(best_first=False)` to keep DFS/BFS traversal with ML scoring only
applied to the per-page top-N.

---

## Local content extraction (`content="ml"`)

`content="ml"` fills the same structured fields as `smart` — but with local ML
and statistics instead of an LLM, so it costs **no tokens**:

| Field | Technique | Dependency |
|-------|-----------|------------|
| `summary` | extractive **TextRank** over static sentence embeddings (reuses the Model2Vec embedder) | `[ml]` (else lead sentences) |
| `topics` | **YAKE** statistical keyphrases | `[nlp]` (else frequency fallback) |
| `entities` | **spaCy** NER | `[nlp]` + a model (else regex fallback) |
| `sentiment` | **VADER** (lexicon + rules) | `[nlp]` (else `"neutral"`) |

```python
r = WebCrawler(ml_cfg=MLConfig(summary_sentences=4, keyphrase_topk=8)).crawl(
    "https://example.com/article", content="ml"
)[0]
print(r.summary, r.topics, r.entities, r.sentiment)   # filled, zero tokens
```

Tune via `MLConfig(summary_sentences=…, keyphrase_topk=…, sentiment=…, use_spacy_ner=…)`.
Everything degrades gracefully — a missing optional dep drops that field to its
pure-python fallback, never an error.

## Install

```bash
pip install lazycrawler[ml]      # link scoring + TextRank summary (model2vec + numpy)
pip install lazycrawler[nlp]     # content: YAKE keyphrases, VADER sentiment, spaCy NER
python -m spacy download en_core_web_sm   # optional: spaCy entity model
```

Without the extra, ML mode still runs — semantic scoring is simply skipped and
the **lexical + structural** signals are used (still topic-aware, still far
better than "first N"). The Model2Vec model (~30 MB) downloads once on first use
and is cached; the embedder is loaded once and shared across all workers.

---

## Configuration (`MLConfig`)

| Field | Default | Description |
|---|---|---|
| `model` | `"minishlab/potion-retrieval-32M"` | Model2Vec static-embedding model |
| `w_sem` / `w_lex` / `w_struct` | `0.55` / `0.20` / `0.25` | score blend weights |
| `best_first` | `True` | best-first frontier (else DFS/BFS) |
| `min_link_score` | `0.0` | drop frontier links below this score |
| `max_candidates_to_embed` | `400` | cap on links embedded per page |

---

## When to use which mode

| | `pure` | `ml` | `smart` |
|---|---|---|---|
| Token cost | none | **none** | per-page LLM |
| Link selection | first-N | **semantic best-first** | LLM ranking |
| Content fields | text | **summary/entities/topics/sentiment** (local ML) | summary/entities/topics/sentiment (LLM) |
| Best for | bulk crawl | **targeted research at zero token cost** | deep extraction on key pages |

The killer pattern the two knobs enable: **`links="ml"` for breadth + `content="smart"`
on the winners** — point the crawl at the topic for free, spend tokens only where
they matter.
