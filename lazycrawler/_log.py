# -*- coding: utf-8 -*-
"""
lazycrawler._log
================
Single named logger for the package. Nothing is silently swallowed: every
caught exception is logged here (with a traceback at WARNING/ERROR), and the
``strict`` crawler option re-raises orchestration errors instead of continuing.

By default the logger emits to stderr at INFO so progress and warnings are
visible out of the box (matching a CLI tool's behaviour). Tune or silence it:

    import logging
    from lazycrawler import set_log_level
    set_log_level(logging.WARNING)          # quieter
    set_log_level(logging.DEBUG)            # verbose (best-effort failures too)

    # or take full control (disable the default handler, use your own config):
    logging.getLogger("lazycrawler").handlers.clear()
"""

from __future__ import annotations

import logging
import sys

log = logging.getLogger("lazycrawler")

# Attach a default handler once. propagate=False avoids double logging if the
# host application also configures the root logger.
if not log.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    log.addHandler(_handler)
    log.setLevel(logging.INFO)
    log.propagate = False


def set_log_level(level) -> None:
    """Set the lazycrawler logger level (e.g. logging.DEBUG / 'WARNING')."""
    log.setLevel(level)
