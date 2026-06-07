# ML mode — models & theory

`ml` mode does "smart-but-zero-token" crawling by stacking **classical IR**,
**static embeddings**, and **unsupervised statistical NLP** — techniques that run
in microseconds-to-milliseconds on CPU with no neural forward pass at request
time (beyond a vector lookup) and no API call. This page explains *what* each
component is, *how* it works, its *efficacy* and its *limitations*, with links to
the reference papers and documentation.

The design bet: for **frontier steering and triage**, these methods recover the
bulk of an LLM's usefulness (relevance ranking, a summary, topics, entities,
sentiment) at a tiny fraction of the cost — while being deterministic and
explainable. The trade-off (a quality ceiling below an LLM) is discussed at the
end.

---

## 1. Static embeddings — Model2Vec / Potion

**Where used:** semantic link scoring (`cos(topic, anchor)`) and the sentence-
similarity graph behind the TextRank summary.

**What it is.** A *static* embedding maps each token to a fixed vector,
independent of context; a text's vector is the (weighted) mean of its token
vectors. **Model2Vec** distills a contextual sentence-transformer into static
vectors: it passes the model's vocabulary through the transformer, reduces
dimensionality with **PCA**, and re-weights by token frequency (**Zipf**
weighting). Inference is then a lookup + average — no transformer forward pass.

**Efficacy.** ~**500× faster** than a MiniLM sentence-transformer on CPU,
~8–30 MB on disk, ≈15k sentences/s, with MTEB scores far above older static
embeddings (GloVe/fastText) and approaching small transformers (the
`potion-retrieval-32M` model used here is retrieval-tuned). It directly addresses
the **vocabulary-mismatch** problem — "renewable energy" matches "solar
deployment" — that pure keyword overlap misses.

**Limitations.** Being static, it has **no context**: word-sense ambiguity
("Apple" the company vs the fruit) is unresolved, and on very short or ambiguous
anchors it is noisier than a contextual encoder. Its quality ceiling is below
contextual transformers; it is a *speed/quality* trade, not a free lunch. We
mitigate by embedding anchor **+ URL tokens + surrounding context**, not the
anchor alone.

