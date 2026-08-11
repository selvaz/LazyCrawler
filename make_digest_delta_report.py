# -*- coding: utf-8 -*-
"""make_digest_delta_report.py -- what's new in the latest morning digest
relative to the last N evening (USClose) digests.

Reads digests.db (written by make_news_report.py --cycle ...): the most
recent digest for one "query" cycle (default: morning) is checked against
the last N daily digests from a "baseline" cycle (default: usclose, N=4),
which stand in for what's already been covered recently. Per digest,
prefers the Claude engine's text and falls back to DeepSeek's when Claude's
isn't available for that day/cycle.

This compares already-synthesized digest text, not raw articles: it is a
second-order "what's actually new" pass over the daily executive briefs.

Usage:
    python make_digest_delta_report.py
    python make_digest_delta_report.py --query-cycle morning --baseline-cycle usclose --baseline-count 4
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from make_news_report import CYCLES, DEFAULT_DB, DIGESTS_DB, REPORT_DIR  # noqa: E402

DELTA_PROMPT = """\
You are a buy-side macro/portfolio analyst. Below is the accumulated recent
coverage from the last {n_baseline} evening executive news digests (oldest
first) -- treat this as everything already known/covered -- followed by a
NEW morning digest that just came in. Both are already themed summaries of
crawled news, not raw articles.

Your job is to identify what in the NEW morning digest is genuinely new or
materially changed relative to the accumulated evening coverage.

Rules:
1. NEW: an item, development, or story in the morning digest with no
   equivalent mention anywhere in the evening baseline.
2. UPDATED: an item that WAS covered in the evening baseline but has a
   materially new development, number, decision, or escalation in the
   morning digest -- not just restated in different words.
3. Do NOT include anything from the morning digest that simply repeats the
   evening baseline with no new information -- that is noise, not signal.
4. Group by theme (the same themes the digests use: monetary policy,
   growth/inflation, geopolitical risk, corporate/sector news, regional
   flashpoints). For each item, tag it "[NEW]" or "[UPDATED: what changed]",
   one dense factual line/paragraph each.
5. If genuinely nothing is new or updated for a theme, omit that theme
   entirely rather than padding it.

When a digest line is too compressed to tell whether something is genuinely
NEW vs. an UPDATE, or you need a number/date/name the digest text omitted,
you have tools to dig deeper -- use them in this order of preference (cheapest
first):
  a. search_cached / get_session_pages / get_page -- the FULL original
     crawled articles behind these digests, already on disk, free, no
     network call. session_ids: baseline={baseline_session_ids},
     query={query_session_id}.
  b. web_search -- LazyCrawler's own search (DuckDuckGo-backed); cheap, use
     this for anything not already in the cache.
  c. Only if (a) and (b) don't resolve it, fall back to your own native web
     search/fetch tools.
Don't over-research -- most items are resolvable from the digest text alone;
only dig deeper for genuinely ambiguous NEW-vs-UPDATED calls.

=== Evening baseline ({n_baseline} digests, oldest first) ===

{baseline}

=== New morning digest to check against the baseline ===

