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

- **semantic** — cosine similarity, via **Model2Vec** static embeddings
  (numpy-only, ~500× faster than a sentence-transformer on CPU), between the
  anchor (+ URL tokens) and *both* the topic **and the current page's content**.
  The page-context term (weight `w_context`) is the focused-crawling intuition
  that links resembling the page you're already on tend to stay on-topic;
- **lexical** — topic↔anchor/URL token overlap (stopwords and 1–2 char fragments
  stripped, so common words neither dilute the topic nor match spuriously);
- **structural** — URL depth / query / anchor-quality priors (topic-independent).

The score is normalized to `[0, 1]`. When the semantic signal is unavailable
(no `ml` extra, or the embedding fails) its weight is redistributed across the
lexical + structural signals, so the score stays on the same scale and a
`min_link_score` gate keeps the same meaning with or without the model installed.
The bounded per-page embedding budget (`max_candidates_to_embed`) is spent on the
candidates a cheap lexical pre-rank already favors, not whichever links happen to
appear first in the HTML.

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

> **On `sentiment`:** VADER is a lexicon/rule model originally tuned for short,
> social-media-style text. It is a useful, free heuristic for opinionated content
> (reviews, op-eds, headlines) but is far less meaningful on neutral expository
> pages (docs, reference, specs). Treat it as a coarse hint, not a calibrated
> measure — set `MLConfig(sentiment=False)` to skip it. For very long documents
> the TextRank candidate pool is capped (`summary_max_sentences`) and **sampled at
> an even stride across the whole document**, so the summary isn't biased toward
> the introduction.

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
pip install "lazycrawler[ml] @ git+https://github.com/selvaz/LazyCrawler.git@v0.15.0"      # link scoring + TextRank summary (model2vec + numpy)
pip install "lazycrawler[nlp] @ git+https://github.com/selvaz/LazyCrawler.git@v0.15.0"     # content: YAKE keyphrases, VADER sentiment, spaCy NER
python -m spacy download en_core_web_sm   # optional: spaCy entity model
```

Without the extra, ML mode still runs — semantic scoring is simply skipped and
the **lexical + structural** signals are used (still topic-aware, and the score is
renormalized so `min_link_score` gates stay meaningful). This degraded path is the
one exercised in CI; the offline test suite includes a benchmark asserting that,
under a fixed page budget, the best-first frontier collects the on-topic links a
plain document-order "first N" pass would miss. The Model2Vec model (~30 MB)
downloads once on first use and is cached; the embedder is loaded once and shared
across all workers.

---

## Configuration (`MLConfig`)

| Field | Default | Description |
|---|---|---|
| `model` | `"minishlab/potion-retrieval-32M"` | Model2Vec static-embedding model |
| `w_sem` / `w_lex` / `w_struct` | `0.55` / `0.20` / `0.25` | score blend weights (renormalized when semantic is unavailable) |
| `w_context` | `0.15` | within the semantic term, weight on current-page similarity vs the topic (0 = topic only) |
| `best_first` | `True` | best-first frontier (else DFS/BFS) |
| `min_link_score` | `0.0` | drop frontier links below this normalized `[0,1]` score |
| `max_candidates_to_embed` | `400` | cap on links embedded per page (spent on the best lexical pre-rank) |
| `summary_sentences` / `summary_max_sentences` | `4` / `200` | TextRank output size / candidate-pool cap (even-stride sampled) |

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
