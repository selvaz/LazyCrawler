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
import json
import multiprocessing as mp
import os
import queue
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import feedparser

from lazycrawler import CrawlerConfig, CrawlerDB, DBConfig, LLMConfig, WebCrawler
from lazycrawler.http import HTTPClient, registrable_domain
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
WRITE_SAFETY_EXIT = 4

# feedparser's own urllib fetch chokes on this VPS's SSL setup (OS cert store
# issue -- see README "Environments with SSL inspection"); fetch the raw feed
# bytes through LazyCrawler's own requests-based HTTPClient (certifi-backed,
# already the working path for every other fetch in this ecosystem) and hand
# feedparser the text instead of a URL.
_FEED_HTTP = HTTPClient()


@dataclass(frozen=True)
class ArticleJob:
    """Picklable description of one article handled by the crawl worker."""

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
    new_page: bool = False
    refetched_identical: bool = False
    cache_hit: bool = False
    write_failed: bool = False
    error: str | None = None


@dataclass(frozen=True)
class WorkerConfig:
    """Resources shared by every item handled during one worker lifetime."""

    db_path: str
    cost_db_path: str
    session_id: str
    # Primarily an integration seam: unlike monkeypatches, a module-level
    # callable survives Windows ``spawn`` and lets the actual worker/session
    # attribution path be exercised without making network requests.
    crawler_factory: object | None = None


@dataclass
class ConsecutiveTimeoutCircuit:
    """Small, deterministic policy object used by the article scheduling loop."""

    limit: int
    consecutive: int = 0

    def record(self, timed_out: bool) -> bool:
        self.consecutive = self.consecutive + 1 if timed_out else 0
        return self.consecutive >= self.limit


class DomainTimeoutCircuits:
    """Track independent timeout streaks and a conservative global bound."""

    def __init__(self, limit: int):
        self.limit = limit
        self._circuits: dict[str, ConsecutiveTimeoutCircuit] = {}
        self.attempted_domains: set[str] = set()
        self.tripped_domains: set[str] = set()

    def is_tripped(self, domain: str) -> bool:
        return domain in self.tripped_domains

    def record(self, domain: str, timed_out: bool) -> bool:
        self.attempted_domains.add(domain)
        circuit = self._circuits.setdefault(domain, ConsecutiveTimeoutCircuit(self.limit))
        if circuit.record(timed_out):
            self.tripped_domains.add(domain)
        return domain in self.tripped_domains

    def global_bound_open(self) -> bool:
        """Stop pathological runs once multiple domains and a majority have tripped.

        Requiring two tripped domains ensures a single broken site can never end
        the crawl before another site's first article is attempted.
        """
        return (
            len(self.tripped_domains) >= 2
            and len(self.tripped_domains) * 2 > len(self.attempted_domains)
        )


def _partial_run_exit_code(
    *, partial: bool, new_pages: int, attempted: int, minimum_new_pages: int, minimum_ratio: float
) -> int:
    """Return the public write-safety exit code for a completed crawl."""
    ratio = new_pages / attempted if attempted else 0.0
    insufficient = new_pages < minimum_new_pages or ratio < minimum_ratio
    return WRITE_SAFETY_EXIT if partial and insufficient else 0


def _crawl_item(job, messages, crawlers) -> ArticleOutcome:
    """Real worker-loop item function, separately callable for in-process tests."""
    started = time.perf_counter()
    fetch_elapsed = 0.0
    try:
        crawler = crawlers[job.mode]

        fetch_lock = threading.Lock()

        def observe_fetch(event, elapsed):
            # Called from crawler worker threads when workers > 1; overlapping
            # fetches are summed, so fetch time can exceed wall time there.
            nonlocal fetch_elapsed
            with fetch_lock:
                if event == "start":
                    messages.put(("phase", "fetch", fetch_elapsed))
                else:
                    fetch_elapsed += elapsed
                    messages.put(("phase", "extraction", fetch_elapsed))

        def observe_phase(phase):
            messages.put(("phase", phase, fetch_elapsed))

        messages.put(("phase", "fetch", 0.0))
        results = crawler.crawl(
            job.url,
            mode=job.mode,
            topic=job.topic,
            session_id=job.session_id,
            origin=job.origin,
            fetch_observer=observe_fetch,
            phase_observer=observe_phase,
        )
        total = time.perf_counter() - started
        result = results[0] if results else None
        disposition = getattr(result, "write_disposition", None) if result else None
        from_cache = bool(getattr(result, "from_cache", False)) if result else False
        write_failed = disposition == "write_failed"
        return ArticleOutcome(
            "write_failed" if write_failed else (result.status if result else "no_result"),
            result.url if result else job.url,
            fetch_elapsed,
            max(0.0, total - fetch_elapsed),
            total,
            # A current producer labels a page only after upsert_page returns.
            # Preserve the legacy fallback, except for an explicitly failed
            # write: extracted is not the same thing as persisted.
            new_page=(disposition == "new_page" or (not from_cache and disposition is None))
            if result
            else False,
            refetched_identical=disposition == "refetched_identical",
            # Older/custom PageResult producers may not yet label URL-cache
            # reuse. Treat the only otherwise-unclassified successful cached
            # result as a cache hit, keeping the accounting exhaustive.
            cache_hit=from_cache and disposition not in ("new_page", "refetched_identical"),
            write_failed=write_failed,
        )
    except BaseException as exc:
        total = time.perf_counter() - started
        return ArticleOutcome(
            "error",
            job.url,
            fetch_elapsed,
            max(0.0, total - fetch_elapsed),
            total,
            error=repr(exc),
        )


