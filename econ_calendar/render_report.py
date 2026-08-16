# -*- coding: utf-8 -*-
"""Daily calendar report as a self-contained HTML page.

Follows the visual system of econ_report.py (LazyCrawler-runtime-jobs): same CSS
token names, same filter-bar + card-grid layout, same dual theme. That is not
decoration: the ecosystem's scheduled reports are read one after another, and a
different stylesheet makes them look like different systems.

Differences from econ_report.py, all of them from what the calendar knows in
additionn: the band is T1/T2/T3 from the catalogue rather than a registry tier;
the surprise is computable because a consensus exists; and the consensus source
is always stated.
"""
from __future__ import annotations

import html
import json
from datetime import date

TOKENS = """
  :root {
    --bg: #F5F6FA; --surface: #FFFFFF; --surface-2: #ECEEF3;
    --ink: #12151C; --ink-soft: #4B5468; --ink-faint: #8890A0;
    --border: #DDE1E8; --accent: #4C6FA6; --accent-ink: #2E4468;
    --pos: #B24A2E; --neg: #2E6E8E; --mid: #B8860B; --mid-ink: #7A5A07;
    --radius: 10px;
    --shadow: 0 1px 2px rgba(20,24,34,.04), 0 8px 24px -12px rgba(20,24,34,.12);
  }
  :root[data-theme="dark"] {
    --bg: #0D0F15; --surface: #151822; --surface-2: #1D212D;
    --ink: #E9EAF0; --ink-soft: #9AA3B7; --ink-faint: #656E82;
    --border: #262B38; --accent: #7C9BD0; --accent-ink: #B9CCE8;
    --pos: #D97B5C; --neg: #5DA0C4; --mid: #E0BB4A; --mid-ink: #F0D48A;
    --shadow: 0 1px 2px rgba(0,0,0,.3), 0 8px 24px -12px rgba(0,0,0,.5);
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg: #0D0F15; --surface: #151822; --surface-2: #1D212D;
      --ink: #E9EAF0; --ink-soft: #9AA3B7; --ink-faint: #656E82;
      --border: #262B38; --accent: #7C9BD0; --accent-ink: #B9CCE8;
      --pos: #D97B5C; --neg: #5DA0C4; --mid: #E0BB4A; --mid-ink: #F0D48A;
      --shadow: 0 1px 2px rgba(0,0,0,.3), 0 8px 24px -12px rgba(0,0,0,.5);
    }
  }
"""

