# -*- coding: utf-8 -*-
"""make_news_report.py -- build the digest + full-text report for one crawl run.

Reads every "done" page from the given (or latest) news_crawl session and
writes, under reports/news/:

  - news_full_<session>_<region>.md  one file per geographic region (us,
                                      europe, asia, africa, latam, mena,
                                      global) with every article's full
                                      extracted text + metadata (source,
                                      published date, sentiment, topics,
                                      entities) -- the "entire news", not
                                      just a summary. The region comes from
                                      the <session>_meta.json sidecar that
                                      run_news_crawl.py writes (source ->
                                      region/category), not from the page
                                      row itself (LazyCrawler's own schema
                                      has no region column).
  - news_digest_<session>.md         a DeepSeek-written executive digest,
                                      grouped by theme (not region), built
                                      from the per-article
                                      summaries/sentiment/topics already
                                      extracted at crawl time (ml
                                      TextRank/VADER or smart DeepSeek) --
                                      this call does NOT re-read raw article
                                      text, so it stays a small, cheap
                                      synthesis step regardless of how many
                                      articles were crawled.

Usage:
    python make_news_report.py
    python make_news_report.py --session-id news_20260723_070000
    python make_news_report.py --no-digest   # full report only, skip DeepSeek
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from lazycrawler import CrawlerDB, DBConfig  # noqa: E402

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "news.db"
REPORT_DIR = ROOT / "reports" / "news"
DIGEST_MODEL = "deepseek-v4-flash"
UNKNOWN_REGION = "unclassified"

DIGEST_PROMPT = """\
You are a buy-side macro/portfolio analyst preparing a same-day briefing for
a portfolio manager who allocates across asset classes and regions. Below is
a list of news items crawled in the last cycle (title, source, sentiment,
topics, short summary) from financial wires, central banks, and geopolitical
outlets spanning developed and emerging markets, including local-language
sources translated at crawl time.

Write a concise executive digest in Markdown:
1. Group items by theme (monetary policy, growth/inflation data, geopolitical
   risk, market-moving corporate/sector news, regional flashpoints).
2. Within each theme, lead with whatever is most likely to matter for asset
   allocation (rates, currencies, equities, commodities), and note the
   prevailing sentiment/tone for that theme.
3. Add a short "Under-covered by Western wires" section for anything
   emerging-market/local-source items surfaced that the major outlets
   missed or downplayed.
4. Be dense and factual, no filler, no restating the obvious. Use headers
   per theme.