**Lineage & references**
- Model2Vec — [GitHub (MinishLab)](https://github.com/MinishLab/model2vec) · [package docs](https://minish.ai/packages/model2vec/introduction/) · ["500× faster on CPU" write-up](https://huggingface.co/blog/Pringled/model2vec)
- Static-embedding training, background — Hugging Face: ["Train 400× faster static embeddings"](https://huggingface.co/blog/static-embeddings)
- Word2Vec — Mikolov et al. 2013, [arXiv:1301.3781](https://arxiv.org/abs/1301.3781)
- GloVe — Pennington et al. 2014, [EMNLP](https://aclanthology.org/D14-1162/)
- fastText (subword) — Bojanowski et al. 2017, [arXiv:1607.04606](https://arxiv.org/abs/1607.04606)
- Sentence-BERT (the distillation source family) — Reimers & Gurevych 2019, [arXiv:1908.10084](https://arxiv.org/abs/1908.10084)

---

## 2. Lexical signal — token overlap, TF-IDF, BM25

**Where used:** the `w_lex` term of the link score (topic ↔ anchor + URL tokens).

**What it is.** A bag-of-words match: how many topic terms appear in the
candidate. The principled relatives are **TF-IDF** (down-weight common terms) and
**BM25** (TF saturation + length normalization), the workhorses of search.

**Efficacy.** Deterministic, explainable, and strong when the page literally uses
the topic's terms. Zero model, zero download.

**Limitations.** The **vocabulary-mismatch** problem (synonyms, paraphrase) — the
exact gap that motivates adding the semantic term — and it is **sparse on short
anchors** (3–5 tokens give weak IDF). LazyCrawler uses a normalized overlap and
lets the semantic term carry meaning; BM25 (`rank-bm25`) is the natural upgrade if
you want a stronger lexical ranker.

**References**
- TF-IDF (term specificity) — Spärck Jones 1972, [paper](https://www.staff.city.ac.uk/~sbrp622/idfpapers/ksj_orig.pdf)
- BM25 / Probabilistic Relevance Framework — Robertson & Zaragoza 2009, [Foundations & Trends in IR](https://www.nowpublishers.com/article/Details/INR-019)

---

## 3. Structural priors — URL & anchor heuristics

**Where used:** the `w_struct` term (URL depth, query-string penalty, anchor
quality) — *topic-independent* priors for "is this a content-bearing link worth
following".

**What it is.** Hand-crafted features that encode crawler folklore: shallow
article-like slugs are usually content; query-heavy or very deep URLs are often
faceted/duplicate; descriptive multi-word anchors beat "click here". This is the
classic **focused-crawling** idea — bias the frontier toward on-topic, high-value
links rather than breadth-first.

**Efficacy.** Cheap, robust priors that complement the semantic/lexical signals,
especially when the topic is empty or anchors are terse.

**Limitations.** Weights are hand-tuned and **site-dependent**; they encode
heuristics, not a learned model. (A future learning-to-rank step could replace
them with weights fit from crawl feedback.)

**References**
- Focused crawling — Chakrabarti, van den Berg, Dom 1999, [paper](https://www.sciencedirect.com/science/article/abs/pii/S1389128699000523)
- Best-first crawler study — Cho, García-Molina, Page 1998, ["Efficient Crawling Through URL Ordering"](https://snap.stanford.edu/class/cs224w-readings/cho98crawling.pdf)

---

## 4. Extractive summarization — TextRank / LexRank

**Where used:** `PageResult.summary` in `content="ml"`.

**What it is.** A graph-based, **unsupervised, extractive** summarizer: build a
graph whose nodes are sentences and whose edge weights are sentence-to-sentence
similarity (LazyCrawler uses **Model2Vec cosine** for the edges), run
**PageRank** over it (power iteration), and return the highest-centrality
sentences **in their original order**. "Extractive" means it *selects* existing
sentences; it never generates text.

**Efficacy.** Picks genuinely salient sentences, is **faithful by construction**
(no hallucination — every word came from the page), language-agnostic, and fast.
Using embedding-based edges (vs raw word overlap) improves on the original
word-overlap TextRank.

**Limitations.** **Extractive ≠ abstractive**: it cannot compress, paraphrase, or
synthesize across sentences the way an LLM does; long sentences are kept whole;
without a redundancy penalty (e.g. **MMR**) near-duplicate sentences can co-occur;
and it depends on a decent sentence splitter.

**References**
- TextRank — Mihalcea & Tarau 2004, [ACL Anthology W04-3252](https://aclanthology.org/W04-3252/)
- LexRank — Erkan & Radev 2004, [JAIR / arXiv:1109.2128](https://arxiv.org/abs/1109.2128)
- PageRank — Page, Brin, Motwani, Winograd 1999, [Stanford InfoLab](http://ilpubs.stanford.edu:8090/422/)
- MMR (redundancy control) — Carbonell & Goldstein 1998, [ACM](https://dl.acm.org/doi/10.1145/290941.291025)

---

## 5. Keyphrase extraction — YAKE

**Where used:** `PageResult.topics` in `content="ml"`.

**What it is.** **YAKE!** (Yet Another Keyword Extractor) is an **unsupervised,
single-document, corpus-free** keyphrase extractor. It scores candidate n-grams
from *local* statistical features — term frequency, casing, position in the text,
sentence dispersion, and term relatedness — with **no training and no
dictionary/thesaurus**, and is language-independent.

**Efficacy.** A strong, fast baseline that works on a single page without any
background corpus — ideal for a crawler that sees one document at a time.

**Limitations.** It surfaces **salient surface phrases**, not semantically
clustered "topics"; it can pick up recurrent boilerplate, and it does no semantic
deduplication of near-synonymous phrases. Embedding-based alternatives
(**KeyBERT**, which could reuse our Model2Vec embedder) or **RAKE** trade speed
for different behavior. (Without the `nlp` extra, LazyCrawler falls back to a
frequency-based extractor.)

**References**
- YAKE! — Campos et al. 2020, *Information Sciences* 509:257–289 — [Semantic Scholar](https://www.semanticscholar.org/paper/YAKE!-Keyword-extraction-from-single-documents-Campos-Mangaravite/9cb32bdd43f64b36cb447ba1307869c5d8bf675c) · [GitHub (LIAAD)](https://github.com/LIAAD/yake)
- RAKE — Rose et al. 2010, [book chapter](https://doi.org/10.1002/9780470689646.ch1)
- KeyBERT — [project](https://maartengr.github.io/KeyBERT/)

---

## 6. Named-entity recognition — spaCy

**Where used:** `PageResult.entities` in `content="ml"`.

**What it is.** spaCy's small English pipeline (`en_core_web_sm`) runs a compact
CNN token-to-vector encoder feeding a **transition-based** NER that tags spans as
`PERSON`, `ORG`, `GPE`, `PRODUCT`, etc. It is a small *supervised* model (trained
once, shipped as weights) — not an LLM, and it does no per-request training.

**Efficacy.** Fast CPU NER with solid accuracy on general English news/web text;
deterministic and offline.

**Limitations.** The **small** model trades accuracy for speed/size (a transformer
NER like `en_core_web_trf` is more accurate but heavier); it suffers under
**domain shift** (finance/biomed/legal) and needs the model downloaded
(`python -m spacy download en_core_web_sm`). Without it, LazyCrawler falls back to
a **regex** proper-noun extractor — weaker, but dependency-free.

**References**
- spaCy NER — [usage docs](https://spacy.io/usage/linguistic-features#named-entities) · [English models](https://spacy.io/models/en)
- Transition-based parsing background — Honnibal & Johnson 2015, [EMNLP](https://aclanthology.org/D15-1162/)

---

## 7. Sentiment — VADER

**Where used:** `PageResult.sentiment` (`negative` / `neutral` / `positive`) in
`content="ml"`.

**What it is.** **VADER** (Valence Aware Dictionary and sEntiment Reasoner) is a
**rule-based, lexicon** model: a human-curated valence dictionary plus five
heuristics — punctuation emphasis (`!!!`), capitalization, degree modifiers
("very"), negation, and contrastive conjunctions ("but…") — producing a
`compound` score that we threshold into three classes. No training, fully
interpretable.

**Efficacy.** Very fast, transparent, and tuned for short / social / overtly-toned
text; a strong zero-cost baseline.

**Limitations.** Bounded by **lexicon coverage**; struggles with **sarcasm/irony**
and **domain-specific** tone (financial or technical prose where "volatile" or
"aggressive" aren't negative); and it is **document-level** (no aspect-/target-
level sentiment). For nuanced tone, `content="smart"` is the right tier.

**References**
- VADER — Hutto & Gilbert 2014, ICWSM — [paper (AAAI)](https://ojs.aaai.org/index.php/ICWSM/article/view/14550) · [GitHub (cjhutto)](https://github.com/cjhutto/vaderSentiment)

---

## Efficacy vs. limits — when `ml` underperforms `smart`

| Axis | `ml` (local) | `smart` (LLM) |
|---|---|---|
| Summary | **extractive** — selects sentences, faithful, no synthesis | **abstractive** — compresses, paraphrases, reasons across the doc |
| Topics | salient surface keyphrases (YAKE) | conceptual themes, normalized |
| Entities | small-model NER / regex | reasoned, disambiguated, typed |
| Sentiment | lexicon (document-level, no irony) | contextual, aspect-aware |
| Link relevance | static-embedding cosine + heuristics | reasoned relevance to the goal |
| Cost / latency | **0 tokens**, ms, CPU | tokens, network latency |
| Determinism | **deterministic, explainable** | stochastic |

**Rule of thumb.** `ml` recovers roughly the *breadth* of `smart` at none of the
token cost, with a lower *depth* ceiling. Use it for **steering the frontier,
triage and monitoring at scale**, then spend LLM tokens only on the few pages that
deserve depth. The two-knob design makes the canonical combo explicit:

```python
crawl(url, links="ml", content="smart", topic="...")
# free semantic frontier for breadth + LLM extraction only on the winners
```

See the [ML Mode guide](ml-mode.md) for the practical API and `MLConfig` knobs.