def _worker_loop(config: WorkerConfig, jobs, messages, work_item=_crawl_item) -> None:
    """Own expensive imports/resources once, then service jobs until stopped."""
    db = cost_session = None
    if work_item is _crawl_item:
        from lazybridge.session import Session

        db = CrawlerDB(DBConfig(db_path=config.db_path, ttl_hours=6.0))
        cost_session = Session(db=config.cost_db_path)
        register_cost_session(
            config.cost_db_path, config.session_id, cost_session.session_id, "crawl"
        )
        crawler_factory = config.crawler_factory or WebCrawler
        common = dict(
            crawler_cfg=CrawlerConfig(max_depth=0, respect_robots=True, strict=False),
            db=db,
        )
        crawlers = {
            "ml": crawler_factory(**common),
            "smart": crawler_factory(
                **common, llm_cfg=LLMConfig(model=DEEPSEEK_MODEL, session=cost_session)
            ),
        }
    else:
        crawlers = {}  # injected test work still traverses the production queue loop
    messages.put(("ready", os.getpid()))
    try:
        while True:
            command = jobs.get()
            if command is None:
                break
            kind, item_id, payload = command
            messages.put(("started", item_id, kind))
            if kind == "article":
                outcome = work_item(payload, messages, crawlers)
                messages.put(("outcome", item_id, outcome))
            elif kind == "feed":
                try:
                    value = _feed_items(*payload)
                    messages.put(("feed_outcome", item_id, value, None))
                except BaseException as exc:
                    messages.put(("feed_outcome", item_id, None, repr(exc)))
    finally:
        # A timed-out smart call may already have emitted partial usage. Closing
        # checkpoints all events known before the parent terminated this worker.
        if db is not None:
            db.close()
        if cost_session is not None:
            cost_session.close()


def _stop_process(process, jobs, messages, *, terminate=False) -> None:
    if process is None:
        return
    if terminate and process.is_alive():
        if os.name == "nt":
            import subprocess

            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=5.0,
                )
            except subprocess.TimeoutExpired:
                # Fall through to multiprocessing's bounded kill below.
                pass
        else:
            process.terminate()
    process.join(timeout=5.0)
    if process.is_alive():
        process.kill()
        process.join(timeout=2.0)
    process.close()
    for q in (jobs, messages):
        if q is not None:
            q.cancel_join_thread()
            q.close()


