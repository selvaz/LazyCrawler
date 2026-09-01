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
  - news_digest_<session>.md         an executive digest built from the
                                      per-article summaries/sentiment/topics
                                      already extracted at crawl time (ml
                                      TextRank/VADER or smart DeepSeek) --
                                      this call does NOT re-read raw article
                                      text, so it stays a small, cheap
                                      synthesis step regardless of how many
                                      articles were crawled. Structure
                                      depends on (cycle, engine) -- see
                                      `_select_digest_format`: morning/claude
                                      and usclose are grouped by theme,
                                      morning/deepseek by geography, and
                                      europeclose by asset class.

Usage:
    python make_news_report.py
    python make_news_report.py --session-id news_20260723_070000
    python make_news_report.py --no-digest   # full report only, skip DeepSeek
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))

from artifact_registry import register_report_artifact  # noqa: E402
from lazycrawler import CrawlerDB, DBConfig  # noqa: E402

ROOT = Path(__file__).resolve().parent
#: Both resolved from the environment first, falling back to the paths beside
#: this script -- see run_news_crawl.py for why. DIGESTS_DB matters most: it
#: had no command-line override at all, so unlike the news database it could
#: not even be repointed by the caller, and every digest written from a pinned
#: runtime worktree became invisible to readers of the declared path.
DEFAULT_DB = Path(os.environ.get("LAZYCRAWLER_NEWS_DB") or ROOT / "news.db")
REPORT_DIR = ROOT / "reports" / "news"
DIGESTS_DB = Path(os.environ.get("DIGESTS_DB") or REPORT_DIR / "digests.db")
DIGEST_MODEL = "deepseek-v4-flash"
#: Recognised --cycle values -- the three scheduled tasks in
#: setup_scheduler.ps1. Kept loose (validated only where it matters, e.g.
#: make_digest_delta_report.py's cycle filter) rather than an enum, since
#: this is also passed for ad-hoc manual runs that have no fixed cycle.
CYCLES = ("morning", "europeclose", "usclose")
UNKNOWN_REGION = "unclassified"
#: The scheduler runs the morning cycle at 23:00 on this host's own (Pacific)
#: clock so it lands at 07:00 in Ireland (see setup_scheduler.ps1) -- meaning
#: the host's local calendar date is still "yesterday" at the moment it fires.
#: ClaudeCodeEngine has no other source of "today" -- the Claude Code CLI it
#: launches reads its own process's local OS clock -- so left alone it wrote
#: the wrong day into the digest. DeepSeek's plain API call carries no such
#: local-machine date at all, which is why only the Claude digest showed this.
DIGEST_REFERENCE_TZ = ZoneInfo("Europe/Dublin")

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

#: The fixed section skeletons, named once and shared by the prompt that
#: asks for them and the header that announces them. A reader gets the same
#: sections in the same order every day, and when one is missing that is
#: information ("nothing material in Credit today") rather than the model
#: having reorganised the report.
GEO_COUNTRY_SECTIONS = (
    "Top stories",
    "US",
    "Europe",
    "Asia",
    "MENA",
    "Africa",
    "Latin America",
)
ASSET_CLASS_SECTIONS = (
    "Cross-asset / Geopolitical",
    "Rates & Central Banks",
    "Equities",
    "FX",
    "Commodities",
    "Credit",
)

