# Economic calendar enrichment — raw, unported

**Do not merge this branch.** These nine files are copied verbatim out of a
session temp directory, which was their only copy. They are here so that a
cleaned temp directory cannot delete them, and for no other reason yet.

They are the enrichment half of the economic calendar. The collection half is in
market-data-hub PR #59; the two were split because market-data-hub declares
`requires-python = ">=3.9"` and tests on 3.9, while LazyBridge requires >=3.11.
This repository already handles that: `smart = ["lazybridge>=0.9; python_version
>= '3.11'"]`, with lazybridge imported inside functions rather than at module
level.

| file | lines | what it is |
|---|---|---|
| `agente_release.py` | 321 | the agent, its reviewer, the formatter and the fallback |
| `fonti_locali.py` | 175 | `read_daily_digest`, `search_collected_articles` |
| `domini_bloccati.py` + `.txt` | 58 + 35 domains | sources that refuse automated traffic |
| `arricchisci_giornata.py` | 201 | the T1/T2 day job |
| `sintesi.py` | 129 | executive summary |
| `render_report.py` | 285 | HTML |
| `report_giornata.py` | 147 | the day's report |
| `run_week.py` | 89 | orchestrator |

## What has to change before any of this is usable

Found while porting the collection half; the same defects are here.

**Four absolute `sys.path.insert` and one hardcoded directory.**
`agente_release.py`, `report_giornata.py`, `run_week.py` and `sintesi.py` insert
`C:/Users/Administrator/Documents/GitHub/...` at import time, and
`fonti_locali.py` hardcodes `NEWS_DIR`. They have to become arguments.

**`WebSearch()` is built bare**, at `agente_release.py:114`, so nothing it
downloads is persisted. The fix is not the one in the design note: `WebSearch`
takes `db`, not a `DBConfig`. Introspected from the installed package:

    WebSearch(search_cfg=None, crawler_cfg=None, http_cfg=None, llm_cfg=None,
              db=None, ml_cfg=None)
    CrawlerDB(cfg: Optional[DBConfig] = None)

so it is `WebSearch(db=CrawlerDB(DBConfig(db_path=<news.db>)))` — two
constructors, not one.

**`search_collected_articles` does not read `news.db`.** It globs
`reports/news/news_full_news_*.md` and matches on character windows: no SQL, no
FTS. Persisting pages into `news.db` makes them findable by the database's own
index, but *this function would not see them*. Those are two separable jobs, and
the design note assumes they are one.

**`arricchisci_giornata.enrich_day` needs the hub to list what to enrich** — it
reads `calendar_events` joined to `calendar_indicators`. The standalone,
inject-nothing mode can only work per-release, with the indicator named
explicitly; there is no hub-less way to ask "what came out today".

**Cost, measured, so it is not mistaken for a regression later:** 64 s and 2.3
searches per event on a Western day (Thursday 13th), 157 s and 9.3 on a
non-Western one (Tuesday 11th: RBA, Brazil, Mexico). The local digest covers the
US and Europe; the difference is coverage, not slowness.

## Also still in that temp directory

Only these nine files were the pipeline. The rest is `probe_*`, `diag*` and
one-shot translation scripts. Two exceptions worth knowing about, both already
folded into PR #59: `arricchisci_paese.py` was the real Tradays collector (it
resolves the country from the event href, which is the only thing separating
German CPI from French), and `valida_myfxbook2.py` held the currency→ISO
conversion. Both looked like probes and were neither.
