# -*- coding: utf-8 -*-
"""Automatic resource cleanup: callers (and agents) never need an explicit close().

HTTPClient and CrawlerDB arm a ``weakref.finalize`` so their underlying
session / browser / sqlite connection are released on GC or interpreter exit.
``close()`` / ``with`` stay available for deterministic release and disarm the
finalizer; a second close() is a safe no-op.
"""

from __future__ import annotations

import gc
import json

from lazycrawler import CrawlerConfig, CrawlerDB, DBConfig, HTTPConfig
from lazycrawler.http import HTTPClient
from lazycrawler.tools import CrawlerTools


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


# -- per-call release: sockets freed at the end of a call, lazily rebuilt --------


def test_httpclient_release_then_lazy_reuse():
    c = HTTPClient(HTTPConfig(verify_ssl=False))
    s1 = c.session
    c.release()
    assert c._session is None  # sockets freed at end of the "call"
    s2 = c.session  # rebuilt lazily on next use
    assert s2 is not None and s2 is not s1
    assert c._finalizer.alive  # GC/exit cleanup re-armed on the new session
    c.close()


def test_end_call_release_waits_for_concurrent(make_crawler):
    c = make_crawler()
    c._begin_call()
    c._begin_call()  # two calls in flight
    s = c._http.session
    c._end_call_release()  # one finishes -> must NOT free (other still running)
    assert c._http._session is s
    c._end_call_release()  # last finishes -> now it frees
    assert c._http._session is None


def test_tool_call_releases_http_and_reuses(stub_fetch, tmp_db):
    stub_fetch()
    ct = CrawlerTools(
        db=tmp_db,
        crawler_cfg=CrawlerConfig(respect_robots=False),
        http_cfg=HTTPConfig(verify_ssl=False, link_delay=0),
        content="pure",
    )
    out1 = json.loads(ct.web_crawl("https://e.org/a", depth=0))
    assert out1["found"] >= 1
    # HTTP released at the end of the tool call; counter back to zero
    assert ct._crawler._http._session is None
    assert ct._crawler._call_depth == 0
    # the shared DB cache is NOT closed by a tool call (still usable)
    gp = json.loads(ct.get_page("https://e.org/a"))
    assert gp.get("untrusted_page_text")
    # a subsequent tool call still works (session rebuilt lazily)
    out2 = json.loads(ct.web_crawl("https://e.org/b", depth=0))
    assert out2["found"] >= 1
    ct.close()
