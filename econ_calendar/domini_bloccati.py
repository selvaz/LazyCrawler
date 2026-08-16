# -*- coding: utf-8 -*-
"""Domains that refuse this machine, fed to the crawler's own blacklist.

Not a guess: harvested from the 403/401/429 responses actually logged. Six
hundred fetches were spent on sites that never answer, and a third of those on
bls.gov alone -- which the agent keeps trying precisely because it is told to
look for the issuing agency's release.

The crawler checks this list before the fetch (``_pipeline.py``), so a blocked
domain costs nothing rather than a round trip and a 403.

Kept as data so it can grow: `aggiorna()` rescans the logs and merges in
anything new, instead of the list rotting as sites change their posture.
"""

from __future__ import annotations

import glob
import re
from collections import Counter
from pathlib import Path

ELENCO = Path(__file__).with_name("domini_bloccati.txt")
SOGLIA = 3  # under three refusals it may be a transient failure


def carica() -> list[str]:
    if not ELENCO.exists():
        return []
    return [
        r.strip()
        for r in ELENCO.read_text(encoding="utf-8").splitlines()
        if r.strip() and not r.startswith("#")
    ]


def aggiorna(schema: str = "*log*.txt", soglia: int = SOGLIA) -> tuple[int, int]:
    """Rescan the logs and merge newly refusing domains into the list."""
    conta = Counter()
    for f in glob.glob(schema):
        try:
            t = Path(f).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in re.finditer(r"non-retryable HTTP (\d{3}) for https?://([^/\s]+)", t):
            if m.group(1) in ("401", "403", "429"):
                conta[m.group(2)] += 1

    esistenti = set(carica())
    nuovi = {d for d, n in conta.items() if n >= soglia} - esistenti
    if nuovi:
        with open(ELENCO, "a", encoding="utf-8") as f:
            for d in sorted(nuovi):
                f.write(f"{d}\n")
    return len(nuovi), len(esistenti | nuovi)


if __name__ == "__main__":
    nuovi, totale = aggiorna()
    print(f"{nuovi} domini aggiunti, {totale} in lista")
    for d in carica()[:12]:
        print("  ", d)
