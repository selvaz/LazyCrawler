# -*- coding: utf-8 -*-
"""run_news_crawl.py -- scheduled news-monitor crawl (financial + geopolitical).

Fetches every source's RSS/Atom/RDF feed (see news_sources.py), then crawls
each item link with LazyCrawler: content="ml" (no LLM: TextRank summary,
YAKE topics, spaCy entities, VADER sentiment) for English-language sources,
content="smart" (DeepSeek) for local-language sources where the
English-tuned ml pipeline would degrade. Everything is persisted to a
dedicated SQLite DB (LazyCrawler's own sessions/pages/crawl_edges schema),
keyed by a per-run session_id so make_news_report.py can pull exactly this
run's articles afterward.

Usage:
    python run_news_crawl.py
    python run_news_crawl.py --ml-max-items 40 --smart-max-items 12
    python run_news_crawl.py --sources "bbc,forexlive"   # testing subset
"""

from __future__ import annotations

import argparse
import calendar
from dataclasses import dataclass
import json
import os
import sys
import time
import multiprocessing as mp
import queue
from datetime import datetime, timezone
from pathlib import Path

import feedparser

from lazycrawler import CrawlerConfig, CrawlerDB, DBConfig, LLMConfig, WebCrawler
from lazycrawler.http import HTTPClient
from news_costs import register_cost_session, resolve_cost_db_path
from news_sources import load_sources

ROOT = Path(__file__).resolve().parent
#: Resolved from the environment first, falling back to the path beside this
#: script. The fallback is what this always did; the variable is what lets a
#: deployment say where its data lives. Without it the database follows the
#: CHECKOUT rather than the deployment, so running the same job from a pinned
#: runtime worktree silently starts a second, separate archive -- which is
#: exactly what happened here on 2026-08-14: four days of crawls landed in the
#: worktree's own news.db while every reader still pointed at the original.
DEFAULT_DB = Path(os.environ.get("LAZYCRAWLER_NEWS_DB") or ROOT / "news.db")
REPORT_DIR = ROOT / "reports" / "news"
DEEPSEEK_MODEL = "deepseek-v4-flash"

# feedparser's own urllib fetch chokes on this VPS's SSL setup (OS cert store
# issue -- see README "Environments with SSL inspection"); fetch the raw feed
# bytes through LazyCrawler's own requests-based HTTPClient (certifi-backed,
# already the working path for every other fetch in this ecosystem) and hand
# feedparser the text instead of a URL.
_FEED_HTTP = HTTPClient()


@dataclass(frozen=True)
class ArticleJob:
    """Picklable description of one article for a spawned worker."""

    url: str
    mode: str
    topic: str
    session_id: str
    origin: str
    db_path: str
    cost_db_path: str


@dataclass(frozen=True)
class ArticleOutcome:
    status: str
    url: str
    fetch_seconds: float
    extraction_seconds: float
    total_seconds: float
    error: str | None = None


def _article_worker(job: ArticleJob, messages) -> None:
    """Crawl one article in an isolated process and report phase timings."""
    started = time.perf_counter()
    db = cost_session = None
    try:
        from lazybridge.session import Session

        db = CrawlerDB(DBConfig(db_path=job.db_path, ttl_hours=6.0))
        cost_session = Session(db=job.cost_db_path)
        register_cost_session(job.cost_db_path, job.session_id, cost_session.session_id, "crawl")
        kwargs = {
            "crawler_cfg": CrawlerConfig(max_depth=0, respect_robots=True, strict=False),
            "db": db,
        }
        if job.mode == "smart":
            kwargs["llm_cfg"] = LLMConfig(model=DEEPSEEK_MODEL, session=cost_session)
        crawler = WebCrawler(**kwargs)

        fetch_elapsed = 0.0
        original_fetch = getattr(getattr(crawler, "_http", None), "fetch", None)

        def timed_fetch(url):
            nonlocal fetch_elapsed
            fetch_started = time.perf_counter()
            try:
                return original_fetch(url)
            finally:
                fetch_elapsed += time.perf_counter() - fetch_started
                messages.put(("phase", "extraction", fetch_elapsed))

        if original_fetch is not None:
            crawler._http.fetch = timed_fetch
        messages.put(("phase", "fetch", 0.0))
        results = crawler.crawl(
            job.url,
            mode=job.mode,
            topic=job.topic,
            session_id=job.session_id,
            origin=job.origin,
        )
        total = time.perf_counter() - started
        result = results[0] if results else None
        messages.put(
            (
                "outcome",
                ArticleOutcome(
                    status=result.status if result else "no_result",
                    url=result.url if result else job.url,
                    fetch_seconds=fetch_elapsed,
                    extraction_seconds=max(0.0, total - fetch_elapsed),
                    total_seconds=total,
                ),
            )
        )
    except BaseException as exc:  # child must turn every failure into a result
        total = time.perf_counter() - started
        messages.put(
            (
                "outcome",
                ArticleOutcome("error", job.url, 0.0, total, total, repr(exc)),
            )
        )
    finally:
        if db is not None:
            db.close()
        if cost_session is not None:
            cost_session.close()


