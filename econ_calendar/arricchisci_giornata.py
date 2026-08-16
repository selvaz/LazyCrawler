# -*- coding: utf-8 -*-
"""End-of-day job: enrich the day's T1 and T2 releases with press coverage and
technical detail.

Runs after the close, once commentary has been published. It takes from the
calendar the releases of the day that deserve attention, runs the enrichment
agent on each, and writes the outcome to `calendar_event_notes`.

Choices worth having in plain sight:

- T1 and T2 only. T3 is context: paying for a web search over it changes no
  decision.
- An event already enriched today is not redone. Regeneration is explicit
  (--regenerate), because every pass costs searches and tokens.
- One failing event does not stop the others: a partial day is still worth
  having, and one broken indicator must not void the whole report.
"""

from __future__ import annotations

import argparse
import json
import traceback
from datetime import date, datetime, timedelta, timezone

TIERS = ("T1", "T2")


def to_enrich(con, day: date, tiers=TIERS, regenerate=False) -> list[dict]:
    """The day's releases that deserve enrichment."""
    already = (
        ""
        if regenerate
        else """
          AND NOT EXISTS (SELECT 1 FROM calendar_event_notes n
                          WHERE n.event_id = e.event_id
                            AND n.generated_at::date = current_date)"""
    )
    cur = con.execute(
        f"""
        SELECT e.event_id, i.name, i.area, i.agency, i.criticality,
               e.reference_period, e.actual, e.consensus, e.previous,
               strftime(e.release_utc, '%Y-%m-%d %H:%M') AS release_utc
        FROM calendar_events e
        JOIN calendar_indicators i USING (indicator_key)
        WHERE e.release_utc::date = ?
          AND e.status = 'released'
          AND i.criticality IN ({",".join("?" * len(tiers))})
          {already}
        ORDER BY i.criticality, e.release_utc
    """,
        [day, *tiers],
    )
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]


def salvage(result):
    """Recover the enrichment when the envelope did not bind it.

    The structured output fails intermittently -- around four runs in ten -- and
    the same release succeeds on one attempt and returns prose on the next, so
    there is no single defect to fix in the model that produces it. Two rungs,
    cheapest first:

    1. the text is often valid JSON that simply was not bound: parse it, free;
    2. otherwise it is prose describing what was found, and a small model
       reshapes it into the schema. It runs only on the failures.

    Either way the searches have already been paid for; discarding the answer
    over its shape would be the expensive choice.
    """
    from .agente_release import Enrichment, formatter

    text = (result.text() or "").strip()
    if not text:
        return None, None

    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            return Enrichment.model_validate_json(text[start : end + 1]), "json"
        except Exception:
            pass

    try:
        riformato = formatter(text)
        p = _come_enrichment(riformato.payload, Enrichment)
        if p is not None:
            return p, "haiku"
    except Exception:
        pass
    return None, None


def _come_enrichment(payload, modello):
    """The formatter returns the model on some runs and its JSON text on others.

    Same intermittency as the enrichment agent, one rung down, so the caller is
    made indifferent to which of the two arrives instead of assuming one.
    """
    if payload is None:
        return None
    if isinstance(payload, modello):
        return payload
    testo = str(payload).strip()
    inizio, fine = testo.find("{"), testo.rfind("}")
    if inizio < 0 or fine <= inizio:
        return None
    try:
        return modello.model_validate_json(testo[inizio : fine + 1])
    except Exception:
        return None


