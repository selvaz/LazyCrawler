# -*- coding: utf-8 -*-
"""econ_report.py -- render an ``econ_daily_report`` row as a self-contained
HTML report. ``render_html(row)`` is a pure function of the exact dict shape
``run_econ_monitor.py`` saves to ``reports/econ/*.json`` -- no live API
access, no re-fetching -- so any saved row can always be re-rendered from its
JSON alone. See ``render_econ_report.py`` for a CLI that does exactly that.

Visual system matches market-data-hub's regime/ETF daily reports (same CSS
tokens, filter-bar + card-grid pattern) for a consistent look across the
Lazy* ecosystem's scheduled reports.
"""

from __future__ import annotations

import json

__all__ = ["render_html"]

_TEMPLATE = r"""<title>Economic Release Monitor — daily artifact</title>
<style>
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
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; background: var(--bg); color: var(--ink);
    font-family: -apple-system, "Segoe UI", ui-sans-serif, system-ui, sans-serif; -webkit-font-smoothing: antialiased; }
  body { max-width: 1180px; margin: 0 auto; padding: 28px 24px 64px; }
  .mono { font-family: ui-monospace, "Cascadia Mono", "SFMono-Regular", Consolas, monospace; font-variant-numeric: tabular-nums; }

  header { display: flex; flex-wrap: wrap; align-items: flex-end; justify-content: space-between; gap: 16px;
    padding-bottom: 20px; border-bottom: 1px solid var(--border); margin-bottom: 24px; }
  .eyebrow { font-size: 12px; font-weight: 600; letter-spacing: .08em; text-transform: uppercase; color: var(--accent-ink); margin: 0 0 6px; }
  h1 { font-size: 26px; font-weight: 700; letter-spacing: -.01em; margin: 0; text-wrap: balance; }
  h2 { font-size: 15px; margin: 0 0 12px; }
  .meta-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(90px, max-content)); gap: 4px 26px; text-align: right; }
  .meta-item .k { font-size: 10.5px; letter-spacing: .06em; text-transform: uppercase; color: var(--ink-faint); display: block; }
  .meta-item .v { font-size: 13px; color: var(--ink-soft); }

  section { margin-bottom: 32px; }

  .filter-bar { display: flex; gap: 8px; align-items: center; margin-bottom: 16px; flex-wrap: wrap; }
  .filter-bar input { flex: 1; min-width: 160px; padding: 8px 12px; border-radius: 999px; border: 1px solid var(--border);
    background: var(--surface); color: var(--ink); font: inherit; font-size: 13px; }
  .toggle-group { display: inline-flex; background: var(--surface-2); border-radius: 999px; padding: 3px; gap: 2px; }
  .toggle-group button { border: none; background: transparent; font: inherit; font-size: 12px; font-weight: 600;
    color: var(--ink-soft); padding: 6px 14px; border-radius: 999px; cursor: pointer; }
  .toggle-group button.active { background: var(--surface); color: var(--ink); box-shadow: var(--shadow); }

  .release-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(380px, 1fr)); gap: 12px; }
  .release-card { background: var(--surface); border: 1px solid var(--accent); border-radius: var(--radius);
    box-shadow: var(--shadow); padding: 16px 18px; display: flex; flex-direction: column; gap: 10px; }
  .release-top { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }
  .release-name { font-weight: 700; font-size: 15px; }
  .release-agency { font-size: 11px; color: var(--ink-faint); }
  .badge { font-size: 10px; font-weight: 700; letter-spacing: .02em; padding: 2px 7px; border-radius: 999px; white-space: nowrap; }
  .badge.tier1 { color: var(--pos); background: color-mix(in srgb, var(--pos) 14%, transparent); }
  .badge.tier2 { color: var(--mid-ink); background: color-mix(in srgb, var(--mid) 20%, transparent); }
  .release-figures { display: flex; gap: 18px; align-items: baseline; }
  .release-value { font-size: 22px; font-weight: 700; font-family: ui-monospace, monospace; }
  .release-change { font-size: 13px; font-weight: 600; font-family: ui-monospace, monospace; }
  .release-change.pos { color: var(--pos); } .release-change.neg { color: var(--neg); }
  .release-period { font-size: 11px; color: var(--ink-faint); }
  .why { font-size: 12.5px; color: var(--ink-soft); line-height: 1.5; }
  .commentary { display: flex; flex-direction: column; gap: 6px; border-top: 1px solid var(--border); padding-top: 10px; }
  .commentary a { font-size: 12px; color: var(--accent-ink); text-decoration: none; font-weight: 600; }
  .commentary a:hover { text-decoration: underline; }
  .commentary .src { font-size: 10.5px; color: var(--ink-faint); }
  .commentary .snippet { font-size: 11.5px; color: var(--ink-soft); }
  .no-commentary { font-size: 11.5px; color: var(--ink-faint); font-style: italic; }

  .empty-note { font-size: 13px; color: var(--ink-faint); padding: 20px 0; }

  .status-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 8px; }
  .status-row { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
    padding: 10px 14px; display: flex; align-items: center; justify-content: space-between; gap: 10px; font-size: 12.5px; }
  .status-row.new { border-color: var(--accent); }
  .status-row.error { border-left: 3px solid var(--pos); }
  .status-name { display: flex; flex-direction: column; }
  .status-name .n { font-weight: 600; }
  .status-name .a { font-size: 10.5px; color: var(--ink-faint); }
  .status-fig { text-align: right; font-family: ui-monospace, monospace; }
  .status-fig .p { font-size: 10.5px; color: var(--ink-faint); }
  .status-fig .v { font-weight: 700; }

  footer { margin-top: 40px; padding-top: 18px; border-top: 1px solid var(--border); font-size: 11.5px;
    color: var(--ink-faint); display: flex; flex-wrap: wrap; gap: 6px 22px; }
  footer b { color: var(--ink-soft); font-weight: 600; }
</style>

<header>
  <div>
    <p class="eyebrow">official sources only &middot; scheduled artifact</p>
    <h1>Economic Release Monitor</h1>
  </div>
  <div class="meta-grid mono" id="meta-grid"></div>
</header>

<section id="new-section">
  <h2 id="new-heading"></h2>
  <div class="release-grid" id="release-grid"></div>
</section>

<section id="status-section">
  <h2>All tracked indicators</h2>
  <div class="filter-bar">
    <input type="text" id="search" placeholder="Filter by name or agency...">
    <div class="toggle-group" id="view-toggle">
      <button data-v="all" class="active">All</button>
      <button data-v="new">New today</button>
      <button data-v="tier1">Tier 1</button>
      <button data-v="error">Errors</button>
    </div>
  </div>
  <div class="status-grid" id="status-grid"></div>
</section>

<footer id="footer"></footer>

<script>
const ROW = __ROW_JSON__;
const P = ROW.payload;

function fmtNum(v) {
  if (v == null) return "&mdash;";
  return Math.abs(v) >= 1000 ? v.toLocaleString(undefined, {maximumFractionDigits: 1}) : v.toFixed(2);
}

(function renderNew() {
  const heading = document.getElementById("new-heading");
  const grid = document.getElementById("release-grid");
  const releases = P.new_releases || [];
  heading.textContent = releases.length
    ? `New today (${releases.length})`
    : "New today";
  if (!releases.length) {
    grid.innerHTML = `<div class="empty-note">No tracked indicator released fresh data today.</div>`;
    return;
  }
  grid.innerHTML = releases.map(r => {
    const chgClass = r.change_sign === "pos" ? "pos" : r.change_sign === "neg" ? "neg" : "";
    const commentary = (r.commentary || []).length
      ? r.commentary.map(c => `
          <div>
            <a href="${c.url}" target="_blank" rel="noopener">${c.title}</a>
            <div class="src">${c.source_domain || ""}${c.sentiment ? " &middot; " + c.sentiment : ""}</div>
            ${c.summary ? `<div class="snippet">${c.summary}</div>` : ""}
          </div>`).join("")
      : `<div class="no-commentary">No English-language commentary found yet.</div>`;
    return `
      <div class="release-card">
        <div class="release-top">
          <div>
            <div class="release-name">${r.name}</div>
            <div class="release-agency">${r.agency}</div>
          </div>
          <span class="badge tier${r.tier}">Tier ${r.tier}</span>
        </div>
        <div class="release-figures">
          <span class="release-value">${fmtNum(r.value)}</span>
          <span class="release-change ${chgClass}">${r.change_display || "&mdash;"}</span>
          <span class="release-period">${r.period} &middot; ${r.unit}</span>
        </div>
        <div class="why">${r.why_it_matters}</div>
        <div class="commentary">${commentary}</div>
      </div>`;
  }).join("");
})();

let currentFilter = "all";
let currentQuery = "";

function matchesFilter(s) {
  if (currentFilter === "new" && !s.is_new_today) return false;
  if (currentFilter === "tier1" && s.tier !== 1) return false;
  if (currentFilter === "error" && s.status !== "error") return false;
  if (currentQuery) {
    const q = currentQuery.toLowerCase();
    const hay = (s.name + " " + (s.agency || "")).toLowerCase();
    if (!hay.includes(q)) return false;
  }
  return true;
}

function renderStatus() {
  const grid = document.getElementById("status-grid");
  const visible = (P.all_indicators || []).filter(matchesFilter);
  if (!visible.length) {
    grid.innerHTML = `<div class="empty-note">No indicators match this filter.</div>`;
    return;
  }
  grid.innerHTML = visible.map(s => {
    const classes = ["status-row"];
    if (s.is_new_today) classes.push("new");
    if (s.status === "error") classes.push("error");
    const fig = s.status === "error"
      ? `<span class="status-fig"><span class="v" style="color:var(--pos)">error</span></span>`
      : `<span class="status-fig"><span class="p">${s.last_period || "&mdash;"}</span><br><span class="v">${fmtNum(s.last_value)}</span></span>`;
    return `
      <div class="${classes.join(" ")}">
        <span class="status-name"><span class="n">${s.name}${s.is_new_today ? ' <span class="badge tier1" style="vertical-align:middle">new</span>' : ""}</span><span class="a">${s.agency}</span></span>
        ${fig}
      </div>`;
  }).join("");
}

document.getElementById("view-toggle").addEventListener("click", e => {
  const btn = e.target.closest("button");
  if (!btn) return;
  currentFilter = btn.dataset.v;
  document.querySelectorAll("#view-toggle button").forEach(b => b.classList.toggle("active", b === btn));
  renderStatus();
});
document.getElementById("search").addEventListener("input", e => {
  currentQuery = e.target.value;
  renderStatus();
});
renderStatus();

(function renderHeader() {
  const items = [
    ["As of", P.as_of],
    ["Tracked", P.summary.n_indicators],
    ["New today", P.summary.n_new_today],
    ["Errors", P.summary.n_errors],
  ];
  document.getElementById("meta-grid").innerHTML = items.map(([k, v]) =>
    `<div class="meta-item"><span class="k">${k}</span><span class="v">${v}</span></div>`).join("");
})();

(function renderFooter() {
  document.getElementById("footer").innerHTML = `
    <span><b>Produced by</b> ${ROW.produced_by}</span>
    <span><b>Cadence</b> ${ROW.cadence}</span>
    <span><b>Sources</b> ${P.provenance.sources.join(", ")}</span>
    <span><b>Saved</b> ${ROW.created_at}</span>
  `;
})();
</script>
"""


def render_html(row: dict) -> str:
    """Render ``row`` (the same dict shape ``run_econ_monitor.py`` saves to
    ``reports/econ/*.json``) as a self-contained HTML report. Pure function
    -- no I/O -- works identically whether ``row`` just came off a live run
    or was loaded from disk days later."""
    return _TEMPLATE.replace("__ROW_JSON__", json.dumps(row))
