"""Session origin migration, validation, public APIs and reader contract (offline)."""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from lazycrawler import (
    CrawlerConfig,
    CrawlerDB,
    CrawlerTools,
    DBConfig,
    HTTPConfig,
    SearchConfig,
    WebSearch,
    production_session_filter,
)
from lazycrawler.db import _SCHEMA_SQL


@pytest.mark.parametrize("version", [0, 1, 2])
def test_legacy_database_migrates_in_place(tmp_path, version):
    path = str(tmp_path / "legacy.db")
    with sqlite3.connect(path) as con:
        con.executescript(_SCHEMA_SQL.replace(",\n  origin       TEXT", ""))
        con.execute(f"PRAGMA user_version={version}")
        con.execute(
            "INSERT INTO sessions VALUES ('old', '2026-01-01T00:00:00+00:00', 't', 's', 'pure', 'crawl')"
        )
    # New read-only readers also work before migration without writing the file.
    with CrawlerDB(DBConfig(db_path=path, read_only=True)) as db:
        assert db.get_session("old")["origin"] is None
        assert db.get_sessions()[0]["origin"] is None
        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == version
    for _ in range(2):
        with CrawlerDB(DBConfig(db_path=path)) as db:
            assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 3
            row = db.get_session("old")
            assert row["origin"] is None and row["topic"] == "t"
            assert db.get_session("absent") is None
            cols = {r[1]: r for r in db.conn.execute("PRAGMA table_info(sessions)")}
            assert cols["origin"][2:5] == ("TEXT", 0, None)
    # Older code has no upper-version gate and uses named insert columns. Those
    # statements continue to read/write migrated files; old writers leave NULL.
    with sqlite3.connect(path) as con:
        con.execute(
            "INSERT INTO sessions (session_id, created_at, topic, seed, mode, source) VALUES ('older-writer', '2026-01-02', '', '', 'pure', 'crawl')"
        )
        assert con.execute(
            "SELECT origin FROM sessions WHERE session_id='older-writer'"
        ).fetchone() == (None,)
        assert con.execute(
            "SELECT session_id, created_at, topic, seed, mode, source FROM sessions"
        ).fetchall()
        assert con.execute("PRAGMA user_version").fetchone()[0] == 3


def test_duplicate_column_at_legacy_version_is_idempotent(tmp_db):
    tmp_db.conn.execute("PRAGMA user_version=2")
    tmp_db._migrate()
    assert tmp_db.conn.execute("PRAGMA user_version").fetchone()[0] == 3


def test_origin_resolution_and_session_reads(tmp_db, monkeypatch):
    monkeypatch.delenv("LAZYCRAWLER_SESSION_ORIGIN", raising=False)
    tmp_db.create_session("default")
    assert tmp_db.get_session("default")["origin"] == "unknown"
    monkeypatch.setenv("LAZYCRAWLER_SESSION_ORIGIN", "job:news")
    tmp_db.create_session("env")
    tmp_db.create_session("explicit", origin="interactive:research")
    tmp_db.create_session("explicit", origin="job:replacement")
    assert tmp_db.get_session("env")["origin"] == "job:news"
    assert tmp_db.get_session("explicit")["origin"] == "interactive:research"
    assert {r["origin"] for r in tmp_db.get_sessions()} == {
        "unknown",
        "job:news",
        "interactive:research",
    }


@pytest.mark.parametrize(
    "value", ["", "x" * 65, "a b", " a", "a\t", "a\n", "a\x00", "a\x7f", "a\u00a0b", "a\u200bb", 42]
)
@pytest.mark.parametrize("via_env", [False, True])
def test_invalid_origin_warns_and_falls_back(tmp_db, monkeypatch, caplog, value, via_env):
    if via_env and not isinstance(value, str):
        return  # Environment variables are always strings.
    # Mock this module's environment mapping so even NUL can be validated;
    # native process environments cannot reliably represent embedded NULs.
    monkeypatch.setattr(
        "lazycrawler.session_origin.os",
        SimpleNamespace(
            environ={"LAZYCRAWLER_SESSION_ORIGIN": value if via_env else "job:valid-env"}
        ),
    )
    # The package logger deliberately does not propagate to the root logger.
    caplog.set_level(logging.WARNING, logger="lazycrawler")
    logger = logging.getLogger("lazycrawler")
    logger.addHandler(caplog.handler)
    try:
        tmp_db.create_session("bad", **({} if via_env else {"origin": value}))
    finally:
        logger.removeHandler(caplog.handler)
    assert tmp_db.get_session("bad")["origin"] == "unknown"
    assert "Invalid session origin" in caplog.text


