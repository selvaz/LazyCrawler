# -*- coding: utf-8 -*-
"""Enrichment agent for a single macroeconomic release.

It is a function of (indicator, date): given a release already in the calendar,
it searches the web for commentary on THAT release and for technical detail on
how the indicator is built.

The hard part is not searching, it is discarding. A query like "US CPI" returns
mostly calendar pages, indicator fact sheets and commentary on releases from
months ago. That is why the relevance judgement lives in an agent rather than
in a heuristic: only a reader can tell whether a text is about today's print or
about the one from three months back.

Two constraints learned by measuring, not by assuming:
  - the search `timelimit` restricts the time window; without it most results
    are stale;
  - the statistical offices are unreachable. Not from this machine in
    particular: bls.gov and fxstreet.com answer 403 to automated traffic
    generally. The earlier version of these instructions sent the agent to the
    issuing agency for technical detail, which cost 215 refused fetches on
    bls.gov alone. Drivers and components have to come from the outlets that
    quote the release -- and do: the richest breakdown of any run so far was
    India's CPI, whose ministry never answered once.
"""
from __future__ import annotations

import sys
from typing import Optional

from pydantic import BaseModel, Field

sys.path.insert(0, r'C:/Users/Administrator/Documents/GitHub/LazyCrawler')

from lazybridge import Agent, LLMEngine, Tool  # noqa: E402
from lazybridge_claude_code import ClaudeCodeEngine  # noqa: E402
from lazycrawler import WebSearch  # noqa: E402

from domini_bloccati import carica as domini_bloccati  # noqa: E402
from fonti_locali import read_daily_digest, search_collected_articles  # noqa: E402

# The bulk of the work -- many searches, a lot of text to sift -- runs on the
# cheap model. The relevance judgement does not: it is the one place where an
# error reaches the report unseen, so it runs on the subscription through the
# Claude Agent SDK rather than metered API calls.
MODEL = "deepseek-v4-flash"
TEXT_CAP = 4000          # per page: beyond this the context fills with boilerplate


class Commentary(BaseModel):
    """A piece of commentary published on this specific release."""

    outlet: str = Field(description="Name of the publication or site")
    url: str
    summary: str = Field(description="What it says, in one or two sentences")
    relevance_evidence: str = Field(
        description="The element in the text proving it discusses THIS release: "
                    "the printed value, the reference period, or the release date")


class Enrichment(BaseModel):
    commentary: list[Commentary] = Field(
        default_factory=list,
        description="Only commentary on this release. Better none than a generic one.")
    drivers: Optional[str] = Field(
        None, description="What moved the number: components, line items, base effects")
    components: Optional[str] = Field(
        None, description="How the indicator is built and which sub-series weigh most")
    technical_source: Optional[str] = Field(
        None, description="URL of the issuing agency's release, if found")
    not_found: Optional[str] = Field(
        None, description="What could not be retrieved, and why")


# Per-release search budget. Counting and capping are deliberately separate:
# the ceiling is the risky change -- fewer searches can mean less material --
# so it stays off until a run has said what the real number is. The counter
# spans the verify retries too: the budget belongs to the release, not to one
# attempt at it.
_ricerche = 0
CAP_RICERCHE: Optional[int] = None      # None = count, do not refuse


def nuovo_evento(cap: Optional[int] = None) -> int:
    """Open the budget for a new release. Returns the previous one's count."""
    global _ricerche, CAP_RICERCHE
    fatte, _ricerche = _ricerche, 0
    CAP_RICERCHE = cap
    return fatte


def ricerche_fatte() -> int:
    return _ricerche


