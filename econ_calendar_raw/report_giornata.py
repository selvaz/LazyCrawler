# -*- coding: utf-8 -*-
"""Daily release report from the calendar in DuckDB.

Ordered by criticality rather than by time of day: the point of the report is
not to narrate the session but to say what deserves attention. A surprise is
shown only where a real consensus exists, and the consensus source is always
named.
"""
import argparse
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import duckdb

sys.path.insert(0, r'C:/Users/Administrator/Documents/GitHub/market-data-hub')
sys.path.insert(0, str(Path(__file__).parent))

MARK = {'T1': '\u25cf', 'T2': '\u25cb', 'T3': '\u00b7'}
AREAS = {'US': 'United States', 'CN': 'China', 'EZ': 'Euro area', 'UK': 'United Kingdom',
         'JP': 'Japan', 'IN': 'India', 'MX': 'Mexico', 'BR': 'Brazil',
         'CA': 'Canada', 'AU': 'Australia', 'KR': 'South Korea', 'TW': 'Taiwan'}

# One row per release, with the latest enrichment attached where one exists.
QUERY = """
    SELECT i.criticality, i.area, i.name, i.agency, i.rationale,
           e.reference_period, e.actual, e.consensus, e.previous,
           e.consensus_source, e.actual_num, e.consensus_num,
           e.consensus_low, e.consensus_high, e.consensus_n,
           e.n_sources, e.values_agree,
           strftime(e.release_utc, '%H:%M') AS ora,
           n.commentary_json, n.drivers, n.components, n.technical_source,
           n.model, n.reviewer
    FROM calendar_events e
    JOIN calendar_indicators i USING (indicator_key)
    LEFT JOIN (
        SELECT *, row_number() OVER (PARTITION BY event_id
                                     ORDER BY generated_at DESC) AS rn
        FROM calendar_event_notes
    ) n ON n.event_id = e.event_id AND n.rn = 1
    WHERE e.release_utc::date = ? AND e.status = 'released'
    ORDER BY i.criticality, e.release_utc
"""


def day_rows(con, day: date) -> list[dict]:
    cur = con.execute(QUERY, [day])
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def _stessa(a: float, b: float, toll: float = 0.02) -> bool:
    """Two expectations are the same number, allowing for rounding."""
    return abs(a - b) <= toll * max(abs(a), abs(b), 1e-9)


def compose_text(day: date, rows: list[dict]) -> str:
    """Short caption for the message that carries the HTML report."""
    if not rows:
        return f"Macro calendar \u2014 {day:%d %b %Y}\n\nNo watchlist release."

    n_t1 = sum(1 for r in rows if r['criticality'] == 'T1')
    out = [f"Macro calendar \u2014 {day:%d %b %Y}", "",
           f"{len(rows)} releases, {n_t1} critical.", ""]

    tier = None
    for r in rows:
        if r['criticality'] != tier:
            tier = r['criticality']
            heading = {'T1': 'CRITICAL', 'T2': 'NOTABLE', 'T3': 'CONTEXT'}.get(tier, tier)
            out.append(f"\u2014 {heading} \u2014")
        head = f"{MARK.get(tier, '')} {AREAS.get(r['area'], r['area'])} \u00b7 {r['name']}"
        if r['reference_period'] and r['reference_period'] not in ('-', 'N/D'):
            head += f" ({r['reference_period']})"
        out.append(head)

        lo, hi = r.get('consensus_low'), r.get('consensus_high')
        disperse = lo is not None and hi is not None and not _stessa(lo, hi)

        bits = [f"  {r['ora']} UTC"]
        if r['actual']:
            bits.append(f"actual {r['actual']}")
        if disperse:
            bits.append(f"cons. {lo:g}-{hi:g}")     # una cifra sola qui sarebbe una scelta arbitraria
        elif r['consensus']:
            bits.append(f"cons. {r['consensus']}")
        if r['previous']:
            bits.append(f"prev. {r['previous']}")
        out.append("  \u00b7 ".join(bits) if len(bits) > 1 else bits[0])

        if disperse:
            # Le fonti non concordano sull'attesa: l'intervallo e' l'informazione,
            # e una sorpresa calcolata su uno dei due estremi sarebbe inventata.
            out.append(f"  expectations ranged {lo:g} to {hi:g} across "
                       f"{r.get('consensus_n')} sources - surprise not computable")
        elif r['actual_num'] is not None and r['consensus_num'] is not None:
            delta = r['actual_num'] - r['consensus_num']
            way = "above" if delta > 0 else "below" if delta < 0 else "in line with"
            gap = f"{delta:+,.0f}" if abs(delta) >= 1000 else f"{delta:+.2f}"
            out.append(f"  surprise {gap} ({way} consensus, from {r['consensus_source']})")
        if r['values_agree'] is False:
            out.append(f"  ! {r['n_sources']} sources disagree on the value")
        if r.get('commentary_json') or r.get('drivers'):
            out.append("  + press coverage and technical detail in the report")
        out.append("")

    return "\n".join(out).rstrip()


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--db', default='prova_integrata.duckdb')
    p.add_argument('--day', default=str(date.today() - timedelta(days=1)))
    p.add_argument('--send', action='store_true', help='send to Telegram')
    p.add_argument('--html', default=None, help='report path (default: reports/econ/)')
    args = p.parse_args()

    day = date.fromisoformat(args.day)
    con = duckdb.connect(args.db, read_only=True)
    rows = day_rows(con, day)
    con.close()

    text = compose_text(day, rows)

    # The HTML file is the report; the Telegram text is the caption that carries
    # it, not an alternative format.
    from render_report import render_html
    from sintesi import build_summary
    summary = build_summary(day, rows)
    path = Path(args.html) if args.html else Path('reports/econ') / f'calendar_{day}.html'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_html(day, rows, 'tradays', summary), encoding='utf-8')

    print(text)
    print(f"\n[{len(text)} chars of text] [HTML: {path}]")

    if args.send:
        from lazytools.connectors.telegram import TelegramClient
        token = os.environ['TELEGRAM_BOT_TOKEN']
        chat = os.environ['TELEGRAM_CHAT_ID']
        # from_token, as in send_telegram_run_report.py: the plain constructor
        # does not carry an HTTP client
        with TelegramClient.from_token(token) as client:
            client.send_document(chat_id=chat, document=path.read_bytes(),
                                 filename=path.name, caption=text[:1000])
        print("\nsent to Telegram (HTML as document).")