class PersistentWorker:
    """Spawn-safe worker whose per-item clock starts only at ``started``."""

    def __init__(
        self,
        config,
        *,
        recycle_items=200,
        startup_timeout=60.0,
        work_item=_crawl_item,
        mp_start_method=None,
    ):
        self.config = config
        self.recycle_items = recycle_items
        self.startup_timeout = startup_timeout
        self.work_item = work_item
        self.mp_start_method = mp_start_method
        self.process = self.jobs = self.messages = None
        self.completed = 0
        self.item_id = 0
        self.was_killed = False

    def _start(self):
        ctx = mp.get_context(self.mp_start_method or ("spawn" if os.name == "nt" else "fork"))
        self.jobs, self.messages = ctx.Queue(), ctx.Queue()
        self.process = ctx.Process(
            target=_worker_loop,
            args=(self.config, self.jobs, self.messages, self.work_item),
        )
        self.process.start()
        startup_expires = time.monotonic() + self.startup_timeout
        message = None
        while self.process.is_alive() and time.monotonic() < startup_expires:
            try:
                message = self.messages.get(timeout=0.05)
                break
            except queue.Empty:
                continue
        if message is None:
            self._discard()
            raise RuntimeError("crawl worker exited or did not become ready")
        if message[0] != "ready":
            self._discard()
            raise RuntimeError(f"unexpected crawl worker startup message: {message!r}")

    def _discard(self):
        self.was_killed = True
        _stop_process(self.process, self.jobs, self.messages, terminate=True)
        self.process = self.jobs = self.messages = None
        self.completed = 0

    def _ensure(self):
        if self.process is None or not self.process.is_alive():
            if self.process is not None:
                self._discard()
            self._start()
        elif self.completed >= self.recycle_items:
            self.close()
            self._start()

    def run(self, kind, payload, deadline_seconds):
        self._ensure()
        self.item_id += 1
        item_id = self.item_id
        self.jobs.put((kind, item_id, payload))
        # Startup/import time is deliberately outside the article/feed deadline.
        startup_expires = time.monotonic() + self.startup_timeout
        while self.process.is_alive() and time.monotonic() < startup_expires:
            try:
                message = self.messages.get(timeout=0.05)
            except queue.Empty:
                continue
            if message[:2] == ("started", item_id):
                break
        else:
            self._discard()
            return None, "worker startup"
        expires = time.monotonic() + deadline_seconds
        phase = "fetch" if kind in ("article", "feed") else kind
        while time.monotonic() < expires and self.process.is_alive():
            try:
                message = self.messages.get(
                    timeout=min(0.05, max(0.001, expires - time.monotonic()))
                )
            except queue.Empty:
                continue
            if message[0] == "phase":
                phase = message[1]
            elif message[1] == item_id:
                self.completed += 1
                return message, phase
        self._discard()
        return None, phase

    def article(self, job, deadline_seconds):
        message, phase = self.run("article", job, deadline_seconds)
        return (message[2] if message else None), phase

    def feed(self, url, max_items, max_age_hours, deadline_seconds):
        message, _ = self.run("feed", (url, max_items, max_age_hours), deadline_seconds)
        if message is None:
            raise TimeoutError(f"feed exceeded {deadline_seconds:.1f}s deadline")
        if message[3] is not None:
            raise RuntimeError(message[3])
        return message[2]

    def close(self):
        if self.process is None:
            return
        if self.process.is_alive():
            self.jobs.put(None)
        _stop_process(self.process, self.jobs, self.messages)
        self.process = self.jobs = self.messages = None
        self.completed = 0

    def checkpoint_if_killed(self):
        """Checkpoint WAL left behind by a force-terminated worker, if any."""
        if not self.was_killed:
            return
        db_path = Path(self.config.db_path)
        if not db_path.exists():
            return
        with sqlite3.connect(db_path, timeout=5.0) as connection:
            connection.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchall()

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()


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