def search_web(query: str, window: str = "w") -> str:
    """Search recent web pages and return their text.

    Args:
        query: the search to run, in English for wider coverage.
        window: time span: 'd' last day, 'w' last week, 'm' last month.
            Use 'd' or 'w' for commentary on a recent release.
    """
    global _ricerche
    if CAP_RICERCHE is not None and _ricerche >= CAP_RICERCHE:
        return (f"Search budget for this release is spent ({CAP_RICERCHE} searches). "
                "This is deliberate, not a failure: conclude with the material you "
                "already have. If it is not enough, say so in `not_found` and state "
                "what you searched for. Do not call this tool again.")
    _ricerche += 1
    # The crawler skips blacklisted domains before the fetch. Without this the
    # same refusing sites were retried on every event: 601 wasted round trips
    # across the runs, 215 of them on bls.gov alone -- which the agent keeps
    # reaching for precisely because it is told to find the issuing agency.
    with WebSearch() as search:
        outcome = search.run(query, mode="ml", timelimit=window, max_results=4,
                             overrides={"blacklist": domini_bloccati()})
    chunks = []
    for p in outcome.get("results") or []:
        text = (getattr(p, "text", "") or "").strip()
        if not text:
            continue          # 403 or empty page: no point feeding it to the model
        chunks.append(
            f"--- {getattr(p, 'url', '')}\n"
            f"title: {getattr(p, 'title', '') or ''}\n"
            f"{text[:TEXT_CAP]}"
        )
    if not chunks:
        return "No readable page for this search."
    return "\n\n".join(chunks)


INSTRUCTIONS = """You are a macro analyst. You receive the details of ONE economic release
that has already happened, and you must find commentary on THAT release plus
technical information about the indicator.

Work through the sources in this order, and stop climbing as soon as you have
what you need. Each rung costs more than the one below it, and the lower rungs
are better material: they have already been filtered for market relevance and,
in the delta digest, cross-checked against earlier cycles.

1. `read_daily_digest` -- the curated read for that day. Around 44% of releases
   are already covered here, often with a nuance the wires miss.
2. `search_collected_articles` -- the articles already crawled that day, not yet
   summarised. Covers roughly another 25%. No network access, no cost.
3. `search_web` -- only for what the first two do not cover, about a third of
   releases. Put the printed value and the reference period in the query: that
   is what separates this release from earlier ones.

For drivers and components, use the outlets that quote the release: they carry
the breakdown. Do NOT spend searches hunting the issuing agency's own page --
most statistical offices refuse automated access and answer nothing. If an
agency URL turns up inside a result you already have, put it in
`technical_source`; never go looking for it on its own.

The relevance rule, the important one: discard anything that does not prove it
discusses THIS release. Calendar pages, indicator fact sheets and commentary on
earlier releases are excluded even when they concern the same indicator. For
every item you keep you must be able to quote the element tying it to this
release: the printed value, the reference period, or the publication date.

Returning zero commentary is better than returning a generic item. If you find
nothing relevant, say so in `not_found` and state what you searched for."""

REVIEW = """You verify ONE thing only: that every commentary item genuinely concerns the
release described in the task, and not a different print of the same indicator.

For each item, check that at least one of these matches the release in the task:
  - the printed value;
  - the reference period;
  - the publication date.

Reject if even a single item:
  - reports a value or a period different from the release;
  - is a calendar page, an indicator fact sheet or a generic overview;
  - has `relevance_evidence` containing none of those elements, or evidence
    that does not credibly appear in the quoted text.

There is one more thing to check, and only one. An empty result is acceptable
only if the analyst actually went looking. If `commentary` is empty, or every
item fails the checks above, then `not_found` must state that a WEB SEARCH was
run and returned nothing usable. If it does not -- if it only mentions the
digest or the collected articles -- reject, and say explicitly to run
`search_web` and try again. The local sources cover about two thirds of
releases: stopping there abandons the remaining third without looking.

Judge nothing else: not the writing quality, not how complete the drivers are,
not how many outlets were found. An empty result that names a fruitless web
search is a correct answer and must be approved.

Reply exactly "approved" if every item passes. Otherwise reply "rejected: "
followed by which items fail and why, or by the instruction to run search_web
and retry, so the analyst knows what to do next."""

# Last rung of the pipeline. The enrichment model answers in prose about four
# times out of ten -- it finds the material, then describes it instead of
# filling the schema -- and those runs used to be thrown away after paying for
# every search. Rather than chase a formatting failure in a model chosen for
# volume, a small model reshapes the text it already produced. It invents
# nothing: it only moves what is there into the fields.
#
# On the subscription through the Claude Agent SDK, like the reviewer: it runs
# only on the failures, but those are frequent enough that metered calls would
# add up.
FORMAT = """You receive an analyst's notes about ONE economic release, written as prose.
Reshape them into the required structure. Move only what is in the text: do not
add outlets, do not invent URLs, do not summarise beyond what is written.

If the notes contain no commentary on the release, return an empty commentary
list and say so in `not_found`. An empty result faithful to the text is correct;
a filled one that adds anything is not."""

