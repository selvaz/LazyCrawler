# -*- coding: utf-8 -*-
"""Economic release calendar: the enrichment half.

The collection half lives in market-data-hub, which declares
``requires-python = ">=3.9"`` and tests on 3.9. This half reaches LazyBridge,
which needs >= 3.11, so it lives here instead -- the same split this repository
already makes for its ``smart`` extra.

Nothing at import time reaches LazyBridge: ``agente_release`` is imported
inside the functions that need it, so the rest of the package -- the day
report, the HTML, the local sources -- stays usable on an interpreter without
the ``smart`` extra installed.
"""
