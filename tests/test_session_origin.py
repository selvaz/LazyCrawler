"""Session origin migration, validation, public APIs and reader contract (offline)."""

from __future__ import annotations

import inspect
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
        [
            "job:news",
            "job:calendar",
            "unknown",
            "interactive:test",
            "jobless",
            "JOB:news",
            "Job:news",
        ]
    ):
        tmp_db.create_session(str(i), origin=origin)
    tmp_db.conn.execute("UPDATE sessions SET origin=NULL WHERE session_id='4'")
    tmp_db.conn.commit()
    predicate = production_session_filter()
    assert predicate == "s.origin GLOB 'job:*'"
    # A second origin column must not make the helper ambiguous.
    rows = tmp_db.conn.execute(
        f"SELECT s.origin FROM sessions s CROSS JOIN (SELECT 'job:spoof' AS origin) other WHERE {predicate}"
    ).fetchall()
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
@pytest.mark.parametrize(
    "origin, expected",
    [
        (None, "job:env"),
        ("", "job:env"),
        ("interactive:tool", "interactive:tool"),
        ("job:host", "job:host"),
        ("invalid label", "unknown"),
        (0, "unknown"),
    ],
)
def test_tool_entry_points_persist_origin(
    tmp_db, stub_fetch, monkeypatch, method, origin, expected
):
    monkeypatch.setenv("LAZYCRAWLER_SESSION_ORIGIN", "job:env")
    stub_fetch()
    monkeypatch.setattr(
        "lazycrawler.search.search_ddg_urls", lambda *a, **kw: ["https://e.example/search"]
    )
    with CrawlerTools(
        db=tmp_db,
        origin=origin,
        content="pure",
        enforce_ssrf_guard=False,
        crawler_cfg=CrawlerConfig(max_depth=0, respect_robots=False),
        http_cfg=HTTPConfig(allow_private_networks=True, link_delay=0),
    ) as tools:
        arg = ["https://e.example/a"] if method == "web_crawl_many" else "https://e.example/a"
        tool = getattr(tools, method)
        # These signatures are what Tool.wrap exposes to the LLM. Neither a
        # spoofed production label nor an empty argument can override the host.
        assert "origin" not in inspect.signature(tool).parameters
        for spoof in ("job:spoofed", ""):
            with pytest.raises(TypeError, match="origin"):
                tool(arg, origin=spoof)
        output = json.loads(tool(arg))
    assert output["found"] == 1
    assert tmp_db.get_session(output["session_id"])["origin"] == expected


@pytest.mark.parametrize("origin", [None, "job:explicit", ""])
def test_reused_session_adopts_null_origin_only(tmp_db, monkeypatch, origin):
    monkeypatch.setenv("LAZYCRAWLER_SESSION_ORIGIN", "job:env")
    tmp_db.create_session("reused", topic="original", source="crawl")
    tmp_db.conn.execute("UPDATE sessions SET origin=NULL WHERE session_id='reused'")
    tmp_db.conn.commit()
    original = tmp_db.get_session("reused")
    tmp_db.create_session("reused", topic="replacement", source="search:duckduckgo", origin=origin)
    expected = "unknown" if origin == "" else (origin or "job:env")
    assert tmp_db.get_session("reused") == {**original, "origin": expected}
    tmp_db.create_session("reused", origin="interactive:later")
    assert tmp_db.get_session("reused")["origin"] == expected


@pytest.mark.parametrize("existing", ["", "unknown", "interactive:research", "job:first"])
def test_reused_session_preserves_any_non_null_origin(tmp_db, existing):
    tmp_db.create_session("reused")
    tmp_db.conn.execute("UPDATE sessions SET origin=? WHERE session_id='reused'", (existing,))
    tmp_db.conn.commit()
    tmp_db.create_session("reused", origin="job:later")
    assert tmp_db.get_session("reused")["origin"] == existing


def test_production_page_reached_by_at_least_one_job(tmp_db):
    from lazycrawler.http import url_hash

    for sid, origin in [("research", "interactive:research"), ("job", "job:news")]:
        tmp_db.create_session(sid, origin=origin)
    for name in ("shared", "research-only", "orphan"):
        tmp_db.upsert_page({"url": f"https://e.example/{name}", "status": "done", "mode": "pure"})
    shared = url_hash("https://e.example/shared")
    tmp_db.add_edge("research", shared)
    tmp_db.add_edge("job", shared)
    tmp_db.add_edge("research", url_hash("https://e.example/research-only"))
    rows = tmp_db.conn.execute(f"""
        SELECT p.url_hash FROM pages p WHERE EXISTS (
            SELECT 1 FROM crawl_edges e JOIN sessions s USING (session_id)
            WHERE e.url_hash=p.url_hash AND {production_session_filter()}
        )
    """).fetchall()
    assert [r[0] for r in rows] == [shared]
