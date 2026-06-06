# -*- coding: utf-8 -*-
"""
lazycrawler.ratelimit
=====================
Per-host rate limiting, applied in BOTH sequential and parallel mode.

A single :class:`HostRateLimiter` is shared across all worker threads. Before
each fetch the crawler calls :meth:`HostRateLimiter.wait`, which enforces a
minimum gap between consecutive requests to the *same* host. The effective gap
is the larger of:

  - the configured ``per_host_delay`` (HTTPConfig), and
  - the host's robots.txt ``Crawl-delay`` (when robots are respected).

The "next allowed time" per host is reserved while holding the lock and the
sleep happens *outside* the lock, so concurrent requests to the same host queue
up politely without serializing requests to different hosts.
"""

from __future__ import annotations

import threading
import time
from typing import Dict, Optional

from ._log import log
from .http import get_hostname


class HostRateLimiter:
    """Thread-safe minimum-gap limiter keyed by host."""

    def __init__(self, default_delay: float = 0.0, robots: Optional[object] = None):
        self._default = max(0.0, float(default_delay or 0.0))
        self._robots = robots  # RobotsChecker | None
        self._next_allowed: Dict[str, float] = {}
        self._lock = threading.Lock()

    def _delay_for(self, url: str) -> float:
        delay = self._default
        if self._robots is not None:
            try:
                cd = self._robots.crawl_delay(url)
            except Exception:
                cd = None
            if cd:
                delay = max(delay, float(cd))
        return delay

    def wait(self, url: str) -> None:
        """Block until it is polite to fetch ``url`` (no-op when delay is 0)."""
        delay = self._delay_for(url)
        if delay <= 0:
            return
        host = get_hostname(url)
        if not host:
            return
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next_allowed.get(host, 0.0))
            self._next_allowed[host] = start + delay  # reserve our slot
            sleep_for = start - now
        if sleep_for > 0:
            log.debug("  rate-limit: waiting %.2fs for host %s", sleep_for, host)
            time.sleep(sleep_for)