#: Shared verbatim by both fixed-skeleton prompts, so the two cannot drift
#: apart on the one rule that decides whether the report can be trusted at
#: all. A digest is read as a record of what was published; a plausible
#: number the model supplied itself is indistinguishable from a reported
#: one once it is on the page, and downstream it would travel into
#: digests.db, into the delta report, and into the committee's context as
#: fact. Inference stays allowed -- and stays labelled: the desk rule this
#: encodes is "never invent data, hypotheses yes if well-founded".
NO_FABRICATION_RULE = """\
- NEVER invent anything. Every fact, figure, price, percentage, date, name,
  institution and quotation you write must come from the news items below.
  If the items do not give a number or a detail, say it was not reported --
  do not supply a plausible one, do not round or extrapolate one, and do not
  fill a gap from your own background knowledge or from what such a story
  usually contains. If the items conflict, report the disagreement rather
  than resolving it into a single invented figure.
- Do not report an event that is not in the items, however likely it seems
  as a consequence of what is. Do not upgrade a report, a plan, an
  expectation or a proposal into an accomplished fact: keep the items' own
  hedging ("expected", "reportedly", "due today") instead of dropping it.
- Analysis and inference ARE allowed and wanted -- reading transmission,
  weighing significance, connecting two items -- provided they rest on what
  the items actually say and are written as your reading rather than as
  reported fact. Attribute a claim to the source that made it whenever it
  is that source's assertion rather than an established fact.
"""

#: morning/deepseek only -- see `_select_digest_format`. Deliberately
#: different from DIGEST_PROMPT rather than a second run of the same
#: synthesis: two engines writing the same theme-grouped digest on the same
#: article pool produced near-duplicate reading, not a second opinion. This
#: gives the morning cycle two genuinely different views of the same crawl
#: instead.
GEO_COUNTRY_DIGEST_PROMPT = (
    """\
You are a buy-side macro/portfolio analyst preparing a same-day briefing for
a portfolio manager who allocates across asset classes and regions. Below is
a list of news items crawled in the last cycle (title, source, sentiment,
topics, short summary) from financial wires, central banks, and geopolitical
outlets spanning developed and emerging markets, including local-language
sources translated at crawl time.

Write a concise executive digest in Markdown, organised geographically
rather than by theme.

STRUCTURE -- follow it exactly. Use these section headings, spelled exactly
as written, as second-level Markdown headings (`## `), in this order:

## Top stories
## US
## Europe
## Asia
## MENA
## Africa
## Latin America

Do not add sections, rename them, reorder them, or nest them differently.
Do not write a title, a preamble, or a closing summary -- the document
already has a header, so begin your output directly with `## Top stories`.

Within the structure:
- "Top stories": 3-6 bullets, the single most important developments across
  all regions today, regardless of where they happened.
- Every other section: use third-level headings (`### `) named after a
  COUNTRY (`### Japan`, `### Germany`) for country-specific stories, and
  exactly `### Regional` for cross-border items that belong to no single
  country. Do not name a third-level heading after a theme, a sector or an
  event -- `### Rates`, `### Politics` and `### Other` are all wrong;
  country name or `### Regional`, nothing else.
- Within each country/region group, lead with whatever is most likely to
  matter for asset allocation, and note the prevailing sentiment/tone.
- Do not repeat a story across sections, and do not repeat it twice inside
  one section: place it once, where it originated, and cross-reference
  briefly (e.g. "see US") if it has material knock-on effects elsewhere.
- Include an item only if it plausibly matters to a portfolio manager
  allocating across asset classes and regions. Obituaries, local crime,
  sport, weather nuisance, transport accidents and domestic human-interest
  stories do not qualify, however prominent in the crawl -- leave them out
  entirely rather than filing them under a catch-all heading.
- If nothing in today's crawl is material for a section, keep the heading
  and write exactly `_Nothing material in this cycle._` under it. Never pad
  a section just to fill it.
- Write finished prose only. Never think out loud, never correct yourself
  mid-sentence, never pose a question to yourself, and never comment on
  whether an item belongs in a section -- decide, then write the result.
- Be dense and factual, no filler, no restating the obvious.
"""
    + NO_FABRICATION_RULE
    + """
News items ({n} total):
{items}
"""
)