News items ({n} total):
{items}
"""


def _latest_session_id(db: CrawlerDB) -> str | None:
    row = db.conn.execute(
        "SELECT session_id FROM sessions WHERE session_id LIKE 'news_%' "
        "ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    return row[0] if row else None


def _load_meta(session_id: str) -> dict[str, dict]:
    """The url -> {name, category, region, lang} sidecar run_news_crawl.py
    wrote for this session. Missing/unreadable -> {} (pages fall back to
    UNKNOWN_REGION rather than crashing the report)."""
    meta_path = REPORT_DIR / f"{session_id}_meta.json"
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _enrich(pages: list[dict], meta: dict[str, dict]) -> list[dict]:
    for p in pages:
        info = meta.get(p.get("url"), {})
        p["source_name"] = info.get("name") or p.get("domain")
        p["category"] = info.get("category") or "n/a"
        p["region"] = info.get("region") or UNKNOWN_REGION
    return pages


def _group_by_region(pages: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for p in pages:
        groups.setdefault(p["region"], []).append(p)
    return groups


def _fmt_article(p: dict) -> str:
    lines = [
        f"### {p.get('title') or '(untitled)'}",
        f"- Source: {p.get('source_name') or p.get('domain')} ({p.get('category', 'n/a')}) "
        f"| Published: {p.get('published_iso') or 'n/a'} "
        f"| Sentiment: {p.get('sentiment') or 'n/a'} | Mode: {p.get('mode')}",
        f"- URL: {p.get('url')}",
    ]
    topics = p.get("topics") or []
    entities = p.get("entities") or []
    if topics:
        lines.append(f"- Topics: {', '.join(topics)}")
    if entities:
        lines.append(f"- Entities: {', '.join(entities[:20])}")
    if p.get("summary"):
        lines.append(f"\n**Summary**: {p['summary']}")
    lines.append(f"\n{p.get('clean_text') or '(no text extracted)'}")
    return "\n".join(lines)


def _fmt_index_entry(i: int, p: dict) -> str:
    lines = [
        f"{i}. **{p.get('title') or '(untitled)'}** -- "
        f"{p.get('source_name') or p.get('domain')} "
        f"[{p.get('category', 'n/a')}, {p.get('sentiment') or 'n/a'}]",
    ]
    summary = p.get("summary") or "(no summary extracted)"
    lines.append(f"   {summary}")
    return "\n".join(lines)


def build_region_report(region: str, pages: list[dict], session_id: str) -> str:
    parts = [
        f"# News crawl - {region} - {session_id}",
        "",
        f"Generated: {datetime.now().isoformat(timespec='seconds')} | {len(pages)} articles",
        "",
        "## Index",
        "",
    ]
    for i, p in enumerate(pages, start=1):
        parts.append(_fmt_index_entry(i, p))
        parts.append("")
    parts.append("\n---\n")
    parts.append("## Full articles")
    parts.append("")
    for p in pages:
        parts.append(_fmt_article(p))
        parts.append("\n---\n")
    return "\n".join(parts)


def _digest_input(pages: list[dict]) -> str:
    lines = []
    for p in pages:
        topics = ", ".join((p.get("topics") or [])[:6])
        summary = (p.get("summary") or "")[:400]
        lines.append(
            f"- [{p.get('domain')}] {p.get('title')} | sentiment={p.get('sentiment')} "
            f"| topics={topics} | summary={summary}"
        )
    return "\n".join(lines)


def generate_index_summaries(pages: list[dict], cost_session=None) -> None:
    """Overwrite every page's ``summary`` with a fresh, clean 2-4 sentence
    English summary written from ``clean_text`` -- for EVERY page, not just
    smart-mode/non-English ones.

    Two independent problems showed up once summaries actually got read in
    the index instead of just archived in the DB:
      1. Language: smart-mode summaries come out in the source's own
         language (Spanish/Portuguese/Arabic/Japanese/French).
      2. Quality (ml-mode only): LazyCrawler's no-LLM TextRank summarizer
         sometimes returns a much-longer-than-requested block full of page
         chrome ("- Published", "Related topics", byline fragments) instead
         of a short summary -- BBC's page layout in particular confuses it.
         MLConfig.summary_sentences=4 is the intent; TextRank/lead-fallback
         doesn't reliably hit that in practice.
    Re-summarizing every article from clean_text with one cheap batched
    DeepSeek call fixes both at once and is more robust than patching either
    the translation step or LazyCrawler's TextRank/sentence-splitter for
    every page layout it might meet. Title and clean_text stay untouched
    (original language, full text) -- only this orientation summary changes.
    """
    targets = [p for p in pages if p.get("clean_text")]
    if not targets:
        return

    from lazybridge import Agent
    from pydantic import BaseModel

    class Summaries(BaseModel):
        summaries: list[str]

    agent = Agent(
        model=DIGEST_MODEL, name="news_index_summarizer", session=cost_session, output=Summaries
    )

    chunk_size = 40
    for start in range(0, len(targets), chunk_size):
        chunk = targets[start : start + chunk_size]
        numbered = "\n".join(
            f"{i + 1}. TITLE: {p.get('title') or '(untitled)'}\n"
            f"   TEXT: {(p.get('clean_text') or '')[:1200]}"
            for i, p in enumerate(chunk)
        )
        prompt = (
            "For each numbered article below, write a clean 2-4 sentence "
            "summary IN ENGLISH, regardless of the article's own language. "
            "Base it on TEXT, not on TITLE alone. Strip out any page chrome "
            "that leaked into TEXT (bylines, 'Published X ago', 'Related "
            "topics', navigation labels) -- summarize only the actual news "
            "content.\n"
            f"Return exactly {len(chunk)} summaries, same order, one per "
            "input item -- no renumbering, no commentary, no merging or "
            "dropping items.\n\n" + numbered
        )
        try:
            env = agent(prompt)
        except Exception:
            continue  # leave this chunk's summaries as extracted rather than fail the run
        if not (env.ok and isinstance(env.payload, Summaries)):
            continue
        summaries = env.payload.summaries
        # strict=False: a length mismatch (the model returning too few/many
        # items) degrades to "some articles keep their original summary"
        # rather than crashing the whole report.
        for p, summary in zip(chunk, summaries, strict=False):
            if summary:
                p["summary"] = summary


def build_digest(pages: list[dict], cost_session=None) -> str:
    from lazybridge import Agent

    agent = Agent(model=DIGEST_MODEL, name="news_digest_writer", session=cost_session)
    prompt = DIGEST_PROMPT.format(n=len(pages), items=_digest_input(pages))
    env = agent(prompt)
    return env.text()


def _usage_from_cost_db(cost_db_path: Path) -> dict:
    """Aggregate token usage/cost straight from the cost DB's raw ``events``
    table instead of ``Session.usage_summary()``: that method scopes its
    query to ``Session.session_id``, a fresh uuid4 generated by every
    ``Session(...)`` construction with no override -- since
    run_news_crawl.py and this script are two separate process
    invocations, each gets its own uuid and would only ever see its own
    half of the events in this shared file. The file itself is already
    scoped to one news-crawl run (its name is ``<session_id>_cost.db``),
    so reading every row in it, ignoring the per-Session session_id
    column entirely, is exactly the right scope."""
    total = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    by_agent: dict[str, dict] = {}
    if not cost_db_path.exists():
        return {"total": total, "by_agent": by_agent}
    con = sqlite3.connect(str(cost_db_path))
    try:
        rows = con.execute(
            "SELECT payload FROM events WHERE event_type='model_response'"
        ).fetchall()
    finally:
        con.close()
    for (payload_json,) in rows:
        p = json.loads(payload_json)
        name = p.get("agent_name") or "unknown"
        in_tok, out_tok, cost = (
            p.get("input_tokens") or 0,
            p.get("output_tokens") or 0,
            p.get("cost_usd") or 0.0,
        )
        total["input_tokens"] += in_tok
        total["output_tokens"] += out_tok
        total["cost_usd"] += cost
        ag = by_agent.setdefault(name, {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})
        ag["input_tokens"] += in_tok
        ag["output_tokens"] += out_tok
        ag["cost_usd"] += cost
    return {"total": total, "by_agent": by_agent}


def build_cost_report(session_id: str, n_articles: int, n_smart: int, cost_db_path: Path) -> str:
    """Cost report for this run: smart-mode extraction (run_news_crawl.py,
    one LLM call per local-language article) + the digest synthesis call
    (this script) -- both logged to the same per-session cost DB."""
    summary = _usage_from_cost_db(cost_db_path)
    total = summary["total"]
    lines = [
        f"# News crawl - run cost - {session_id}",
        "",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        f"Articles: {n_articles} total ({n_smart} via DeepSeek smart-mode, "
        f"{n_articles - n_smart} via no-LLM ml-mode)",
        "",
        f"**Total cost: ${total['cost_usd']:.4f}** "
        f"({total['input_tokens']:,} input tokens, {total['output_tokens']:,} output tokens)",
        "",
        "## By agent",
        "",
        "| Agent | Input tokens | Output tokens | Cost (USD) |",
        "|---|---|---|---|",
    ]
    for name, agent_totals in sorted(summary["by_agent"].items()):
        lines.append(
            f"| {name} | {agent_totals['input_tokens']:,} | "
            f"{agent_totals['output_tokens']:,} | ${agent_totals['cost_usd']:.4f} |"
        )
    if n_articles:
        lines.append("")
        lines.append(
            f"Average per article (crawl + index summary + digest share): "
            f"${total['cost_usd'] / n_articles:.5f}"
        )
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description="Build the news-monitor digest + full report")
    p.add_argument("--db", default=str(DEFAULT_DB))
    p.add_argument("--session-id", help="Defaults to the latest news_crawl session")
    p.add_argument(
        "--no-digest", action="store_true", help="Skip the DeepSeek digest (full report only)"
    )
    args = p.parse_args()

    db = CrawlerDB(DBConfig(db_path=args.db))
    session_id = args.session_id or _latest_session_id(db)
    if not session_id:
        print("No news_crawl session found in the DB.", file=sys.stderr)
        return 1

    pages = db.get_pages(session_id=session_id, status="done")
    db.close()
    if not pages:
        print(f"Session {session_id}: no 'done' pages found.", file=sys.stderr)
        return 1

    meta = _load_meta(session_id)
    pages = _enrich(pages, meta)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    from lazybridge.session import Session

    cost_db_path = REPORT_DIR / f"{session_id}_cost.db"
    cost_session = Session(db=str(cost_db_path))

    generate_index_summaries(pages, cost_session=cost_session)

    by_region = _group_by_region(pages)
    for region, region_pages in sorted(by_region.items()):
        region_path = REPORT_DIR / f"news_full_{session_id}_{region}.md"
        region_path.write_text(
            build_region_report(region, region_pages, session_id), encoding="utf-8"
        )
        print(f"Full report [{region}]: {region_path} ({len(region_pages)} articles)")

    if not args.no_digest:
        digest_text = build_digest(pages, cost_session=cost_session)
        digest_path = REPORT_DIR / f"news_digest_{session_id}.md"
        digest_path.write_text(digest_text, encoding="utf-8")
        print(f"Digest: {digest_path}")

    cost_session.close()
    n_smart = sum(1 for p in pages if p.get("mode") == "smart")
    cost_text = build_cost_report(session_id, len(pages), n_smart, cost_db_path)
    cost_path = REPORT_DIR / f"news_cost_{session_id}.md"
    cost_path.write_text(cost_text, encoding="utf-8")
    print(f"Cost report: {cost_path}")

    print(f"SESSION_ID={session_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