def run_article_with_deadline(
    job: ArticleJob, deadline_seconds: float, *, worker=_article_worker
) -> tuple[ArticleOutcome | None, str]:
    """Run a picklable worker in a killable process; return outcome and last phase."""
    method = "spawn" if os.name == "nt" else "fork"
    ctx = mp.get_context(method)
    messages = ctx.Queue()
    process = ctx.Process(target=worker, args=(job, messages))
    process.start()
    expires = time.monotonic() + deadline_seconds
    phase = "worker startup"
    outcome = None
    while process.is_alive() and time.monotonic() < expires:
        try:
            message = messages.get(timeout=min(0.05, max(0.001, expires - time.monotonic())))
        except queue.Empty:
            continue
        if message[0] == "phase":
            phase = message[1]
        else:
            outcome = message[1]
    process.join(timeout=0.05)
    if process.is_alive():
        process.terminate()
        process.join(timeout=2.0)
    while outcome is None:
        try:
            message = messages.get_nowait()
        except queue.Empty:
            break
        if message[0] == "phase":
            phase = message[1]
        else:
            outcome = message[1]
    messages.close()
    return outcome, phase


def _console_safe(s: str) -> str:
    """Sanitize for printing to a Windows console whose codepage (cp1252/cp932)
    can't represent every Unicode title -- Arabic/Japanese/etc. source titles
    would otherwise raise UnicodeEncodeError and get miscounted as a crawl
    error even though the crawl itself succeeded (matches the
    encode/decode('ascii', 'replace') convention used elsewhere in this
    ecosystem, e.g. amzn_report_agent.py)."""
    enc = sys.stdout.encoding or "ascii"
    return s.encode(enc, "replace").decode(enc)


def _entry_age_hours(entry) -> float | None:
    """Hours since the entry's own published/updated date, or None if the
    feed doesn't supply a parseable one (feedparser's *_parsed fields are
    already normalized to UTC struct_time)."""
    st = entry.get("published_parsed") or entry.get("updated_parsed")
    if not st:
        return None
    return (time.time() - calendar.timegm(st)) / 3600.0


def _feed_items(
    url: str, max_items: int, max_age_hours: float
) -> tuple[list[tuple[str, str]], int]:
    """(title, link) pairs from a feed, newest-first, capped at max_items --
    returns (items, n_skipped_as_too_old). An entry with no parseable date is
    kept (the feed itself is inherently a "recent" listing; we can't tell)."""
    body = _FEED_HTTP.get_text(url)
    if not body:
        raise RuntimeError("could not fetch feed body")
    parsed = feedparser.parse(body)
    if parsed.bozo and not parsed.entries:
        raise RuntimeError(f"unparseable feed: {parsed.bozo_exception}")
    items = []
    skipped_old = 0
    for entry in parsed.entries:
        if len(items) >= max_items:
            break
        link = entry.get("link")
        if not link:
            continue
        age = _entry_age_hours(entry)
        if age is not None and age > max_age_hours:
            skipped_old += 1
            continue
        items.append((entry.get("title", "") or "", link))
    return items, skipped_old


