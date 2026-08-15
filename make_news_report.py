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
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from artifact_registry import register_report_artifact  # noqa: E402
from lazycrawler import CrawlerDB, DBConfig  # noqa: E402

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "news.db"
REPORT_DIR = ROOT / "reports" / "news"
DIGESTS_DB = REPORT_DIR / "digests.db"
DIGEST_MODEL = "deepseek-v4-flash"
#: Recognised --cycle values -- the three scheduled tasks in
#: setup_scheduler.ps1. Kept loose (validated only where it matters, e.g.
#: make_digest_delta_report.py's cycle filter) rather than an enum, since
#: this is also passed for ad-hoc manual runs that have no fixed cycle.
CYCLES = ("morning", "europeclose", "usclose")
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


DIGEST_ENGINES = ("claude", "deepseek")
DEFAULT_DIGEST_ENGINES = ("claude",)


def _digest_agent(engine_name: str, cost_session):
    from lazybridge import Agent

    if engine_name == "claude":
        from lazybridge import ClaudeCodeEngine

        # Runs through the local Claude Code login (Claude.ai subscription),
        # not DEEPSEEK_API_KEY -- see docs/technical-guide.md in
        # LazyBridge for the auth model. web=False: this
        # is a closed-book synthesis over the article summaries already
        # assembled in `items` below -- it should not go browse the web.
        return Agent(
            engine=ClaudeCodeEngine(model="sonnet", web=False),
            name="news_digest_writer_claude",
            session=cost_session,
        )
    if engine_name == "deepseek":
        return Agent(model=DIGEST_MODEL, name="news_digest_writer_deepseek", session=cost_session)
    raise ValueError(f"Unknown digest engine {engine_name!r}; expected one of {DIGEST_ENGINES}")


def build_digest(pages: list[dict], cost_session=None, engine_name: str = "claude") -> str:
    agent = _digest_agent(engine_name, cost_session)
    prompt = DIGEST_PROMPT.format(n=len(pages), items=_digest_input(pages))
    env = agent(prompt)
    return env.text()


def _digest_preview(digest_text: str, max_chars: int = 300) -> str:
    """A cheap-to-read summary for the digest's artifact record: its first
    non-empty, non-heading paragraph, truncated. Falls back to a truncated
    whole-text preview if every line looks like a heading/blank."""
    for para in digest_text.split("\n\n"):
        line = para.strip()
        if line and not line.startswith("#"):
            return line[:max_chars]
    return digest_text.strip()[:max_chars]


_HEADER_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)


def _digest_themes(digest_text: str) -> list[str]:
    """The digest's theme names, in the order it presents them.

    DIGEST_PROMPT asks the model to "group items by theme ... use headers
    per theme", so a cheap regex parse of the digest's own markdown section
    headers recovers the theme names directly -- no extra LLM call needed
    just for the artifact record.
    """
    return [h.strip(" *") for h in _HEADER_RE.findall(digest_text) if h.strip(" *")]


def _digest_summary(digest_text: str, n_articles: int) -> str:
    """Keyword-dense summary for the digest artifact.

    Leads with the actual theme names the digest is grouped by -- the
    single highest-value search term here, since "what themes were covered
    on date X" is the natural query and content is never full-text
    searched. Falls back to a plain text preview if no headers were found
    (e.g. the model didn't use markdown headers this run).
    """
    themes = _digest_themes(digest_text)
    if themes:
        return f"Executive digest of {n_articles} articles grouped by theme: " + ", ".join(themes)
    return _digest_preview(digest_text)


def _region_summary(region: str, region_pages: list[dict]) -> str:
    """Keyword-dense summary for one region's full-report artifact.

    Built entirely from data already computed while assembling the report
    (source names and topics already attached to each page by ``_enrich``/
    crawl-time extraction) -- no extra computation just for the artifact
    record.
    """
    topic_counts: Counter[str] = Counter()
    for p in region_pages:
        topic_counts.update(p.get("topics") or [])
    source_counts = Counter(p.get("source_name") or p.get("domain") for p in region_pages)
    source_counts.pop(None, None)

    parts = [f"{len(region_pages)} articles for region {region}"]
    top_topics = [t for t, _ in topic_counts.most_common(3)]
    if top_topics:
        parts.append("top topics: " + ", ".join(top_topics))
    top_sources = [s for s, _ in source_counts.most_common(3)]
    if top_sources:
        parts.append("top sources: " + ", ".join(top_sources))
    return "; ".join(parts)