STYLE = TOKENS + """
  html, body { margin: 0; padding: 0; background: var(--bg); color: var(--ink);
    font-family: -apple-system, "Segoe UI", ui-sans-serif, system-ui, sans-serif;
    -webkit-font-smoothing: antialiased; }
  .mono { font-family: ui-monospace, "Cascadia Mono", "SFMono-Regular", Consolas, monospace;
    font-variant-numeric: tabular-nums; }
  .wrap { max-width: 1100px; margin: 0 auto; padding: 28px 20px 64px; }
  .eyebrow { font-size: 11px; letter-spacing: .14em; text-transform: uppercase;
    color: var(--accent-ink); font-weight: 700; }
  h1 { font-size: 26px; margin: 8px 0 4px; letter-spacing: -.01em; }
  .sub { color: var(--ink-soft); font-size: 14px; margin: 0 0 20px; max-width: 76ch; }
  .filter-bar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
    margin-bottom: 18px; }
  .toggle-group { display: inline-flex; background: var(--surface-2);
    border-radius: 999px; padding: 3px; gap: 2px; }
  .toggle-group button { border: none; background: transparent; font: inherit;
    font-size: 12px; font-weight: 600; color: var(--ink-soft); padding: 6px 14px;
    border-radius: 999px; cursor: pointer; }
  .toggle-group button.active { background: var(--surface); color: var(--ink);
    box-shadow: var(--shadow); }
  .release-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(380px, 1fr));
    gap: 12px; }
  .release-card { background: var(--surface); border: 1px solid var(--border);
    border-radius: var(--radius); box-shadow: var(--shadow); padding: 16px 18px;
    display: flex; flex-direction: column; gap: 10px; }
  .release-card.tier1 { border-color: var(--accent); }
  .release-top { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }
  .release-name { font-weight: 700; font-size: 15px; }
  .release-agency { font-size: 11px; color: var(--ink-faint); }
  .release-period { font-size: 11px; color: var(--ink-faint); }
  .badge { font-size: 10px; font-weight: 700; letter-spacing: .02em; padding: 2px 7px;
    border-radius: 999px; white-space: nowrap; }
  .badge.tier1 { color: var(--pos); background: color-mix(in srgb, var(--pos) 14%, transparent); }
  .badge.tier2 { color: var(--mid-ink); background: color-mix(in srgb, var(--mid) 20%, transparent); }
  .badge.tier3 { color: var(--ink-faint); background: var(--surface-2); }
  .release-figures { display: flex; gap: 18px; align-items: baseline; flex-wrap: wrap; }
  .release-value { font-size: 22px; font-weight: 700; }
  .figure { font-size: 12px; color: var(--ink-soft); }
  .figure b { display: block; font-size: 10px; letter-spacing: .07em;
    text-transform: uppercase; color: var(--ink-faint); font-weight: 700; }
  .surprise { font-size: 13px; font-weight: 700; }
  .surprise.above { color: var(--pos); } .surprise.below { color: var(--neg); }
  .surprise .src { font-weight: 400; color: var(--ink-faint); font-size: 11px; }
  .warn { font-size: 12px; color: var(--mid-ink);
    background: color-mix(in srgb, var(--mid) 14%, transparent);
    border-radius: 6px; padding: 6px 9px; }
  .commentary { font-size: 12.5px; color: var(--ink-soft); border-top: 1px solid var(--border);
    padding-top: 9px; margin-top: 2px; }
  .commentary h4 { margin: 0 0 5px; font-size: 10px; letter-spacing: .08em;
    text-transform: uppercase; color: var(--ink-faint); }
  .commentary p { margin: 0 0 7px; }
  .press { margin: 0; padding: 0; list-style: none; }
  .press li { margin-bottom: 6px; }
  .press a { color: var(--accent-ink); text-decoration: none; font-weight: 600; }
  .press a:hover { text-decoration: underline; }
  .generated { font-size: 10px; color: var(--ink-faint); margin-top: 4px; }
  .summary { background: var(--surface); border: 1px solid var(--accent);
    border-radius: var(--radius); box-shadow: var(--shadow); padding: 18px 20px;
    margin-bottom: 20px; }
  .summary h2 { margin: 0 0 8px; font-size: 18px; letter-spacing: -.01em; }
  .summary ul { margin: 10px 0 0; padding-left: 18px; }
  .summary li { margin-bottom: 5px; font-size: 14px; }
  .summary .implications { margin: 12px 0 0; font-size: 14px; color: var(--ink-soft); }
  .summary .caveats { margin: 10px 0 0; font-size: 12.5px; color: var(--mid-ink);
    background: color-mix(in srgb, var(--mid) 12%, transparent);
    border-radius: 6px; padding: 8px 10px; }
  .summary .by { margin-top: 10px; font-size: 10px; color: var(--ink-faint); }
  .empty-note { color: var(--ink-faint); font-size: 15px; padding: 40px 0; text-align: center; }
  footer { margin-top: 32px; padding-top: 14px; border-top: 1px solid var(--border);
    font-size: 12px; color: var(--ink-faint); max-width: 76ch; }
"""

LABEL = {"T1": "critical", "T2": "notable", "T3": "context"}
AREAS = {"US": "United States", "CN": "China", "EZ": "Euro area", "UK": "United Kingdom",
         "JP": "Japan", "IN": "India", "MX": "Mexico", "BR": "Brazil",
         "CA": "Canada", "AU": "Australia", "KR": "South Korea", "TW": "Taiwan"}