def main(
    argv=None,
    *,
    worker_factory=None,
    sources_loader=None,
    report_dir: Path | None = None,
    cost_db_resolver=None,
) -> int:
    worker_factory = PersistentWorker if worker_factory is None else worker_factory
    sources_loader = load_sources if sources_loader is None else sources_loader
    cost_db_resolver = resolve_cost_db_path if cost_db_resolver is None else cost_db_resolver
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
        "--worker-recycle-items",
        type=int,
        default=int(os.environ.get("LAZYCRAWLER_NEWS_WORKER_RECYCLE", "200")),
        help="Recycle the persistent worker after this many completed items (default: 200)",
    )
    p.add_argument(
        "--worker-startup-timeout-seconds",
        type=float,
        default=60.0,
        help="Maximum wait for worker imports/initialization (not an item deadline)",
    )
    p.add_argument(
        "--worker-start-attempts",
        type=int,
        default=int(os.environ.get("LAZYCRAWLER_NEWS_WORKER_START_ATTEMPTS", "3")),
        help="Maximum worker start attempts for an article (default: 3)",
    )
    p.add_argument(
        "--worker-restart-backoff-seconds",
        type=float,
        default=float(os.environ.get("LAZYCRAWLER_NEWS_WORKER_BACKOFF", "0.5")),
        help="Pause between failed worker start attempts (default: 0.5)",
    )
    p.add_argument(
        "--minimum-article-budget-seconds",
        type=float,
        default=5.0,
        help="Do not start an article with less wall budget remaining (default: 5)",
    )
    p.add_argument(
        "--feed-deadline-seconds",
        type=float,
        default=30.0,
        help="Kill and restart the worker when one feed exceeds this deadline",
    )
    p.add_argument(
        "--max-consecutive-timeouts",
        type=int,
        default=int(os.environ.get("LAZYCRAWLER_NEWS_MAX_CONSECUTIVE_TIMEOUTS", "5")),
        help="Skip a domain after this many consecutive timeouts (default: 5)",
    )
    p.add_argument(
        "--minimum-new-pages",
        type=int,
        default=int(os.environ.get("LAZYCRAWLER_NEWS_MINIMUM_NEW_PAGES", "1")),
        help="Partial runs fail when fewer new pages are written (default: 1)",
    )
    p.add_argument(
        "--minimum-new-page-ratio",
        type=float,
        default=float(os.environ.get("LAZYCRAWLER_NEWS_MINIMUM_NEW_PAGE_RATIO", "0.10")),
        help="Partial runs fail below this new-pages/attempted ratio (default: 0.10)",
    )
    p.add_argument(
        "--sources",
        help="Comma-separated case-insensitive name substrings to limit the run to (testing)",
    )
    args = p.parse_args(argv)
    if (
        args.article_deadline_seconds <= 0
        or args.wall_budget_minutes <= 0
        or args.worker_recycle_items <= 0
        or args.feed_deadline_seconds <= 0
        or args.worker_start_attempts <= 0
        or args.worker_restart_backoff_seconds < 0
        or args.minimum_article_budget_seconds <= 0
        or args.max_consecutive_timeouts <= 0
        or args.minimum_new_pages < 0
        or not 0 <= args.minimum_new_page_ratio <= 1
    ):
        p.error("deadlines, wall budget, and worker recycle count must be positive")

    session_id = args.session_id or f"news_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    run_started = time.monotonic()
    budget_seconds = args.wall_budget_minutes * 60.0

    # A LazyBridge Session, backed by its own small SQLite event log, attached
    # to every smart-mode agent LazyCrawler builds internally (via
    # LLMConfig.session). PageResult carries no cost/usage fields, so this is
    # the only way to recover what a run actually spent; make_news_report.py
    # reopens the shared DB to add the digest call's cost. The side table
    # links both processes' LazyBridge UUIDs to this news session_id.
    active_report_dir = REPORT_DIR if report_dir is None else report_dir
    active_report_dir.mkdir(parents=True, exist_ok=True)
    cost_db_path = cost_db_resolver(active_report_dir)
    cost_db_path.parent.mkdir(parents=True, exist_ok=True)

    sources = list(sources_loader(args.sources_config))
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
        "attempted": 0,
        "new_pages": 0,
        "refetched_identical": 0,
        "cache_hits": 0,
        "write_failed": 0,
    }
    budget_exhausted = False
    worker_failed = False
    circuit_open = False
    timeout_circuits = DomainTimeoutCircuits(args.max_consecutive_timeouts)
    worker = worker_factory(
        WorkerConfig(str(args.db), str(cost_db_path), session_id),
        recycle_items=args.worker_recycle_items,
        startup_timeout=args.worker_startup_timeout_seconds,
    )
    try:
        for source_index, source in enumerate(sources):
            if time.monotonic() - run_started >= budget_seconds:
                totals["skipped_budget"] += sum(
                    (args.ml_max_items if remaining.mode == "ml" else args.smart_max_items)
                    for remaining in sources[source_index:]
                )
                budget_exhausted = True
                print(
                    f"Wall-clock budget exhausted; skipping {totals['skipped_budget']} "
                    "remaining item slot(s) and continuing to report stage.",
                    flush=True,
                )
                break
            max_items = args.ml_max_items if source.mode == "ml" else args.smart_max_items
            try:
                items, skipped_old = worker.feed(
                    source.url,
                    max_items,
                    args.max_age_hours,
                    args.feed_deadline_seconds,
                )
            except Exception as exc:
                print(f"[{source.name}] feed fetch failed: {exc}", file=sys.stderr)
                totals["feeds_failed"] += 1
                continue
            totals["skipped_old"] += skipped_old

            old_note = f", {skipped_old} skipped (too old)" if skipped_old else ""
            print(
                f"[{source.name}] {len(items)} item(s){old_note}, mode={source.mode}",
                flush=True,
            )
            for item_index, (title, link) in enumerate(items):
                domain = registrable_domain(link) or link
                if timeout_circuits.is_tripped(domain):
                    totals["skipped_budget"] += 1
                    print(f"    SKIPPED tripped_domain={domain} url={link}", flush=True)
                    continue
                remaining_budget = budget_seconds - (time.monotonic() - run_started)
                if remaining_budget < args.minimum_article_budget_seconds:
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
                totals["attempted"] += 1
                for start_attempt in range(1, args.worker_start_attempts + 1):
                    try:
                        outcome, phase = worker.article(
                            job,
                            min(args.article_deadline_seconds, remaining_budget),
                        )
                        break
                    except RuntimeError as exc:
                        if start_attempt == args.worker_start_attempts:
                            totals["errors"] += 1
                            worker_failed = True
                            print(
                                "Crawl worker could not be restarted after "
                                f"{start_attempt} attempt(s): {exc}; ending partial run.",
                                file=sys.stderr,
                                flush=True,
                            )
                            break
                        print(
                            f"Crawl worker start failed (attempt {start_attempt}/"
                            f"{args.worker_start_attempts}): {exc}; retrying.",
                            file=sys.stderr,
                            flush=True,
                        )
                        time.sleep(args.worker_restart_backoff_seconds)
                if worker_failed:
                    break
                if outcome is None:
                    totals["timeouts"] += 1
                    domain_tripped = timeout_circuits.record(domain, True)
                    print(
                        f"    TIMEOUT url={link} phase={phase} "
                        f"deadline={args.article_deadline_seconds:.1f}s {_console_safe(title[:80])}",
                        file=sys.stderr,
                        flush=True,
                    )
                    if domain_tripped:
                        print(
                            f"Article circuit breaker opened for {domain} after "
                            f"{args.max_consecutive_timeouts} consecutive timeouts; "
                            "skipping that domain.",
                            file=sys.stderr,
                            flush=True,
                        )
                        circuit_open = timeout_circuits.global_bound_open()
                        if circuit_open:
                            print(
                                "Global article circuit breaker opened because more than half "
                                "of attempted domains tripped; ending partial run.",
                                file=sys.stderr,
                                flush=True,
                            )
                            break
                    continue
                timeout_circuits.record(domain, False)
                status = outcome.status
                if status == "done":
                    totals[source.mode] += 1
                    totals["new_pages"] += int(outcome.new_page)
                    totals["refetched_identical"] += int(outcome.refetched_identical)
                    totals["cache_hits"] += int(outcome.cache_hit)
                    url_meta[outcome.url] = {
                        "name": source.name,
                        "category": source.category,
                        "region": source.region,
                        "lang": source.lang,
                    }
                else:
                    totals["errors"] += 1
                    totals["write_failed"] += int(outcome.write_failed)
                print(
                    f"    {status:<14} fetch={outcome.fetch_seconds:.2f}s "
                    f"extraction={outcome.extraction_seconds:.2f}s total={outcome.total_seconds:.2f}s "
                    f"url={link} {_console_safe(title[:80])}"
                    + (f" error={outcome.error}" if outcome.error else ""),
                    flush=True,
                )
            if budget_exhausted or worker_failed or circuit_open:
                break
    finally:
        worker.close()
        try:
            checkpoint = getattr(worker, "checkpoint_if_killed", None)
            if checkpoint is not None:
                checkpoint()
        except (OSError, sqlite3.Error) as exc:
            print(f"Warning: news DB WAL checkpoint failed: {exc}", file=sys.stderr)

    meta_path = active_report_dir / f"{session_id}_meta.json"
    partial = (
        budget_exhausted
        or worker_failed
        or bool(timeout_circuits.tripped_domains)
        or totals["timeouts"] > 0
        or totals["errors"] > 0
        or totals["write_failed"] > 0
    )
    reasons = []
    if budget_exhausted:
        reasons.append("wall_budget")
    if worker_failed:
        reasons.append("worker_failure")
    if timeout_circuits.tripped_domains:
        reasons.append("consecutive_timeouts")
    elif totals["timeouts"]:
        reasons.append("article_timeouts")
    if totals["errors"] and not worker_failed:
        reasons.append("article_errors")
    new_page_ratio = totals["new_pages"] / totals["attempted"] if totals["attempted"] else 0.0
    url_meta["_run_summary"] = {
        "partial": partial,
        "reason": reasons[0] if len(reasons) == 1 else reasons,
        "tripped_domains": sorted(timeout_circuits.tripped_domains),
        "new_page_ratio": new_page_ratio,
        **totals,
    }
    meta_path.write_text(json.dumps(url_meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print("_run_summary=" + json.dumps(url_meta["_run_summary"], separators=(",", ":")), flush=True)
    print(f"SESSION_ID={session_id}", flush=True)
    if timeout_circuits.tripped_domains:
        return WRITE_SAFETY_EXIT
    return _partial_run_exit_code(
        partial=partial,
        new_pages=totals["new_pages"],
        attempted=totals["attempted"],
        minimum_new_pages=args.minimum_new_pages,
        minimum_ratio=args.minimum_new_page_ratio,
    )


if __name__ == "__main__":
    raise SystemExit(main())
