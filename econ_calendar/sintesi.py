# -*- coding: utf-8 -*-
"""Executive summary for a day of releases.

Written by Sonnet on the subscription, from what the calendar and the
enrichment already hold: no web access, no new facts. It reads the day's
releases -- values, expectations, the press coverage already gathered -- and
says what they add up to.

Deliberately not a judge and not a researcher. It has no tools: if a number is
not in the material it was given, it cannot go and find one, which is the point.
A summary that invents a figure is worse than no summary.
"""

from __future__ import annotations

import json
from datetime import date

from lazybridge import Agent, ClaudeCodeEngine
from pydantic import BaseModel, Field


class ExecutiveSummary(BaseModel):
    headline: str = Field(
        description="One sentence: what the day amounted to, for someone who reads nothing else"
    )
    key_points: list[str] = Field(
        default_factory=list,
        description="Two to four points, each naming the release and the number "
        "that matters. No filler.",
    )
    implications: str = Field(
        description="What follows for policy expectations or positioning, "
        "stated with the uncertainty it deserves"
    )
    caveats: str | None = Field(
        None,
        description="Where the data itself is weak: expectations that diverged "
        "across sources, values the sources disagree on, releases "
        "with no coverage found",
    )


INSTRUCTIONS = """You write the opening summary of a daily macro calendar report, for a
reader who follows markets and will read the detail below if the summary earns it.

Work only from the material given. Every number you cite must appear in it. If
something is missing, say it is missing rather than filling the gap.

What matters:
- Lead with what the day amounted to, not with a list of what was published.
- Name the release and the figure that carries the point. "US CPI came in at
  3.4% y/y" beats "inflation data was released".
- Where expectations diverged across sources, the surprise is not computable.
  Say so instead of picking a number: an invented surprise is the one error
  that would discredit the whole report.
- Where sources disagree on the published value, flag it in the caveats.
- Be plain. No "markets will be watching closely", no "mixed signals" without
  saying which signal pointed where.

If the day holds nothing worth a reader's attention, say that in one line."""

summary_agent = Agent(
    engine=ClaudeCodeEngine(
        model="sonnet",
        system=INSTRUCTIONS,
        web=False,  # it summarises what is there; it does not go looking
        max_turns=1,
    ),
    output=ExecutiveSummary,
    name="executive_summary",
)


def _material(day: date, rows: list[dict]) -> str:
    """Everything the summary is allowed to know."""
    parts = [f"Releases of {day:%d %B %Y}.", ""]
    for r in rows:
        lines = [
            f"[{r['criticality']}] {r['area']} - {r['name']}"
            f"{' (' + r['reference_period'] + ')' if r.get('reference_period') not in (None, '', '-', 'N/D') else ''}"
            f" at {r['ora']} UTC"
        ]
        lines.append(f"  actual: {r.get('actual') or 'not published'}")
        lo, hi = r.get("consensus_low"), r.get("consensus_high")
        if lo is not None and hi is not None and abs(lo - hi) > 0.02 * max(abs(lo), abs(hi), 1e-9):
            lines.append(
                f"  expectations DIVERGED across sources: {lo:g} to {hi:g}"
                f" - surprise not computable"
            )
        elif r.get("consensus"):
            lines.append(f"  consensus: {r['consensus']} (from {r.get('consensus_source')})")
        if r.get("previous"):
            lines.append(f"  previous: {r['previous']}")
        if r.get("values_agree") is False:
            lines.append(f"  WARNING: the {r.get('n_sources')} sources disagree on the value")
        if r.get("drivers"):
            lines.append(f"  drivers: {r['drivers'][:600]}")
        raw = r.get("commentary_json")
        if raw:
            try:
                for c in json.loads(raw)[:3]:
                    lines.append(f"  press ({c.get('outlet')}): {c.get('summary')}")
            except (ValueError, TypeError):
                pass
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def build_summary(day: date, rows: list[dict]):
    """Return an ExecutiveSummary, or None if it could not be produced.

    A missing summary must not cost the report: the detail below stands on its
    own, and a half-written opening would be worse than none.
    """
    if not rows:
        return None
    try:
        result = summary_agent(_material(day, rows))
    except Exception:
        return None
    p = result.payload
    if isinstance(p, ExecutiveSummary):
        return p
    testo = str(p or result.text() or "")
    i, j = testo.find("{"), testo.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        return ExecutiveSummary.model_validate_json(testo[i : j + 1])
    except Exception:
        return None