@pytest.mark.parametrize("value", ["x", "x" * 64, "job:news", "interactive:é", "punct:'%_;"])
def test_valid_origin_is_preserved(tmp_db, value):
    tmp_db.create_session("ok", origin=value)
    assert tmp_db.get_session("ok")["origin"] == value


def test_production_filter(tmp_db):
    for i, origin in enumerate(
        ["job:news", "job:calendar", "unknown", "interactive:test", "jobless"]
    ):
        tmp_db.create_session(str(i), origin=origin)
    tmp_db.conn.execute("UPDATE sessions SET origin=NULL WHERE session_id='4'")
    tmp_db.conn.commit()
    predicate = production_session_filter()
    assert predicate == "origin LIKE 'job:%'"
    rows = tmp_db.conn.execute(f"SELECT origin FROM sessions WHERE {predicate}").fetchall()
    assert {r[0] for r in rows} == {"job:news", "job:calendar"}


@pytest.mark.parametrize("many", [False, True])
@pytest.mark.parametrize("origin", [None, "interactive:explicit"])
def test_sync_crawlers_persist_origin(tmp_db, make_crawler, stub_fetch, monkeypatch, many, origin):
    monkeypatch.setenv("LAZYCRAWLER_SESSION_ORIGIN", "job:sync")
    stub_fetch()
    crawler = make_crawler(db=tmp_db)
    method = crawler.crawl_many if many else crawler.crawl
    method(
        ["https://e.example/a"] if many else "https://e.example/a", session_id="sync", origin=origin
    )
    assert tmp_db.get_session("sync")["origin"] == (origin or "job:sync")


@pytest.mark.parametrize("engine", ["duckduckgo", "brave", "tavily", "gemini"])
@pytest.mark.parametrize("origin", [None, "interactive:explicit"])
def test_search_engines_persist_origin(tmp_db, stub_fetch, monkeypatch, engine, origin):
    monkeypatch.setenv("LAZYCRAWLER_SESSION_ORIGIN", "job:search")
    if engine == "gemini":
        fake_agent = MagicMock()
        fake_agent.return_value.return_value.text.return_value = "Grounded answer"
        monkeypatch.setitem(
            sys.modules,
            "lazybridge",
            SimpleNamespace(
                Agent=fake_agent,
                LLMEngine=MagicMock(),
                NativeTool=SimpleNamespace(GOOGLE_SEARCH="search"),
            ),
        )
    else:
        monkeypatch.setattr(
            f"lazycrawler.search.search_{'ddg' if engine == 'duckduckgo' else engine}_urls",
            lambda *a, **kw: ["https://e.example/search"],
        )
        stub_fetch()
    with WebSearch(
        SearchConfig(engine=engine),
        crawler_cfg=CrawlerConfig(max_depth=0, respect_robots=False),
        http_cfg=HTTPConfig(allow_private_networks=True, link_delay=0),
        db=tmp_db,
    ) as search:
        out = search.run("test", origin=origin)
    assert out["pages_found"] == 1
    assert tmp_db.get_sessions()[0]["origin"] == (origin or "job:search")


@pytest.mark.parametrize("method", ["web_crawl", "web_crawl_many", "web_search"])
def test_tool_entry_points_persist_origin(tmp_db, stub_fetch, monkeypatch, method):
    stub_fetch()
    monkeypatch.setattr(
        "lazycrawler.search.search_ddg_urls", lambda *a, **kw: ["https://e.example/search"]
    )
    with CrawlerTools(
        db=tmp_db,
        content="pure",
        enforce_ssrf_guard=False,
        crawler_cfg=CrawlerConfig(max_depth=0, respect_robots=False),
        http_cfg=HTTPConfig(allow_private_networks=True, link_delay=0),
    ) as tools:
        arg = ["https://e.example/a"] if method == "web_crawl_many" else "https://e.example/a"
        output = json.loads(getattr(tools, method)(arg, origin="interactive:tool"))
    assert output["found"] == 1
    assert tmp_db.get_session(output["session_id"])["origin"] == "interactive:tool"
