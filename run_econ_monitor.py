# -*- coding: utf-8 -*-
"""run_econ_monitor.py -- scheduled daily check for fresh official-source
economic-indicator releases (see econ_indicators.py for the tracked list and
why each one is there).

For every tracked indicator: fetch its latest observation from BLS/BEA/Census
directly (no calendar to parse -- a release is "new" simply when its period
is newer than the last one we recorded). For anything new today: search for
English-language commentary (DuckDuckGo, ml-mode extraction -- no LLM cost),
then save a reproducible JSON+HTML report (same pattern as market-data-hub's
ETF/regime daily reports: raw JSON row is the source of truth, econ_report.
render_html(row) is a pure function of it) and, only if something is
actually new, send it to Telegram.

Usage:
    python run_econ_monitor.py                  # full run
    python run_econ_monitor.py --dry-run         # skip Telegram send
    python run_econ_monitor.py --skip-search     # skip commentary search (faster testing)
    python run_econ_monitor.py --diagnose-census resconst   # list valid category/data_type codes
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent))

from artifact_registry import register_report_artifact  # noqa: E402
from econ_fetch import EconFetchError, Observation, diagnose_census, fetch_latest  # noqa: E402
from econ_indicators import INDICATORS, EconIndicator  # noqa: E402
from econ_report import render_html  # noqa: E402
from econ_state import EconState  # noqa: E402

ROOT = Path(__file__).resolve().parent
REPORT_DIR = ROOT / "reports" / "econ"
PRODUCED_BY = "lazycrawler.econ_monitor"
SEARCH_MAX_RESULTS = 3
SEARCH_TIMELIMIT = "w"


def _format_change(
    indicator: EconIndicator, value: float, prev_value: float | None
) -> tuple[str | None, str | None]:
    """(change_display, change_sign) -- an indicator whose own unit is
    already a percentage (e.g. GDP % change) gets a percentage-point diff,
    not a nonsensical "percent change of a percent"; everything else gets a
    plain relative percent change."""
    if prev_value is None:
        return None, None
    diff = value - prev_value
    sign = "pos" if diff >= 0 else "neg"
    if "%" in indicator.unit:
        return f"{diff:+.2f} pp", sign
    if prev_value != 0:
        return f"{diff / abs(prev_value) * 100:+.2f}%", sign
    return f"{diff:+,.1f}", sign


def _search_commentary(indicator: EconIndicator, obs: Observation, max_results: int) -> list[dict]:
    """English-language commentary on today's release, via LazyCrawler's own
    DuckDuckGo search + ml-mode extraction (TextRank summary, VADER
    sentiment -- no LLM cost). Best-effort: an empty list means "nothing
    found", not a failure -- the report shows that plainly rather than
    blocking on it."""
    from lazycrawler import CrawlerConfig
    from lazycrawler.config import SearchConfig
    from lazycrawler.search import WebSearch

    query = f'"{indicator.name}" {obs.period} report reaction analysis'
    try:
        with WebSearch(
            search_cfg=SearchConfig(engine="duckduckgo", timelimit=SEARCH_TIMELIMIT),
            crawler_cfg=CrawlerConfig(max_depth=0, respect_robots=True, strict=False),
        ) as search:
            result = search.run(query, mode="ml", max_results=max_results)
    except Exception as exc:  # noqa: BLE001 - a bad search must not abort the run
        print(f"  [{indicator.key}] commentary search failed (non-fatal): {exc}", file=sys.stderr)
        return []

    commentary = []
    for r in result.get("results") or []:
        if r.status != "done" or not r.title:
            continue
        commentary.append(
            {
                "title": r.title,
                "url": r.url,
                "source_domain": urlparse(r.url).netloc,
                "summary": (r.summary or "")[:300] or None,
                "sentiment": r.sentiment,
            }
        )
    return commentary


def _run(
    *, skip_search: bool, search_max_results: int, state_db: Path, dry_run: bool = False
) -> dict:
    state = EconState(state_db)
    today = date.today()

    new_releases: list[dict] = []
    all_status: list[dict] = []
    n_errors = 0

    for indicator in INDICATORS:
        try:
            observations = fetch_latest(indicator)
        except EconFetchError as exc:
            print(f"[{indicator.key}] FETCH ERROR: {exc}", file=sys.stderr)
            n_errors += 1
            all_status.append(
                {
                    "key": indicator.key,
                    "name": indicator.name,
                    "tier": indicator.tier,
                    "agency": indicator.agency,
                    "status": "error",
                    "error_msg": str(exc),
                    "is_new_today": False,
                    "last_period": None,
                    "last_value": None,
                }
            )
            continue

        latest = observations[0]
        prev = observations[1] if len(observations) > 1 else None
        # New period, or the same period with a different number.
        #
        # BEA publishes a quarter's GDP three times -- advance, second, third --
        # under one TimePeriod, revising the value. Comparing periods only, the
        # second and third look like a quarter already seen and were dropped,
        # although the indicator set asks for each of them by name.
        seen = state.last_seen(indicator.key)
        if seen is None:
            is_new = True
        else:
            seen_period, seen_value = seen
            this_period = latest.period_date.isoformat()
            is_new = this_period > seen_period or (
                this_period == seen_period and latest.value != seen_value
            )

        print(
            f"[{indicator.key}] latest={latest.period} value={latest.value} "
            f"{'(NEW)' if is_new else '(no change)'}"
        )

        all_status.append(
            {
                "key": indicator.key,
                "name": indicator.name,
                "tier": indicator.tier,
                "agency": indicator.agency,
                "status": "ok",
                "error_msg": None,
                "is_new_today": is_new,
                "last_period": latest.period,
                "last_value": latest.value,
            }
        )

        if is_new:
            commentary = (
                [] if skip_search else _search_commentary(indicator, latest, search_max_results)
            )
            change_display, change_sign = _format_change(
                indicator, latest.value, prev.value if prev else None
            )
            new_releases.append(
                {
                    "key": indicator.key,
                    "name": indicator.name,
                    "tier": indicator.tier,
                    "agency": indicator.agency,
                    "unit": indicator.unit,
                    "why_it_matters": indicator.why_it_matters,
                    "period": latest.period,
                    "value": latest.value,
                    "prev_value": prev.value if prev else None,
                    "change_display": change_display,
                    "change_sign": change_sign,
                    "commentary": commentary,
                }
            )
            if not dry_run:
                state.mark_seen(
                    indicator.key, latest.period_date.isoformat(), latest.period, latest.value
                )

    payload = {
        "as_of": today.isoformat(),
        "new_releases": new_releases,
        "all_indicators": all_status,
        "summary": {
            "n_indicators": len(INDICATORS),
            "n_new_today": len(new_releases),
            "n_errors": n_errors,
        },
        "provenance": {
            "sources": ["BLS", "BEA", "Census Bureau"],
            "detection": (
                "reactive: a release counts as 'new' when its reported period is newer than "
                "the last one recorded for that indicator -- no release calendar is parsed"
            ),
        },
    }
    return {
        "kind": "econ_daily_report",
        "produced_by": PRODUCED_BY,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cadence": "daily",
        "payload": payload,
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Daily official-source economic release monitor")
    p.add_argument(
        "--dry-run", action="store_true", help="Build + save the report but skip Telegram"
    )
    p.add_argument(
        "--skip-search",
        action="store_true",
        help="Skip the commentary search step (faster testing)",
    )
    p.add_argument("--search-max-results", type=int, default=SEARCH_MAX_RESULTS)
    p.add_argument(
        "--state-db", help="Override the state DB path (default: econ_state.db next to this script)"
    )
    p.add_argument(
        "--diagnose-census",
        metavar="PROGRAM",
        help="List every (category_code, data_type_code) Census actually returns for PROGRAM, then exit",
    )
    args = p.parse_args()

    if args.diagnose_census:
        try:
            pairs = diagnose_census(args.diagnose_census)
        except EconFetchError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        print(
            f"{len(pairs)} distinct (category_code, data_type_code) pairs for {args.diagnose_census!r}:"
        )
        for cat, dtype in pairs:
            print(f"  category_code={cat!r}  data_type_code={dtype!r}")
        return 0

    from econ_state import DEFAULT_STATE_DB

    state_db = Path(args.state_db) if args.state_db else DEFAULT_STATE_DB
    row = _run(
        skip_search=args.skip_search,
        search_max_results=args.search_max_results,
        state_db=state_db,
        dry_run=args.dry_run,
    )

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    today_str = row["payload"]["as_of"]
    json_path = REPORT_DIR / f"econ_daily_{today_str}.json"
    json_path.write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved: {json_path}")

    html_path = json_path.with_suffix(".html")
    html_path.write_text(render_html(row), encoding="utf-8")
    print(f"Rendered: {html_path}")

    summary = row["payload"]["summary"]
    new_names = ", ".join(r["name"] for r in row["payload"]["new_releases"])
    register_report_artifact(
        kind="report",
        title=f"Economic Release Monitor {today_str}",
        summary=(
            f"{summary['n_new_today']} new release(s) of {summary['n_indicators']} tracked"
            + (f": {new_names}" if new_names else "")
        ),
        tags=["daily", "econ"],
        content_uri=str(html_path),
    )

    if summary["n_new_today"] == 0:
        print("Nothing new today -- skipping Telegram send.")
        return 0

    if args.dry_run:
        print(f"[dry-run] Would send Telegram: {summary['n_new_today']} new release(s)")
        return 0

    import os

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print(
            "Telegram not configured (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID unset) -- skipping send."
        )
        return 0

    from lazytools.connectors.telegram import TelegramClient

    with TelegramClient.from_token(token) as client:
        client.send_document(
            chat_id=chat_id,
            document=html_path.read_bytes(),
            filename=html_path.name,
            caption=f"Economic Release Monitor {today_str}: {new_names}"[:1024],
        )
    print("Sent Telegram document.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
