# -*- coding: utf-8 -*-
"""One-off backfill: enrich and report every day of a week.

Sequential by design. The enrichment agent drives web searches; running five
days concurrently would multiply the request rate against the same handful of
sites and is the quickest way to start collecting 403s.

Each day is committed before moving on, so an interruption keeps what is done.
"""

from __future__ import annotations

import argparse
import os
from datetime import date, timedelta
from pathlib import Path

import duckdb

from .arricchisci_giornata import enrich_day
from .render_report import render_html
from .report_giornata import compose_text, day_rows
from .sintesi import build_summary


def send(path: Path, caption: str) -> None:
    from lazytools.connectors.telegram import TelegramClient

    with TelegramClient.from_token(os.environ["TELEGRAM_BOT_TOKEN"]) as client:
        client.send_document(
            chat_id=os.environ["TELEGRAM_CHAT_ID"],
            document=path.read_bytes(),
            filename=path.name,
            caption=caption[:1000],
        )


if __name__ == "__main__":
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
    ap.add_argument("--from-day", default="2026-08-10")
    ap.add_argument("--to-day", default="2026-08-14")
    ap.add_argument("--no-send", action="store_true")
    ap.add_argument("--no-enrich", action="store_true")
    ap.add_argument("--regenerate", action="store_true", help="redo events already enriched today")
    ap.add_argument(
        "--search-cap",
        type=int,
        default=None,
        help="max web searches per release; omit to count without capping",
    )
    args = ap.parse_args()

    day = date.fromisoformat(args.from_day)
    last = date.fromisoformat(args.to_day)
    out_dir = Path("reports/econ")
    out_dir.mkdir(parents=True, exist_ok=True)

    totals = {"enriched": 0, "failed": 0, "sent": 0}
    while day <= last:
        print("=" * 64, flush=True)
        print(day, flush=True)

        if not args.no_enrich:
            con = duckdb.connect(args.db)
            ok, failed = enrich_day(
                con,
                day,
                news_dir=args.news_dir,
                news_db=args.news_db,
                regenerate=args.regenerate,
                search_cap=args.search_cap,
            )
            con.close()  # commit before the next day
            totals["enriched"] += ok
            totals["failed"] += failed

        con = duckdb.connect(args.db, read_only=True)
        rows = day_rows(con, day)
        con.close()

        path = out_dir / f"calendar_{day}.html"
        summary = build_summary(day, rows)
        path.write_text(render_html(day, rows, "tradays", summary), encoding="utf-8")
        caption = compose_text(day, rows)
        enriched = sum(1 for r in rows if r.get("commentary_json") or r.get("drivers"))
        print(f"  report: {len(rows)} releases, {enriched} enriched -> {path.name}", flush=True)

        if not args.no_send and rows:
            send(path, caption)
            totals["sent"] += 1
            print("  sent to Telegram", flush=True)

        day += timedelta(days=1)

    print("\n" + "=" * 64, flush=True)
    print(
        f"enriched {totals['enriched']}, failed {totals['failed']}, reports sent {totals['sent']}",
        flush=True,
    )