def main() -> int:
    p = argparse.ArgumentParser(description="Crawl the news-monitor source list")
    p.add_argument(
        "--sources-config",
        required=True,
        metavar="PATH",
        help="YAML source list. Required: there is no default and no search "
        "path, because a crawl running silently against someone else's "
        "curation produces a plausible digest of the wrong world. See "
        "examples/news_sources.example.yaml.",
    )
    p.add_argument("--db", default=str(DEFAULT_DB), help="Dedicated SQLite DB path")
    p.add_argument(
        "--ml-max-items",
        type=int,
        default=40,
        help="Max items per ml-mode (no-LLM) source per run (exhaustive-ish; "
        "most feeds return fewer than this anyway)",
    )
    p.add_argument(
        "--smart-max-items",
        type=int,
        default=12,
        help="Max items per smart-mode (DeepSeek) source per run -- LLM cost cap",
    )
    p.add_argument(
        "--max-age-hours",
        type=float,
        default=36.0,
        help="Skip feed items older than this (today-or-yesterday, with slack for "
        "timezones/feed lag). An item with no parseable feed date is kept.",
    )
    p.add_argument("--session-id", help="Override session id (default: timestamped)")
    p.add_argument(
        "--article-deadline-seconds",
        type=float,
        default=float(os.environ.get("LAZYCRAWLER_NEWS_ARTICLE_DEADLINE", "90")),
        help="Kill and skip an article after this many seconds (default: 90)",
    )
    p.add_argument(
        "--wall-budget-minutes",
        type=float,
        default=float(os.environ.get("LAZYCRAWLER_NEWS_WALL_BUDGET", "100")),
        help="Stop starting articles after this many minutes (default: 100)",
    )
    p.add_argument(
        "--sources",
        help="Comma-separated case-insensitive name substrings to limit the run to (testing)",
    )
    args = p.parse_args()
    if args.article_deadline_seconds <= 0 or args.wall_budget_minutes <= 0:
        p.error("article deadline and wall budget must be positive")

    session_id = args.session_id or f"news_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    run_started = time.monotonic()
    budget_seconds = args.wall_budget_minutes * 60.0

    # A LazyBridge Session, backed by its own small SQLite event log, attached
    # to every smart-mode agent LazyCrawler builds internally (via
    # LLMConfig.session). PageResult carries no cost/usage fields, so this is
    # the only way to recover what a run actually spent; make_news_report.py
    # reopens the shared DB to add the digest call's cost. The side table
    # links both processes' LazyBridge UUIDs to this news session_id.
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    cost_db_path = resolve_cost_db_path(REPORT_DIR)
    cost_db_path.parent.mkdir(parents=True, exist_ok=True)

    sources = list(load_sources(args.sources_config))
    if args.sources:
        needles = [s.strip().lower() for s in args.sources.split(",")]
        sources = [s for s in sources if any(n in s.name.lower() for n in needles)]

    # url -> {name, category, region, lang} sidecar, keyed by the *final*
    # (post-redirect) URL, since that's what ends up in pages.url and is what
    # make_news_report.py looks up -- some feeds (e.g. ForexLive) redirect
    # every article to a different domain (investinglive.com).
    url_meta: dict[str, dict] = {}

    totals = {
        "ml": 0,
        "smart": 0,
        "errors": 0,
        "timeouts": 0,
        "feeds_failed": 0,
        "skipped_old": 0,
        "skipped_budget": 0,
    }
    budget_exhausted = False
    for source_index, source in enumerate(sources):
        max_items = args.ml_max_items if source.mode == "ml" else args.smart_max_items
        try:
            items, skipped_old = _feed_items(source.url, max_items, args.max_age_hours)
        except Exception as exc:
            print(f"[{source.name}] feed fetch failed: {exc}", file=sys.stderr)
            totals["feeds_failed"] += 1
            continue
        totals["skipped_old"] += skipped_old

        old_note = f", {skipped_old} skipped (too old)" if skipped_old else ""
        print(f"[{source.name}] {len(items)} item(s){old_note}, mode={source.mode}", flush=True)
        for item_index, (title, link) in enumerate(items):
            if time.monotonic() - run_started >= budget_seconds:
                totals["skipped_budget"] += len(items) - item_index
                # Remaining feeds have not been fetched, so conservatively count
                # their configured maximum as skipped work in the summary.
                for remaining in sources[source_index + 1 :]:
                    totals["skipped_budget"] += (
                        args.ml_max_items if remaining.mode == "ml" else args.smart_max_items
                    )
                budget_exhausted = True
                print(
                    f"Wall-clock budget exhausted; skipping {totals['skipped_budget']} "
                    "remaining item slot(s) and continuing to report stage.",
                    flush=True,
                )
                break
            job = ArticleJob(
                url=link,
                mode=source.mode,
                topic=f"{source.category} | {source.region} | {source.name}",
                session_id=session_id,
                origin=os.environ.get(
                    "LAZYCRAWLER_SESSION_ORIGIN", "job:lazycrawler-run_news_crawl"
                ),
                db_path=str(args.db),
                cost_db_path=str(cost_db_path),
            )
            remaining_budget = budget_seconds - (time.monotonic() - run_started)
            outcome, phase = run_article_with_deadline(
                job, min(args.article_deadline_seconds, max(0.001, remaining_budget))
            )
            if outcome is None:
                totals["timeouts"] += 1
                totals["errors"] += 1
                print(
                    f"    TIMEOUT url={link} phase={phase} "
                    f"deadline={args.article_deadline_seconds:.1f}s {_console_safe(title[:80])}",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            status = outcome.status
            if status == "done":
                totals[source.mode] += 1
                url_meta[outcome.url] = {
                    "name": source.name,
                    "category": source.category,
                    "region": source.region,
                    "lang": source.lang,
                }
            else:
                totals["errors"] += 1
            print(
                f"    {status:<14} fetch={outcome.fetch_seconds:.2f}s "
                f"extraction={outcome.extraction_seconds:.2f}s total={outcome.total_seconds:.2f}s "
                f"url={link} {_console_safe(title[:80])}"
                + (f" error={outcome.error}" if outcome.error else ""),
                flush=True,
            )
        if budget_exhausted:
            break

    meta_path = REPORT_DIR / f"{session_id}_meta.json"
    url_meta["_run_summary"] = {
        "partial": budget_exhausted or totals["timeouts"] > 0,
        **totals,
    }
    meta_path.write_text(json.dumps(url_meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        f"\nSession {session_id}: ml={totals['ml']} smart={totals['smart']} "
        f"errors={totals['errors']} feeds_failed={totals['feeds_failed']} "
        f"timeouts={totals['timeouts']} skipped_budget={totals['skipped_budget']} "
        f"partial={str(url_meta['_run_summary']['partial']).lower()} "
        f"skipped_old={totals['skipped_old']} (max_age_hours={args.max_age_hours})",
        flush=True,
    )
    print(f"SESSION_ID={session_id}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