#: europeclose only -- see `_select_digest_format`. Section order is fixed
#: and Cross-asset/Geopolitical comes first: that bucket is where the
#: single most consequential story of a cycle usually lands (an event
#: moving several asset classes at once), and reading it after five other
#: sections buried it on the days it mattered most. Written with the
#: Investment Committee's newsfeed context in mind (only usclose is read
#: there today, but this format is deliberately the one a desk could route
#: by asset class without re-parsing free prose), while staying disciplined
#: about not crossing into investment advice.
#:
#: It carries no NEW/UPDATED tagging, deliberately. An earlier draft did,
#: borrowed from `make_digest_delta_report.py` -- but that report is handed
#: an explicit baseline of recent digests to diff against, and this one is
#: shown a single cycle and nothing else. Asked to tag anyway, the model
#: produced things like "[UPDATED: market pricing shift since Friday]",
#: which reads as a comparison against a previous report it was never
#: given. Novelty against recent coverage is the delta job's question, and
#: it is the only one holding the evidence to answer it.
ASSET_CLASS_DIGEST_PROMPT = (
    """\
You are a buy-side macro/portfolio analyst preparing a same-day briefing for
a portfolio manager and, downstream, for an investment committee's tactical
research process. Below is a list of news items crawled in the last cycle
(title, source, sentiment, topics, short summary) from financial wires,
central banks, and geopolitical outlets spanning developed and emerging
markets, including local-language sources translated at crawl time.

Write a concise executive digest in Markdown, organised by asset class
rather than by theme or region.

STRUCTURE -- follow it exactly. Use these section headings, spelled exactly
as written, as second-level Markdown headings (`## `), in this order:

## Cross-asset / Geopolitical
## Rates & Central Banks
## Equities
## FX
## Commodities
## Credit

Do not add sections, rename them, reorder them, or nest them differently.
Do not write a title, a preamble, or a closing summary -- the document
already has a header, so begin your output directly with
`## Cross-asset / Geopolitical`. That first section is for macro and
geopolitical developments that do not map cleanly onto one asset class, or
that move several at once.

Within each section:
- List only the most important developments, not every article that
  technically fits. Prioritise what the items themselves report as a
  surprise against expectations (a print against its consensus, a decision
  against what was signalled) and what has a plausible transmission channel
  into that asset class.
- Write each item as a single bullet, in this exact shape:
    - **Headline of the development** -- the facts, with numbers and named
      sources where the crawl gives them.
      Impact: direction=<increase|decrease|steepen|flatten|widen|tighten|
      mixed|indeterminate>, magnitude=<low|moderate|high>,
      confidence=<low|medium|high>
  Every item gets exactly one Impact line, with all three fields present,
  using only the listed values. This is a conditional, disciplined read of
  transmission, not a recommendation: never use buy/sell/hold,
  overweight/underweight, position sizing, price targets, or entry/exit
  levels. Market numbers are allowed only as documented facts or attributed
  external forecasts, never as your own price prediction.
- This is a single cycle's snapshot, and you have not been shown any earlier
  report. Do not label an item as new, updated, unchanged or continuing
  relative to previous coverage, and do not describe what has "changed
  since" some earlier moment -- you have no baseline and would be inventing
  one. Deciding what is genuinely new against recent coverage is a separate
  report's job. Where an item's own source describes it as a continuation
  ("a sixth consecutive day of strikes", "up from 2.9% in July"), report
  that, because it is the source speaking rather than a comparison you made.
- If two developments push the same asset class in different directions
  (e.g. geopolitical risk-off vs. a hawkish central bank both touching
  gold), say so explicitly instead of collapsing them into one call -- flag
  the conflict rather than silently picking a winner.
- If nothing in today's crawl is material for a section, keep the heading
  and write exactly `_Nothing material in this cycle._` under it. Never pad
  a section with weak or tangential items just to fill it.
- Be dense and factual, no filler, no restating the obvious.
"""
    + NO_FABRICATION_RULE
    + """
The Impact line is subject to the same rule: it is your reading of
transmission, never a reported figure, and its confidence field must fall to
`low` when the items support the direction only weakly. Never state a price
level, a spread or a yield in an Impact line unless the items reported it.

News items ({n} total):
{items}
"""
)


