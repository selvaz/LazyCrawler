# -*- coding: utf-8 -*-
"""HostRateLimiter: per-host minimum gap + robots Crawl-delay integration."""

from __future__ import annotations

import time

from lazycrawler.ratelimit import HostRateLimiter


def test_no_delay_is_noop():
    rl = HostRateLimiter(default_delay=0.0)
    t0 = time.monotonic()
    rl.wait("https://e.org/a")
    rl.wait("https://e.org/b")
    assert time.monotonic() - t0 < 0.05


def test_same_host_is_throttled():
    rl = HostRateLimiter(default_delay=0.2)
    t0 = time.monotonic()
    rl.wait("https://e.org/a")  # first call: no wait
    rl.wait("https://e.org/b")  # same host: must wait ~0.2s
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.18


def test_different_hosts_not_throttled():
    rl = HostRateLimiter(default_delay=0.2)
    t0 = time.monotonic()
    rl.wait("https://a.example/x")
    rl.wait("https://b.example/x")
    assert time.monotonic() - t0 < 0.1


class _FakeRobots:
    def __init__(self, delay):
        self._delay = delay

    def crawl_delay(self, url):
        return self._delay


def test_robots_crawl_delay_applied():
    rl = HostRateLimiter(default_delay=0.0, robots=_FakeRobots(0.2))
    t0 = time.monotonic()
    rl.wait("https://e.org/a")
    rl.wait("https://e.org/b")
    assert time.monotonic() - t0 >= 0.18


def test_effective_delay_is_max_of_config_and_robots():
    rl = HostRateLimiter(default_delay=0.3, robots=_FakeRobots(0.1))
    t0 = time.monotonic()
    rl.wait("https://e.org/a")
    rl.wait("https://e.org/b")
    assert time.monotonic() - t0 >= 0.28  # config 0.3 wins