{query}
"""


def _fetch_cycle_digests(db_path: Path, cycle: str) -> list[dict]:
    """Every distinct daily digest for ``cycle``, most recent first.

    Per session_id, prefers the 'claude' engine row and falls back to
    'deepseek' -- days before the Claude swap (or a Claude failure) only
    ever wrote a deepseek row, which is exactly the desired fallback.
    """
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT session_id, engine, n_articles, text, produced_at "
            "FROM digests WHERE cycle = ? ORDER BY session_id DESC",
            (cycle,),
        ).fetchall()
    finally:
        con.close()

    by_session: dict[str, dict[str, sqlite3.Row]] = {}
    order: list[str] = []
    for row in rows:
        sid = row["session_id"]
        if sid not in by_session:
            by_session[sid] = {}
            order.append(sid)
        by_session[sid][row["engine"]] = row

    out: list[dict] = []
    for sid in order:
        engines = by_session[sid]
        chosen = engines.get("claude") or engines.get("deepseek")
        if chosen is None:
            continue
        out.append(
            {
                "session_id": sid,
                "engine": chosen["engine"],
                "n_articles": chosen["n_articles"],
                "text": chosen["text"],
                "produced_at": chosen["produced_at"],
            }
        )
    return out  # most recent first


def _date_label(session_id: str) -> str:
    # "news_20260804_200008" -> "20260804"
    parts = session_id.replace("news_", "").split("_")
    return parts[0] if parts else session_id


def _format_digest_block(d: dict) -> str:
    return (
        f"=== Digest from {_date_label(d['session_id'])} "
        f"({d['engine']}, {d['n_articles']} articles) ===\n{d['text']}"
    )


def build_delta_report(baseline: list[dict], query: dict) -> str:
    from lazybridge import Agent
    from lazybridge_claude_code import ClaudeCodeEngine
    from lazycrawler import CrawlerDB, DBConfig
    from lazycrawler.tools import CrawlerTools

    # Same news.db the standard digests were built from -- search_cached /
    # get_page / get_session_pages read the already-crawled articles behind
    # these digests for free (no network); web_search is LazyCrawler's own
    # (DuckDuckGo-backed) search, cheaper than Claude's native WebSearch --
    # the prompt tells the model to prefer both over its own native web
    # tools. web=True on the engine keeps native WebSearch/WebFetch
    # available as the last-resort fallback the prompt describes.
    crawler_tools = CrawlerTools(db=CrawlerDB(DBConfig(db_path=str(DEFAULT_DB))))
    try:
        agent = Agent(
            engine=ClaudeCodeEngine(model="sonnet", web=True),
            name="news_digest_delta_writer",
            tools=crawler_tools.as_tools(),
        )
        prompt = DELTA_PROMPT.format(
            n_baseline=len(baseline),
            baseline="\n\n".join(_format_digest_block(d) for d in baseline),
            query=_format_digest_block(query),
            baseline_session_ids=[d["session_id"] for d in baseline],
            query_session_id=query["session_id"],
        )
        env = agent(prompt)
        return env.text()
    finally:
        crawler_tools.close()


def main() -> int:
    p = argparse.ArgumentParser(
        description="Report what's new in the latest query-cycle digest vs. the last N baseline-cycle digests"
    )
    # All three required, none defaulted. Which cycle is checked against
    # which baseline, and over how many digests, decides what counts as
    # "new" — and a default here would let one desk's editorial judgement
    # run silently for another's.
    p.add_argument("--query-cycle", required=True, choices=CYCLES,
                   help="Cycle whose latest digest is checked for novelty")
    p.add_argument("--baseline-cycle", required=True, choices=CYCLES,
                   help="Cycle whose recent digests form the baseline")
    p.add_argument("--baseline-count", type=int, required=True,
                   help="How many most-recent baseline digests to use")
    p.add_argument("--send", action="store_true", help="Also send the delta report to Telegram (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID)")
    args = p.parse_args()

    query_digests = _fetch_cycle_digests(DIGESTS_DB, args.query_cycle)
    if not query_digests:
        print(
            f"No digests found for --query-cycle={args.query_cycle!r} in {DIGESTS_DB}. "
            "Run make_news_report.py with --cycle set for that cycle first.",
            file=sys.stderr,
        )
        return 1
    query = query_digests[0]  # most recent

    baseline_all = _fetch_cycle_digests(DIGESTS_DB, args.baseline_cycle)
    baseline = list(reversed(baseline_all[: args.baseline_count]))  # oldest first, for the prompt
    if len(baseline) < 1:
        print(
            f"No digests found for --baseline-cycle={args.baseline_cycle!r} in {DIGESTS_DB}. "
            "Run make_news_report.py with --cycle set for that cycle first.",
            file=sys.stderr,
        )
        return 1

    print(f"Query: {_date_label(query['session_id'])} ({args.query_cycle}, engine={query['engine']}, {query['n_articles']} articles)")
    print(f"Baseline: {len(baseline)} '{args.baseline_cycle}' digest(s): " + ", ".join(_date_label(d["session_id"]) for d in baseline))

    report_text = build_delta_report(baseline, query)

    out_path = REPORT_DIR / f"digest_delta_{args.query_cycle}_vs_{args.baseline_cycle}_{query['session_id']}.md"
    out_path.write_text(report_text, encoding="utf-8")
    print(f"Delta report: {out_path}")

    if args.send:
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            print(
                "Telegram not configured: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.",
                file=sys.stderr,
            )
            return 2
        from lazytools.connectors.telegram import TelegramClient

        with TelegramClient.from_token(token) as client:
            client.send_document(
                chat_id=chat_id,
                document=report_text.encode("utf-8"),
                filename=out_path.name,
                caption=(
                    f"News delta: what's new in {args.query_cycle} vs last "
                    f"{len(baseline)} {args.baseline_cycle} digests | {query['session_id']}"
                )[:1024],
            )
        print(f"Sent Telegram document: {out_path.name}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