#: The three digest formats, each as (prompt template, section skeleton).
#: ``None`` sections means the format has no fixed skeleton -- the original
#: thematic digest lets the model choose its own themes, and that is the
#: point of it.
DIGEST_FORMATS = {
    "thematic": (DIGEST_PROMPT, None),
    "geographic": (GEO_COUNTRY_DIGEST_PROMPT, GEO_COUNTRY_SECTIONS),
    "asset-class": (ASSET_CLASS_DIGEST_PROMPT, ASSET_CLASS_SECTIONS),
}


def _select_digest_format(cycle: str | None, engine_name: str) -> str:
    """Which of ``DIGEST_FORMATS`` writes this (cycle, engine) digest.

    Keyed on cycle first, not engine: europeclose gets the asset-class
    format regardless of which engine ever runs it, and it is cycle --
    not engine -- that decides what a digest is *for*. The one
    engine-level split is morning/deepseek, so the two engines that both
    run that cycle stop writing near-duplicates of each other. Everything
    else (morning/claude, usclose, any ad-hoc manual run with no cycle)
    keeps the original theme-grouped digest -- usclose especially, since
    `make_digest_delta_report.py` reads it as the baseline for the delta
    report and expects that shape, and `newsfeed.py` in the Investment
    Committee package already reads it as-is.
    """
    if cycle == "europeclose":
        return "asset-class"
    if cycle == "morning" and engine_name == "deepseek":
        return "geographic"
    return "thematic"


def _session_date(session_id: str) -> str:
    """The date the crawl ran, read off its own session id.

    Session ids are ``news_YYYYMMDD_HHMMSS``, so the crawl's date is already
    carried by the thing being reported on. Dating the masthead from the
    clock instead would be right only while the report is built straight
    after its crawl: ``--session-id`` exists precisely to rebuild an older
    session, and that run would have stamped today onto a report of last
    week's news -- and persisted it, since the header goes to the file and
    into digests.db.

    Falls back to today in Europe/Dublin for an id that does not carry a
    parseable date (an ad-hoc session named by hand). Dublin rather than
    the host clock for the reason DIGEST_REFERENCE_TZ documents: this host
    still reads yesterday when the morning cycle fires.
    """
    part = session_id.removeprefix("news_").split("_")[0]
    try:
        return datetime.strptime(part, "%Y%m%d").date().isoformat()
    except ValueError:
        return datetime.now(DIGEST_REFERENCE_TZ).date().isoformat()