def save(con, event: dict, result, run_id: str | None, attempts: int = 1) -> str | None:
    """Persist the enrichment. Returns how it was obtained, or None if nothing was."""
    p, via = result.payload, "direct"
    if p is None:
        p, via = salvage(result)
    if p is None:
        return None
    con.execute(
        """
        INSERT OR REPLACE INTO calendar_event_notes
            (event_id, generated_at, model, reviewer, review_attempts,
             commentary_json, n_comments, drivers, components,
             technical_source, not_found, run_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            event["event_id"],
            datetime.now(timezone.utc),
            "deepseek-v4-flash",
            "claude-sonnet (reasoning medium)",
            attempts,
            json.dumps([c.model_dump() for c in p.commentary], ensure_ascii=False),
            len(p.commentary),
            p.drivers,
            p.components,
            p.technical_source,
            p.not_found,
            run_id,
        ],
    )
    return via


def enrich_day(
    con, day: date, *, news_dir, news_db=None, regenerate=False, limit=None, search_cap=None
) -> tuple[int, int]:
    """Enrich a day's T1/T2 releases.

    `news_dir` is where the news crawl writes its markdown, and `news_db` the
    crawler database web searches persist into. Both are arguments rather than
    module constants: they used to be one machine's absolute paths, which made
    this importable only there and, worse, silent about it -- a missing
    directory returns nothing and reads exactly like a quiet day.

    The import is deferred because the agent module reaches lazybridge, which
    needs Python >= 3.11, and nothing else in this file does.
    """
    from .agente_release import configura, describe, nuovo_evento, release_agent, ricerche_fatte

    configura(news_dir=news_dir, news_db=news_db)

    events = to_enrich(con, day, regenerate=regenerate)
    if limit:
        events = events[:limit]
    print(f"{day}: {len(events)} T1/T2 releases to enrich", flush=True)

    ok = failed = 0
    for e in events:
        print(f"  [{e['criticality']}] {e['area']} {e['name']} ...", flush=True)
        # Searches and seconds per release, printed next to the commentary count:
        # the three only mean something together. A run that halves the time and
        # halves the commentary has not improved, and either number on its own
        # would say it had.
        nuovo_evento(search_cap)
        partito = datetime.now(timezone.utc)
        try:
            result = release_agent(describe(e))
            via = save(con, e, result, run_id=f"enrichment-{day}")
            costo = (
                f"  [{ricerche_fatte()} searches, "
                f"{(datetime.now(timezone.utc) - partito).total_seconds():.0f}s]"
            )
            if via is None:
                failed += 1
                print(
                    f"      no usable output, not even after reshaping; skipped{costo}", flush=True
                )
                continue
            p = result.payload if via == "direct" else salvage(result)[0]
            recuperato = {
                "direct": "",
                "json": " (json recovered from text)",
                "haiku": " (prose reshaped by haiku)",
            }[via]
            print(
                f"      {len(p.commentary)} commentary{recuperato}"
                f"{' | drivers' if p.drivers else ''}"
                f"{' | components' if p.components else ''}"
                f"{'  (' + p.not_found[:60] + ')' if p.not_found else ''}{costo}",
                flush=True,
            )
            ok += 1
        except Exception as exc:
            # a failing indicator must not void the day
            failed += 1
            print(f"      FAILED: {type(exc).__name__}: {str(exc)[:120]}", flush=True)
            traceback.print_exc(limit=1)
    return ok, failed


if __name__ == "__main__":
    # duckdb arrives with the `calendar` extra, which CI deliberately does not
    # install: nothing else in this repository reads a DuckDB file. It is
    # imported here rather than at module level so the functions above -- which
    # are handed an open connection and never open one themselves -- can be
    # imported and tested without it. Deferral, not a fallback: this still
    # fails loudly and immediately when the extra is missing.
    import duckdb

    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="prova_integrata.duckdb")
    ap.add_argument(
        "--news-dir",
        required=True,
        help="where run_news_crawl.py writes news_full_news_*.md; "
        "there is no default, because a wrong one returns "
        "nothing and looks like a quiet day",
    )
    ap.add_argument(
        "--news-db",
        default=None,
        help="crawler database web searches persist into "
        "(news.db). Without it every page fetched is "
        "discarded when the search closes.",
    )
    ap.add_argument("--day", default=str(date.today() - timedelta(days=1)))
    ap.add_argument("--regenerate", action="store_true", help="redo events already enriched today")
    ap.add_argument(
        "--limit",
        type=int,
        default=None,
        help="stop after N events (to try without spending it all)",
    )
    ap.add_argument(
        "--search-cap",
        type=int,
        default=None,
        help="max web searches per release; omit to count without capping",
    )
    args = ap.parse_args()

    con = duckdb.connect(args.db)
    ok, failed = enrich_day(
        con,
        date.fromisoformat(args.day),
        news_dir=args.news_dir,
        news_db=args.news_db,
        regenerate=args.regenerate,
        limit=args.limit,
        search_cap=args.search_cap,
    )
    con.close()
    print(f"\nenriched {ok}, failed {failed}", flush=True)