def _e(x) -> str:
    return html.escape(str(x)) if x is not None else ""


def _enrichment_block(r: dict) -> str:
    """Generated commentary, visibly separated from the published figures."""
    items = []
    raw = r.get("commentary_json")
    if raw:
        try:
            items = json.loads(raw)
        except (ValueError, TypeError):
            items = []
    if not (items or r.get("drivers") or r.get("components")):
        return ""

    parts = ['<div class="commentary">']
    if r.get("drivers"):
        parts.append(f'<h4>What moved it</h4><p>{_e(r["drivers"])}</p>')
    if r.get("components"):
        parts.append(f'<h4>How it is built</h4><p>{_e(r["components"])}</p>')
    if items:
        parts.append('<h4>Press coverage of this print</h4><ul class="press">')
        for c in items[:6]:
            parts.append(
                f'<li><a href="{_e(c.get("url"))}" target="_blank" rel="noopener">'
                f'{_e(c.get("outlet"))}</a> &mdash; {_e(c.get("summary"))}</li>')
        parts.append("</ul>")
    if r.get("technical_source"):
        parts.append(f'<p class="generated">Agency release cited: '
                     f'<a href="{_e(r["technical_source"])}" target="_blank" '
                     f'rel="noopener">{_e(r["technical_source"])}</a></p>')
    parts.append(
        f'<p class="generated">Generated by {_e(r.get("model") or "model")}, '
        f'relevance reviewed by {_e(r.get("reviewer") or "reviewer")}. '
        f'Model-written text, not published data.</p>')
    parts.append("</div>")
    return "".join(parts)


def _card(r: dict) -> str:
    tier = r["criticality"] or "T3"
    cls = "tier1" if tier == "T1" else "tier2" if tier == "T2" else "tier3"
    parts = [f'<article class="release-card {cls}">']
    period = f' <span class="release-period">({_e(r["reference_period"])})</span>' \
        if r["reference_period"] and r["reference_period"] not in ("-", "N/D") else ""
    parts.append(
        f'<div class="release-top"><div>'
        f'<div class="release-name">{_e(r["name"])}{period}</div>'
        f'<div class="release-agency">{_e(AREAS.get(r["area"], r["area"]))}'
        f'{" &middot; " + _e(r["agency"]) if r["agency"] else ""} &middot; '
        f'{_e(r["ora"])} UTC</div></div>'
        f'<span class="badge {cls}">{LABEL.get(tier, tier)}</span></div>'
    )

    figures = [f'<div class="release-value mono">{_e(r["actual"]) or "&mdash;"}</div>']
    lo_, hi_ = r.get("consensus_low"), r.get("consensus_high")
    atteso = (f"{lo_:g}–{hi_:g}"
              if lo_ is not None and hi_ is not None
              and abs(lo_ - hi_) > 0.02 * max(abs(lo_), abs(hi_), 1e-9)
              else r["consensus"])
    for label, value in (("consensus", atteso), ("previous", r["previous"])):
        if value:
            figures.append(f'<div class="figure"><b>{label}</b>'
                           f'<span class="mono">{_e(value)}</span></div>')
    parts.append(f'<div class="release-figures">{"".join(figures)}</div>')

    lo, hi = r.get("consensus_low"), r.get("consensus_high")
    disperse = (lo is not None and hi is not None
                and abs(lo - hi) > 0.02 * max(abs(lo), abs(hi), 1e-9))
    if disperse:
        parts.append(
            f'<div class="warn">Expectations ranged <span class="mono">{lo:g}</span> to '
            f'<span class="mono">{hi:g}</span> across {r.get("consensus_n")} sources. '
            f'The surprise is not computable: picking one of them would invent it.</div>')
    elif r["actual_num"] is not None and r["consensus_num"] is not None:
        delta = r["actual_num"] - r["consensus_num"]
        way = "above" if delta > 0 else "below" if delta < 0 else ""
        gap = f"{delta:+,.0f}" if abs(delta) >= 1000 else f"{delta:+.3g}"
        wording = ("above consensus" if way == "above"
                   else "below consensus" if way == "below" else "in line")
        parts.append(
            f'<div class="surprise {way}"><span class="mono">{gap}</span> {wording}'
            f' <span class="src">consensus from {_e(r["consensus_source"])}</span></div>')

    if r["values_agree"] is False:
        parts.append(f'<div class="warn">The {r["n_sources"]} sources disagree on the '
                     f'published value.</div>')
    parts.append(_enrichment_block(r))
    if r.get("rationale") and not r.get("drivers"):
        parts.append(f'<div class="commentary">{_e(r["rationale"])}</div>')
    parts.append("</article>")
    return "".join(parts)