def _digest_header(
    session_id: str, *, cycle: str | None, engine_name: str, n_articles: int, digest_format: str
) -> str:
    """The report's masthead, written here rather than asked of the model.

    A model told to "start with a title and some metadata" writes a
    slightly different one every day -- different date format, different
    field order, the article count occasionally wrong. Everything here is
    known to this process, so none of it is worth a token of model
    attention or a day's drift. The prompts for the fixed-skeleton formats
    tell the model this header exists and to start at its first section.
    """
    _, sections = DIGEST_FORMATS[digest_format]
    date = _session_date(session_id)
    cycle_label = cycle or "ad-hoc"
    lines = [
        f"# News digest — {cycle_label} — {date}",
        "",
        f"**Structure:** {digest_format} | **Session:** `{session_id}` | "
        f"**Engine:** {engine_name} | **Articles:** {n_articles}",
    ]
    if sections:
        lines += [
            "",
            "**Sections:** "
            + " · ".join(sections)
            + ". A section with nothing material in this cycle says so rather than "
            "being dropped or padded.",
        ]
    lines += ["", "---", ""]
    return "\n".join(lines)


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
    """One line per article for the digest prompt.

    Carries ``region`` -- the curated area from ``lazycrawler_sources.yaml``
    via ``_enrich()``, not something the model has to re-derive from the
    source name or article text -- because GEO_COUNTRY_DIGEST_PROMPT groups
    by it directly. ``source_name`` (the curated label, e.g. "LiveMint -
    Economy") replaces the bare domain for the same reason: more signal for
    an editorial grouping than a raw hostname.
    """
    lines = []
    for p in pages:
        topics = ", ".join((p.get("topics") or [])[:6])
        summary = (p.get("summary") or "")[:400]
        source = p.get("source_name") or p.get("domain")
        region = p.get("region") or "n/a"
        lines.append(
            f"- [{region}/{source}] {p.get('title')} | sentiment={p.get('sentiment')} "
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

    # Guarded like the per-chunk call below, and for the same reason. The
    # engine this now builds resolves its provider eagerly, so an absent
    # DEEPSEEK_API_KEY raises *here* rather than at the first call -- which
    # would take the whole report down, when the contract of this function
    # has always been that a summariser it cannot use leaves the extracted
    # summaries in place and lets the regional reports be written anyway.
    try:
        agent = Agent(
            engine=_bounded_engine(),
            name="news_index_summarizer",
            session=cost_session,
            output=Summaries,
        )
    except Exception:
        return

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


#: Per-HTTP-operation deadline (seconds) for every DeepSeek call this script
#: makes.  Normal calls here finish in 15-30s.
_HTTP_TIMEOUT_SECONDS = 90.0


def _bounded_engine():
    """An LLMEngine whose DeepSeek calls cannot hang indefinitely.

    LazyBridge's own ``request_timeout`` is an ``asyncio.wait_for`` around the
    provider call, and on 2026-08-26 that deadline demonstrably failed to
    fire: a ``news_index_summarizer`` call ran for 5626 seconds and then
    returned *successfully*, with ``request_timeout=120.0`` in force and no
    error or retry recorded.  An asyncio deadline can only act if the
    cancellation it requests is actually honoured; a timeout on the HTTP
    client acts one layer down, aborting the socket operation itself, so it
    does not depend on that.  Both are kept -- ``request_timeout`` still
    covers the cheap case, this covers the case that got us.

    ``max_retries=0`` disables the *OpenAI SDK's* own retries so they cannot
    silently multiply LazyBridge's; LazyBridge remains the single owner of
    retry policy.
    """
    from lazybridge import LLMEngine
    from lazybridge.core.providers import DeepSeekProvider

    provider = DeepSeekProvider(
        model=DIGEST_MODEL,
        timeout=_HTTP_TIMEOUT_SECONDS,
        max_retries=0,
    )
    # provider= is typed ``str | None`` upstream but accepts a constructed
    # provider; this is the only way to reach the client's own timeout from
    # outside LazyBridge today.
    return LLMEngine(DIGEST_MODEL, provider=provider)  # type: ignore[arg-type]


def _digest_agent(engine_name: str, cost_session):
    from lazybridge import Agent

    if engine_name == "claude":
        from lazybridge import ClaudeCodeEngine

        # Runs through the local Claude Code login (Claude.ai subscription),
        # not DEEPSEEK_API_KEY -- see docs/technical-guide.md in
        # LazyBridge for the auth model. web=False: this
        # is a closed-book synthesis over the article summaries already
        # assembled in `items` below -- it should not go browse the web.
        #
        # system=: overrides the Claude Code CLI's own self-reported date,
        # which otherwise comes from this host's local OS clock (Pacific) --
        # see DIGEST_REFERENCE_TZ above for why that clock reads "yesterday"
        # every morning run.
        today = datetime.now(DIGEST_REFERENCE_TZ).date().isoformat()
        return Agent(
            engine=ClaudeCodeEngine(
                model="sonnet",
                web=False,
                system=(
                    f"Today's date is {today} (Europe/Dublin). Use this as "
                    "'today' for this report, not any other date your "
                    "environment may suggest."
                ),
                # This writer sat at 63-78s for a week, then ran 89.2s and
                # 112.3s on 2026-08-26 -- 94% of the 120s default it was
                # silently taking.  The next slow run would not have been a
                # slow digest, it would have been no digest.  300s is ~2.7x
                # the observed maximum: wide enough that ordinary variance
                # cannot reach it, and the outer process deadline (the job
                # runner's timeout_hours tree-kill) is the real guarantee
                # anyway, so a tight bound here buys nothing and costs runs.
                #
                # This deadline covers the whole retry loop rather than each
                # attempt, so max_retries does not multiply it.
                request_timeout=300.0,
                max_retries=3,
            ),
            name="news_digest_writer_claude",
            session=cost_session,
        )
    if engine_name == "deepseek":
        return Agent(
            engine=_bounded_engine(),
            name="news_digest_writer_deepseek",
            session=cost_session,
        )
    raise ValueError(f"Unknown digest engine {engine_name!r}; expected one of {DIGEST_ENGINES}")


def build_digest(
    pages: list[dict], cost_session=None, engine_name: str = "claude", cycle: str | None = None
) -> str:
    """The model's digest body, without the header -- see ``_digest_header``.

    Returned bare so the caller decides whether to prepend the header: the
    thematic format is read by `make_digest_delta_report.py` and by the
    Investment Committee's `newsfeed.py`, both of which were built against
    the un-headed text, so it stays exactly as it was.
    """
    agent = _digest_agent(engine_name, cost_session)
    prompt_template, _ = DIGEST_FORMATS[_select_digest_format(cycle, engine_name)]
    prompt = prompt_template.format(n=len(pages), items=_digest_input(pages))
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
        return f"Executive digest of {n_articles} articles covering: " + ", ".join(themes)
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

    digest_failures: list[str] = []
    if not args.no_digest:
        # Single engine (the default) keeps the plain, unsuffixed filename
        # for backward compatibility with the scheduled pipeline and
        # send_telegram_news_report.py's existing exact-name lookup.
        # Multiple engines (comparison mode) suffix every digest with its
        # engine name instead, so send_telegram_news_report.py's glob picks
        # up all of them.
        suffix_names = len(digest_engines) > 1
        for engine_name in digest_engines:
            digest_format = _select_digest_format(args.cycle, engine_name)
            digest_body = build_digest(
                pages, cost_session=cost_session, engine_name=engine_name, cycle=args.cycle
            )
            # A failed model call comes back as an empty envelope, not an
            # exception -- on 2026-09-01 DeepSeek answered 402 Insufficient
            # Balance and this loop wrote a zero-length digest to disk and
            # to digests.db without a word. An empty row there is worse
            # than a missing one: `newsfeed.py` would hand it to the
            # committee as that cycle's news, and
            # `make_digest_delta_report.py` would diff against nothing.
            # Skip the write and remember the failure for the exit code.
            if not digest_body.strip():
                print(
                    f"DIGEST FAILED [{engine_name}, {digest_format}]: the engine returned "
                    f"no text; nothing written to {DIGESTS_DB.name} for this engine.",
                    file=sys.stderr,
                )
                digest_failures.append(engine_name)
                continue
            # The thematic digest is left exactly as the model wrote it:
            # `make_digest_delta_report.py` reads it as the delta baseline
            # and `newsfeed.py` feeds it to the committee, both built
            # against the un-headed text. The two fixed-skeleton formats
            # are new, so nothing downstream has an opinion about them yet.
            digest_text = (
                digest_body
                if digest_format == "thematic"
                else _digest_header(
                    session_id,
                    cycle=args.cycle,
                    engine_name=engine_name,
                    n_articles=len(pages),
                    digest_format=digest_format,
                )
                + digest_body
            )
            suffix = f"_{engine_name}" if suffix_names else ""
            digest_path = REPORT_DIR / f"news_digest_{session_id}{suffix}.md"
            digest_path.write_text(digest_text, encoding="utf-8")
            print(f"Digest [{engine_name}, {digest_format}]: {digest_path}")
            # Summarised from the body, not the headed text: the header's
            # own `# News digest -- ...` line is a Markdown heading and
            # would otherwise be picked up by `_digest_themes` as the
            # report's first theme.
            _register_digest_artifact(session_id, digest_body, digest_path, len(pages))
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

    # Printed before the exit code is decided, so the regional reports and
    # the cost report -- which were written and are still worth having --
    # are reported as done even on a digest failure.
    print(f"SESSION_ID={session_id}")
    if digest_failures:
        print(
            f"One or more digests failed: {', '.join(digest_failures)}.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
