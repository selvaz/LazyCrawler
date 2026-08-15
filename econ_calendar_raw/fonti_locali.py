# -*- coding: utf-8 -*-
"""Local sources for release enrichment: the news material already on disk.

LazyCrawler runs three news cycles a day and leaves behind two layers:

``news_digest_*`` / ``digest_delta_*``
    the curated read -- already filtered for market relevance and, in the delta
    digest, cross-checked against previous cycles. Small (tens of KB).

``news_full_*``
    every article collected, by region. Large (a few MB per run), so it is
    searched for passages rather than read whole.

Measured on three days of watchlist releases: the digest alone covers 44% of
them, the full crawl adds another 25%, and 31% appear in neither. Hence the
cascade -- cheapest layer first, web search only for what is left.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from pathlib import Path

NEWS_DIR = Path(r'C:/Users/Administrator/Documents/GitHub/LazyCrawler/reports/news')
DIGEST_CAP = 24_000       # the digest is small enough to read whole
PASSAGE_CAP = 12_000      # extracted passages: enough to judge, not enough to drown
WINDOW = 500              # characters returned around a hit, for the reader
JUDGE_WINDOW = 140        # characters used to DECIDE relevance: a sentence or two


def _run_ids() -> list[str]:
    """Run identifiers, newest first. Only the timestamped ones are real runs."""
    ids = set()
    for f in NEWS_DIR.glob('news_full_news_*_global.md'):
        m = re.search(r'news_full_news_(\d{8}_\d{6})_global\.md$', f.name)
        if m:
            ids.add(m.group(1))
    return sorted(ids, reverse=True)


def runs_for(day: str, lookahead_days: int = 2) -> list[str]:
    """Runs that can plausibly cover a release on ``day``.

    A release is commented on after it happens, so the useful runs are the ones
    from that day and the following ones -- the morning digest of day+1 covers
    the previous afternoon. Runs before the release are useless by construction.
    """
    d = date.fromisoformat(day)
    limite = d + timedelta(days=lookahead_days)
    out = []
    for rid in _run_ids():
        try:
            stamp = datetime.strptime(rid, '%Y%m%d_%H%M%S').date()
        except ValueError:
            continue
        if d <= stamp <= limite:
            out.append(rid)
    return out


def read_daily_digest(day: str) -> str:
    """Read the news digests covering a given day (YYYY-MM-DD).

    This is the first place to look: it is already filtered for market
    relevance, and the delta digest states explicitly what is new versus the
    previous cycles.

    Args:
        day: the release date, as YYYY-MM-DD.
    """
    runs = runs_for(day)
    if not runs:
        return f"No news run covering {day}."
    pieces = []
    for rid in runs[:2]:                      # the run of the day and the next one
        for pattern in (f'digest_delta_*{rid}*.md', f'news_digest_*{rid}*.md'):
            for f in sorted(NEWS_DIR.glob(pattern)):
                pieces.append(f"--- {f.name}\n{f.read_text(encoding='utf-8', errors='replace')}")
    if not pieces:
        return f"No digest file for the runs covering {day}."
    text = "\n\n".join(pieces)
    return text[:DIGEST_CAP]


def search_collected_articles(query: str, day: str) -> str:
    """Search the articles already crawled for that day, without going online.

    Use this when the digest does not mention the release: the material is
    often there anyway, because the digest keeps only what is newsworthy while
    the crawl keeps everything it collected.

    Args:
        query: words to look for, e.g. "jobless claims 209,000".
        day: the release date, as YYYY-MM-DD.
    """
    runs = runs_for(day)
    if not runs:
        return f"No news run covering {day}."
    parole = [w for w in re.split(r'[\s,]+', query.lower()) if len(w) > 2]
    if not parole:
        return "Query too short."

    # Corpus first, so the anchor can be the RAREST query word. Anchoring on the
    # first word instead pulls in whatever is common: a search for "UK GDP
    # monthly" anchored on "gdp" returned Indian trade figures, and "German CPI
    # inflation" anchored on "german" returned a typhoon report.
    corpus = []
    for rid in runs[:2]:
        for f in sorted(NEWS_DIR.glob(f'news_full_news_{rid}_*.md')):
            corpus.append((f.name, f.read_text(encoding='utf-8', errors='replace')))
    if not corpus:
        return f"No collected articles for {day}."

    intero = "\n".join(t for _, t in corpus).lower()
    frequenza = {w: intero.count(w) for w in parole}
    ancora = min((w for w in parole if frequenza[w]), key=lambda w: frequenza[w], default=None)
    if ancora is None:
        return (f"None of the terms in '{query}' appear in the articles collected "
                f"for {day}. A web search is warranted.")

    # Soglia alta di proposito. Con meta' dei termini bastava che 'india',
    # 'price' e 'index' comparissero entro 900 caratteri l'uno dall'altro
    # perche' un pezzo sul contrabbando di legname in Vietnam risultasse
    # pertinente a un CPI indiano -- e il vero danno non era il rumore, era che
    # lo strumento dichiarava di aver trovato: l'agente si fermava li' e non
    # saliva mai alla ricerca web.
    soglia = len(parole) if len(parole) <= 3 else max(3, round(len(parole) * 0.75))

    candidati, visti = [], set()
    for nome, testo in corpus:
        basso = testo.lower()
        for m in re.finditer(re.escape(ancora), basso):
            # Giudizio su una finestra stretta, restituzione su una larga. Contare
            # i termini entro 500 caratteri li faceva risultare "vicini" anche
            # quando stavano in frasi diverse di un pezzo lungo: e' cosi' che un
            # articolo sul contrabbando di legname passava per un CPI indiano.
            stretta = basso[max(0, m.start() - JUDGE_WINDOW): m.end() + JUDGE_WINDOW]
            vicini = sum(1 for w in parole if w in stretta)
            if vicini < soglia:
                continue
            brano = testo[max(0, m.start() - WINDOW): m.end() + WINDOW]
            impronta = brano[:120]
            if impronta in visti:
                continue
            visti.add(impronta)
            candidati.append((vicini, nome, brano.strip()))

    # the more query terms a passage carries, the more likely it is the release
    candidati.sort(key=lambda c: -c[0])
    trovati, lunghezza = [], 0
    for vicini, nome, brano in candidati:
        pezzo = f"--- {nome} ({vicini}/{len(parole)} terms)\n...{brano}..."
        if lunghezza + len(pezzo) > PASSAGE_CAP:
            break
        trovati.append(pezzo)
        lunghezza += len(pezzo)
    if not trovati:
        return (f"Nothing about '{query}' in the articles collected for {day}. "
                f"Best passage carried only {max((c[0] for c in candidati), default=0)} "
                f"of {len(parole)} terms, below the bar. The material is not on disk: "
                "use search_web.")
    return "\n\n".join(trovati)[:PASSAGE_CAP]


if __name__ == '__main__':
    import sys
    giorno = sys.argv[1] if len(sys.argv) > 1 else '2026-08-13'
    print('runs:', runs_for(giorno))
    d = read_daily_digest(giorno)
    print(f'\ndigest: {len(d)} char')
    print(d[:400])
    for q in ['jobless claims 209,000', 'UK GDP monthly', 'German CPI inflation']:
        r = search_collected_articles(q, giorno)
        print(f"\n--- '{q}': {len(r)} char")
        print(r[:260].replace('\n', ' '))