def _summary_block(s) -> str:
    """The opening read. Kept visibly model-written, like the commentary blocks."""
    if s is None:
        return ""
    parts = [f'<section class="summary"><h2>{_e(s.headline)}</h2>']
    if s.key_points:
        parts.append("<ul>" + "".join(f"<li>{_e(k)}</li>" for k in s.key_points) + "</ul>")
    if s.implications:
        parts.append(f'<p class="implications">{_e(s.implications)}</p>')
    if s.caveats:
        parts.append(f'<p class="caveats">{_e(s.caveats)}</p>')
    parts.append('<p class="by">Written by claude-sonnet from the figures and coverage '
                 'below. No new facts, no web access.</p></section>')
    return "".join(parts)


def render_html(day: date, rows: list[dict], consensus_source: str, summary=None) -> str:
    n_t1 = sum(1 for r in rows if r["criticality"] == "T1")
    enriched = sum(1 for r in rows if r.get("commentary_json") or r.get("drivers"))
    body = (f'<div class="release-grid">{"".join(_card(r) for r in rows)}</div>'
            if rows else
            '<p class="empty-note">No watchlist release on this day.</p>')

    # Solo i livelli effettivamente presenti: su una giornata di soli critici,
    # tre pulsanti su quattro portavano a una pagina vuota.
    presenti = [t for t in ("T1", "T2", "T3") if any(r["criticality"] == t for r in rows)]
    filtri = ""
    if len(presenti) > 1:
        bottoni = '<button class="active" data-tier="all">all</button>' + "".join(
            f'<button data-tier="{t}">{LABEL[t]}</button>' for t in presenti)
        filtri = (f'<div class="filter-bar"><div class="toggle-group" id="filters">'
                  f'{bottoni}</div></div>')
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Macro calendar &mdash; {day:%d %b %Y}</title>
<style>{STYLE}</style></head><body>
<div class="wrap">
  <div class="eyebrow">market-data-hub &middot; economic calendar</div>
  <h1>Releases of {day:%d %B %Y}</h1>
  <p class="sub">{len(rows)} watchlist releases, {n_t1} of them critical.
     Consensus from <b>{_e(consensus_source)}</b>: a single source, so that
     expectations from different providers are never compared.
     {f'{enriched} enriched with press coverage.' if enriched else ''}</p>
  {filtri}
  {_summary_block(summary)}
  {filtri}
  {body}
  <footer>Ordered by criticality, not by time of day: the report exists to say
  what deserves attention, not to narrate the session. Commentary blocks are
  model-written from web sources and reviewed for relevance; they are kept
  visibly apart from the published figures.</footer>
</div>
<script>
  const cls = {{T1: 'tier1', T2: 'tier2', T3: 'tier3'}};
  document.getElementById('filters').addEventListener('click', e => {{
    const b = e.target.closest('button'); if (!b) return;
    document.querySelectorAll('#filters button').forEach(x => x.classList.remove('active'));
    b.classList.add('active');
    const tier = b.dataset.tier;
    document.querySelectorAll('.release-card').forEach(c => {{
      c.style.display = (tier === 'all' || c.classList.contains(cls[tier])) ? '' : 'none';
    }});
  }});
</script>
</body></html>"""
