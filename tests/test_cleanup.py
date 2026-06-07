# -*- coding: utf-8 -*-
"""Automatic resource cleanup: callers (and agents) never need an explicit close().

HTTPClient and CrawlerDB arm a ``weakref.finalize`` so their underlying
session / browser / sqlite connection are released on GC or interpreter exit.
``close()`` / ``with`` stay available for deterministic release and disarm the
finalizer; a second close() is a safe no-op.
"""

from __future__ import annotations

import gc

from lazycrawler import CrawlerDB, DBConfig, HTTPConfig
from lazycrawler.http import HTTPClient


def test_httpclient_finalizer_armed_and_disarmed_on_close():
    c = HTTPClient(HTTPConfig(verify_ssl=False))
    assert c._finalizer.alive  # auto-cleanup armed at construction
    c.close()
    assert not c._finalizer.alive  # disarmed once closed
    c.close()  # idempotent: a second close must not raise


def test_httpclient_auto_releases_on_gc():
    c = HTTPClient(HTTPConfig(verify_ssl=False))
    fin = c._finalizer
    assert fin.alive
    del c
    gc.collect()
    assert not fin.alive  # finalizer fired on GC -> session closed, no leak


def test_crawlerdb_finalizer_armed_and_disarmed_on_close(tmp_path):
    db = CrawlerDB(DBConfig(db_path=str(tmp_path / "cleanup.db")))
    assert db._finalizer.alive
    db.close()
    assert not db._finalizer.alive
    db.close()  # idempotent


def test_crawlerdb_auto_releases_on_gc(tmp_path):
    db = CrawlerDB(DBConfig(db_path=str(tmp_path / "cleanup_gc.db")))
    fin = db._finalizer
    assert fin.alive
    del db
    gc.collect()
    assert not fin.alive
