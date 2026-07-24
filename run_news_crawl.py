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
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import feedparser

sys.path.insert(0, str(Path(__file__).parent))

from lazycrawler import CrawlerConfig, CrawlerDB, DBConfig, LLMConfig, WebCrawler  # noqa: E402
from lazycrawler.http import HTTPClient  # noqa: E402
from news_sources import SOURCES  # noqa: E402

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "news.db"
REPORT_DIR = ROOT / "reports" / "news"
DEEPSEEK_MODEL = "deepseek-v4-flash"

# feedparser's own urllib fetch chokes on this VPS's SSL setup (OS cert store
# issue -- see README "Environments with SSL inspection"); fetch the raw feed
# bytes through LazyCrawler's own requests-based HTTPClient (certifi-backed,
# already the working path for every other fetch in this ecosystem) and hand
# feedparser the text instead of a URL.
_FEED_HTTP = HTTPClient()


def _console_safe(s: str) -> str:
    """Sanitize for printing to a Windows console whose codepage (cp1252/cp932)
    can't represent every Unicode title -- Arabic/Japanese/etc. source titles
    would otherwise raise UnicodeEncodeError and get miscounted as a crawl
    error even though the crawl itself succeeded (matches the
    encode/decode('ascii', 'replace') convention used elsewhere in this
    ecosystem, e.g. amzn_report_agent.py)."""
    enc = sys.stdout.encoding or "ascii"
    return s.encode(enc, "replace").decode(enc)


def _feed_items(url: str, max_items: int) -> list[tuple[str, str]]:
    """(title, link) pairs from a feed, feed order (newest-first), capped."""
    body = _FEED_HTTP.get_text(url)
    if not body:
        raise RuntimeError("could not fetch feed body")
    parsed = feedparser.parse(body)
    if parsed.bozo and not parsed.entries:
        raise RuntimeError(f"unparseable feed: {parsed.bozo_exception}")
    items = []
    for entry in parsed.entries[:max_items]:
        link = entry.get("link")
        if link:
            items.append((entry.get("title", "") or "", link))
    return items


def main() -> int:
    p = argparse.ArgumentParser(description="Crawl the news-monitor source list")
    p.add_argument("--db", default=str(DEFAULT_DB), help="Dedicated SQLite DB path")
    p.add_argument("--ml-max-items", type=int, default=40,
                   help="Max items per ml-mode (no-LLM) source per run (exhaustive-ish; "
                        "most feeds return fewer than this anyway)")
    p.add_argument("--smart-max-items", type=int, default=12,
                   help="Max items per smart-mode (DeepSeek) source per run -- LLM cost cap")
    p.add_argument("--session-id", help="Override session id (default: timestamped)")
    p.add_argument("--sources", help="Comma-separated case-insensitive name substrings "
                                      "to limit the run to (testing)")
    args = p.parse_args()

    session_id = args.session_id or f"news_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"

    # A LazyBridge Session, backed by its own small SQLite event log, attached
    # to every smart-mode agent LazyCrawler builds internally (via
    # LLMConfig.session). PageResult carries no cost/usage fields, so this is
    # the only way to recover what a run actually spent; make_news_report.py
    # reopens the same DB (by session_id) to add the digest call's cost and
    # write the final report.
    from lazybridge.session import Session

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    cost_db_path = REPORT_DIR / f"{session_id}_cost.db"
    cost_session = Session(db=str(cost_db_path))

    db = CrawlerDB(DBConfig(db_path=args.db, ttl_hours=6.0))
    ml_crawler = WebCrawler(
        crawler_cfg=CrawlerConfig(max_depth=0, respect_robots=True, strict=False),
        db=db,
    )
    smart_crawler = WebCrawler(
        crawler_cfg=CrawlerConfig(max_depth=0, respect_robots=True, strict=False),
        llm_cfg=LLMConfig(model=DEEPSEEK_MODEL, session=cost_session),
        db=db,
    )

    sources = SOURCES
    if args.sources:
        needles = [s.strip().lower() for s in args.sources.split(",")]
        sources = [s for s in sources if any(n in s.name.lower() for n in needles)]

    # url -> {name, category, region, lang} sidecar, keyed by the *final*
    # (post-redirect) URL, since that's what ends up in pages.url and is what
    # make_news_report.py looks up -- some feeds (e.g. ForexLive) redirect
    # every article to a different domain (investinglive.com).
    url_meta: dict[str, dict] = {}

    totals = {"ml": 0, "smart": 0, "errors": 0, "feeds_failed": 0}
    for source in sources:
        max_items = args.ml_max_items if source.mode == "ml" else args.smart_max_items
        try:
            items = _feed_items(source.url, max_items)
        except Exception as exc:
            print(f"[{source.name}] feed fetch failed: {exc}", file=sys.stderr)
            totals["feeds_failed"] += 1
            continue

        crawler = ml_crawler if source.mode == "ml" else smart_crawler
        print(f"[{source.name}] {len(items)} item(s), mode={source.mode}")
        for title, link in items:
            t0 = time.time()
            try:
                results = crawler.crawl(
                    link, mode=source.mode,
                    topic=f"{source.category} | {source.region} | {source.name}",
                    session_id=session_id,
                )
                r = results[0] if results else None
                status = r.status if r else "no_result"
                if status == "done":
                    totals[source.mode] += 1
                    url_meta[r.url] = {
                        "name": source.name, "category": source.category,
                        "region": source.region, "lang": source.lang,
                    }
                else:
                    totals["errors"] += 1
                print(f"    {status:<14} ({time.time() - t0:4.1f}s) {_console_safe(title[:80])}")
            except Exception as exc:  # noqa: BLE001 - one bad article must not abort the run
                totals["errors"] += 1
                print(f"    ERROR ({exc}) {_console_safe(title[:80])}", file=sys.stderr)

    db.close()
    cost_session.close()

    meta_path = REPORT_DIR / f"{session_id}_meta.json"
    meta_path.write_text(json.dumps(url_meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\nSession {session_id}: ml={totals['ml']} smart={totals['smart']} "
          f"errors={totals['errors']} feeds_failed={totals['feeds_failed']}")
    print(f"SESSION_ID={session_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