def _register_region_artifact(
    session_id: str, region: str, region_pages: list[dict], region_path: Path
) -> None:
    register_report_artifact(
        kind="report",
        title=f"News full report {session_id} ({region})",
        summary=_region_summary(region, region_pages),
        tags=["daily", f"region:{region}"],
        content_uri=str(region_path),
    )


def _register_digest_artifact(
    session_id: str, digest_text: str, digest_path: Path, n_articles: int
) -> None:
    register_report_artifact(
        kind="digest",
        title=f"News digest {session_id}",
        summary=_digest_summary(digest_text, n_articles),
        tags=["daily", "digest"],
        content_uri=str(digest_path),
    )


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


def _init_digests_db(db_path: Path) -> None:
    con = sqlite3.connect(str(db_path))
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS digests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                cycle TEXT,
                engine TEXT NOT NULL,
                produced_at TEXT NOT NULL,
                n_articles INTEGER NOT NULL,
                text TEXT NOT NULL,
                UNIQUE(session_id, engine)
            )
            """
        )
        con.commit()
    finally:
        con.close()


def save_digest_to_db(
    db_path: Path, *, session_id: str, cycle: str | None, engine: str, n_articles: int, text: str
) -> None:
    """Persist one digest run. Idempotent: re-running the same session_id +
    engine (e.g. regenerating tonight's report by hand) updates the
    existing row in place instead of accumulating duplicates -- callers
    that want per-day history (make_digest_delta_report.py) can then just
    take the last N distinct session_ids for a cycle without worrying
    about manual re-runs skewing the count."""
    _init_digests_db(db_path)
    con = sqlite3.connect(str(db_path))
    try:
        con.execute(
            """
            INSERT INTO digests (session_id, cycle, engine, produced_at, n_articles, text)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id, engine) DO UPDATE SET
                cycle=excluded.cycle,
                produced_at=excluded.produced_at,
                n_articles=excluded.n_articles,
                text=excluded.text
            """,
            (
                session_id,
                cycle,
                engine,
                datetime.now().isoformat(timespec="seconds"),
                n_articles,
                text,
            ),
        )
        con.commit()
    finally:
        con.close()


def main() -> int:
    p = argparse.ArgumentParser(description="Build the news-monitor digest + full report")
    p.add_argument("--db", default=str(DEFAULT_DB))
    p.add_argument("--session-id", help="Defaults to the latest news_crawl session")
    p.add_argument(
        "--no-digest", action="store_true", help="Skip the digest step (full report only)"
    )
    p.add_argument(
        "--digest-engines",
        default=",".join(DEFAULT_DIGEST_ENGINES),
        help=(
            "Comma-separated digest engines to run, e.g. 'claude' (default), "
            "'deepseek', or 'claude,deepseek' to generate and send one digest "
            f"per engine for comparison. Choices: {', '.join(DIGEST_ENGINES)}."
        ),
    )
    p.add_argument(
        "--cycle",
        default=None,
        help=(
            "Which scheduled cycle this run belongs to -- stored alongside "
            "each digest in digests.db so make_digest_delta_report.py can "
            f"pull 'the last N usclose digests' precisely. Choices: {', '.join(CYCLES)}. "
            "Omit for ad-hoc manual runs."
        ),
    )
    args = p.parse_args()
    digest_engines = [e.strip() for e in args.digest_engines.split(",") if e.strip()]
    for e in digest_engines:
        if e not in DIGEST_ENGINES:
            print(
                f"Unknown --digest-engines value {e!r}; expected one of {DIGEST_ENGINES}.",
                file=sys.stderr,
            )
            return 2

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
        _register_region_artifact(session_id, region, region_pages, region_path)

    if not args.no_digest:
        # Single engine (the default) keeps the plain, unsuffixed filename
        # for backward compatibility with the scheduled pipeline and
        # send_telegram_news_report.py's existing exact-name lookup.
        # Multiple engines (comparison mode) suffix every digest with its
        # engine name instead, so send_telegram_news_report.py's glob picks
        # up all of them.
        suffix_names = len(digest_engines) > 1
        for engine_name in digest_engines:
            digest_text = build_digest(pages, cost_session=cost_session, engine_name=engine_name)
            suffix = f"_{engine_name}" if suffix_names else ""
            digest_path = REPORT_DIR / f"news_digest_{session_id}{suffix}.md"
            digest_path.write_text(digest_text, encoding="utf-8")
            print(f"Digest [{engine_name}]: {digest_path}")
            _register_digest_artifact(session_id, digest_text, digest_path, len(pages))
            save_digest_to_db(
                DIGESTS_DB,
                session_id=session_id,
                cycle=args.cycle,
                engine=engine_name,
                n_articles=len(pages),
                text=digest_text,
            )

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