formatter = Agent(
    engine=ClaudeCodeEngine(
        model="haiku",
        system=FORMAT,
        web=False,          # it reshapes text, it does not go looking for more
        max_turns=1,
    ),
    output=Enrichment,
    name="prose_to_structure",
)


reviewer = Agent(
    engine=ClaudeCodeEngine(
        model="sonnet",
        system=REVIEW,
        reasoning_effort="medium",
        web=False,          # the reviewer judges what it is given; it does not search
        max_turns=1,
    ),
    name="relevance_reviewer",
)

release_agent = Agent(
    # 8 bastavano quando lo strumento era uno solo. Con la cascata a tre, i
    # turni finivano nelle chiamate e il modello non arrivava mai a scrivere
    # la risposta: la busta tornava vuota con MaxTurnsExceeded.
    engine=LLMEngine(MODEL, system=INSTRUCTIONS, max_turns=14),
    tools=[
        Tool.wrap(read_daily_digest, name="read_daily_digest"),
        Tool.wrap(search_collected_articles, name="search_collected_articles"),
        Tool.wrap(search_web, name="search_web"),
    ],
    output=Enrichment,
    verify=reviewer,
    max_verify=3,
    name="release_enrichment",
)


# Ultima risorsa, quando il percorso principale non consegna nulla.
#
# Non e' una copia del primo agente con un modello piu' grande: e' una strada
# diversa. Usa gli strumenti web interni dell'SDK invece della catena
# digest -> articoli -> ricerca, ha un budget di turni piu' largo, e produce
# direttamente la struttura. Soprattutto NON passa dal reviewer: se il
# percorso con giudizio ha gia' fallito, ripetere lo stesso giudizio non
# aggiunge garanzie, aggiunge solo un altro modo di non consegnare.
#
# Il prezzo e' che il suo risultato non e' stato verificato da nessuno, e
# questo va detto: chi legge il report deve poter distinguere un commento
# passato al vaglio da uno che non lo e' stato.
FALLBACK = """You are a macro analyst. Find commentary published on ONE specific economic
release, and technical detail on how the indicator is built.

Search the web yourself. Put the printed value and the reference period in the
query: that is what separates this release from earlier prints of the same
indicator.

Keep only what proves it discusses THIS release -- the printed value, the
reference period or the publication date must appear. Discard calendar pages,
indicator fact sheets and commentary on earlier releases. For every item you
keep, quote the element that ties it to this release.

Returning nothing is better than returning something generic. If you find
nothing, say what you searched for in `not_found`."""

fallback_agent = Agent(
    engine=ClaudeCodeEngine(
        model="sonnet",
        system=FALLBACK,
        web=True,           # cerca da se', senza la cascata di strumenti locali
        max_turns=14,       # il percorso principale falliva proprio per turni finiti
    ),
    output=Enrichment,
    name="release_fallback",
)


def describe(event: dict) -> str:
    """The agent's task: everything that identifies the release."""
    lines = [
        f"Indicator: {event['name']} ({event['area']})",
        f"Published: {event['release_utc']}",
        f"Release date for the local sources: {str(event['release_utc'])[:10]}",
    ]
    if event.get("reference_period"):
        lines.append(f"Reference period: {event['reference_period']}")
    if event.get("actual"):
        lines.append(f"Printed value: {event['actual']}")
    if event.get("consensus"):
        lines.append(f"Consensus: {event['consensus']}")
    if event.get("previous"):
        lines.append(f"Previous: {event['previous']}")
    if event.get("agency"):
        lines.append(f"Issuing agency: {event['agency']}")
    return "\n".join(lines)


if __name__ == "__main__":
    import json

    sample = {
        "name": "Initial Jobless Claims", "area": "US",
        "release_utc": "2026-08-13 12:30 UTC", "reference_period": "week to 8 August",
        "actual": "209K", "consensus": "213 K", "previous": "199 K",
        "agency": "Dept. of Labor",
    }
    result = release_agent(describe(sample))
    print(json.dumps(result.payload.model_dump(), ensure_ascii=False, indent=2))
