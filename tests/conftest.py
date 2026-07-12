# -*- coding: utf-8 -*-
"""
Shared pytest fixtures for LazyCrawler.

The package is expected to be installed (``pip install -e ".[all,dev]"``), so
there is no sys.path / .env juggling here — that lived in the old
``setup_paths`` bootstrap and now belongs to ``examples/spyder_setup.py``.

Fixtures
--------
tmp_db        a fresh CrawlerDB on a tmp_path SQLite file (auto-closed)
stub_fetch    factory installing a counting HTTPClient.fetch stub (auto-restored
              by monkeypatch); returns a state dict with call counts
make_crawler  factory building WebCrawler instances (auto-closed) wired for the
              offline tests (robots off, no delays, SSL off)
"""

from __future__ import annotations

import threading

import pytest

from lazycrawler import CrawlerConfig, CrawlerDB, DBConfig, HTTPConfig, WebCrawler
from lazycrawler import http as http_mod
from lazycrawler.http import FetchResult, normalize_url

# A body long enough to clear the default min_text_chars threshold.
DEFAULT_BODY = "Independent generic article body, long enough to be treated as real text. " * 8


@pytest.fixture
def tmp_db(tmp_path):
    db = CrawlerDB(DBConfig(db_path=str(tmp_path / "lazycrawler_test.db")))
    yield db
    db.close()


@pytest.fixture
def db_factory(tmp_path):
    """Build extra DBs (e.g. force_refresh / ttl variants) on the SAME file."""
    dbs = []

    def _make(**cfg):
        base = dict(db_path=str(tmp_path / "lazycrawler_test.db"))
        base.update(cfg)
        db = CrawlerDB(DBConfig(**base))
        dbs.append(db)
        return db

    yield _make
    for db in dbs:
        db.close()


@pytest.fixture
def stub_fetch(monkeypatch):
    """Install a counting HTTPClient.fetch stub. Returns the install() factory.

    install(content_map=None, body=..., links_map=None, pdf_map=None) -> state

    - content_map: {url: text_body}     controls extracted text per URL
    - links_map:   {url: html_fragment} extra <a> anchors injected into the page
    - pdf_map:     {url: bytes}          return PDF bytes (content) for that URL
    The returned ``state`` tracks total calls (``n``) and per-URL counts.
    """

    def install(content_map=None, body=DEFAULT_BODY, links_map=None, pdf_map=None):
        state = {"n": 0, "by_url": {}, "lock": threading.Lock()}
        cmap = content_map or {}
        lmap = links_map or {}
        pmap = pdf_map or {}

        def fetch(self, url, extra_headers=None):
            key = normalize_url(url)
            with state["lock"]:
                state["n"] += 1
                state["by_url"][key] = state["by_url"].get(key, 0) + 1
            if url in pmap or key in pmap:
                return FetchResult(
                    status=200, content=pmap.get(url, pmap.get(key)), content_type="application/pdf"
                )
            b = cmap.get(url, cmap.get(key, body))
            extra_links = lmap.get(url, lmap.get(key, ""))
            html = (
                f"<html><head><title>{key[-14:]}</title></head>"
                f"<body><p>{b}</p>{extra_links}</body></html>"
            )
            return FetchResult(html=html, text=b, status=200)

        monkeypatch.setattr(http_mod.HTTPClient, "fetch", fetch)
        return state

    return install


@pytest.fixture
def make_crawler():
    crawlers = []

    def _make(db=None, http_cfg=None, **cfg):
        base = dict(max_depth=0, max_pages=20, respect_robots=False)
        base.update(cfg)
        # *.example is a non-resolving reserved TLD (RFC 2606): with the SSRF
        # guard on by default (0.15.0+) it would fail-closed as unresolvable.
        # These fixtures test crawler mechanics against mocked fetch, not the
        # guard itself, so private-network access is explicitly allowed.
        hc = http_cfg or HTTPConfig(verify_ssl=False, link_delay=0, allow_private_networks=True)
        c = WebCrawler(CrawlerConfig(**base), hc, db=db)
        crawlers.append(c)
        return c

    yield _make
    for c in crawlers:
        c.close()
