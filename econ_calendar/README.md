# Economic calendar enrichment

The enrichment half of the economic release calendar: given a release already in
the calendar, it finds commentary published on *that* release and technical
detail on how the indicator is built, and writes the outcome to
`calendar_event_notes`.

The collection half is in market-data-hub (merged as
`Give the calendar collection a home, and its timezones a check`). The split is
not a preference: market-data-hub declares `requires-python = ">=3.9"` and tests
on 3.9, while LazyBridge requires >= 3.11. This repository already carries that
shape, with lazybridge behind the `smart` extra — and here, imported inside the
functions that need it, so the rest of the package stays usable without it.

| file | what it is |
|---|---|
| `agente_release.py` | the agent, its reviewer, the formatter and the fallback |
| `fonti_locali.py` | `read_daily_digest`, `search_collected_articles` |
| `domini_bloccati.py` + `.txt` | sources that refuse automated traffic |
| `arricchisci_giornata.py` | the T1/T2 day job |
| `sintesi.py` | executive summary |
| `render_report.py` | HTML |
| `report_giornata.py` | the day's report |
| `run_week.py` | orchestrator |

## Running it

```
python -m econ_calendar.arricchisci_giornata \
    --db <market_data.duckdb> \
    --news-dir <LazyCrawler/reports/news> \
    --news-db <LazyCrawler/news.db> \
    --day 2026-08-13
```

`--news-dir` is required and has no default. It used to be one machine's
absolute path written into the module, which meant the code worked there and
nowhere else — and said nothing about it, because a missing directory returns
an empty digest, which reads exactly like a day on which nothing was published.
It now raises instead.

`--news-db` is what makes the pages a web search downloads persist. Without it
`WebSearch` keeps them in memory and discards them when the search closes, so
the same page is fetched again for the next release that mentions it.

## What the port changed

All five items the raw branch listed as blocking:

- **Four absolute `sys.path.insert` are gone.** Three pointed at
  market-data-hub and were dead — nothing in the package imports it; the job
  opens the DuckDB file directly. The fourth put the package's own directory on
  the path, and is replaced by ordinary relative imports.
- **`NEWS_DIR` is an argument**, threaded through `read_daily_digest`,
  `search_collected_articles` and `enrich_day`, and refused when it does not
  exist.
- **`WebSearch` is given a database.** As the raw branch had already found, the
  constructor takes `db`, not a `DBConfig`: it is
  `WebSearch(db=CrawlerDB(DBConfig(db_path=...)))`, two constructors.
- **`lazybridge_claude_code` is now `lazybridge`.** That package was the
  standalone prototype; the engine was absorbed into lazybridge. It is still
  importable on this machine, which is exactly why the import had to change:
  it would have kept working here and nowhere else.
- **The agent's tools take only what the model can decide.** The naive way to
  thread the news directory through would have put it in the tool schema —
  asking a language model to guess a filesystem path. It is bound by
  `configura()` instead.

## Still true, and deliberately not changed

**`search_collected_articles` does not read `news.db`.** It globs
`reports/news/news_full_news_*.md` and matches on character windows: no SQL, no
FTS. Persisting pages into `news.db` makes them findable by the database's own
index, but *this function still will not see them*. Those are two separable
jobs; only the persistence one is done. Making the search read the database is
a change to what the agent retrieves, and belongs in its own change with its own
before/after measurement.

**Cost, measured, so it is not mistaken for a regression later:** 64 s and 2.3
searches per event on a Western day (Thursday 13th), 157 s and 9.3 on a
non-Western one (Tuesday 11th: RBA, Brazil, Mexico). The local digest covers the
US and Europe; the difference is coverage, not slowness.

**Not yet scheduled.** Nothing registers this as a job. The collection half runs
from the investment-committee matrix; wiring the enrichment beside it is a
separate step, and it needs the collection to have run first.
