# -*- coding: utf-8 -*-
"""CrawlerDB: schema, upsert/get, dedup, FTS, edges, links persistence."""

from __future__ import annotations

from lazycrawler.http import content_hash, url_hash


def _page(url, **kw):
    base = dict(
        url=url,
        url_hash=url_hash(url),
        status="done",
        mode="pure",
        clean_text="the federal budget was approved",
        title="Budget",
    )
    base.update(kw)
    return base


def test_upsert_roundtrip_and_json_fields(tmp_db):
    u = "https://x.example/full"
    tmp_db.upsert_page(
        _page(
            u,
            mode="smart",
            entities=["Congress"],
            topics=["budget", "politics"],
            sentiment="neutral",
            notes="tag:fiscal",
            data={"custom": 42},
            content_hash=content_hash("x"),
        )
    )
    row = tmp_db.get_page(url_hash(u))
    assert row["entities"] == ["Congress"]
    assert row["topics"] == ["budget", "politics"]
    assert row["sentiment"] == "neutral"
    assert row["notes"] == "tag:fiscal"
    assert row["data"] == {"custom": 42}


def test_links_persisted_and_deserialized(tmp_db):
    u = "https://x.example/withlinks"
    tmp_db.upsert_page(_page(u, links=[["A", "https://x.example/a"], ["B", "https://x.example/b"]]))
    row = tmp_db.get_page(url_hash(u))
    assert row["links"] == [["A", "https://x.example/a"], ["B", "https://x.example/b"]]


def test_links_default_empty(tmp_db):
    u = "https://x.example/nolinks"
    tmp_db.upsert_page(_page(u))
    assert tmp_db.get_page(url_hash(u))["links"] == []


def test_find_by_content_hash(tmp_db):
    ch = content_hash("shared body")
    tmp_db.upsert_page(_page("https://a.example/x", content_hash=ch))
    found = tmp_db.find_by_content_hash(ch)
    assert found and found["url"] == "https://a.example/x"
    assert tmp_db.find_by_content_hash("nope") is None


def test_fts_search_finds_page(tmp_db):
    u = "https://x.example/budget"
    tmp_db.upsert_page(_page(u, clean_text="the federal budget was approved by congress"))
    hits = tmp_db.search_text("federal budget")
    assert len(hits) == 1 and hits[0]["url"] == u


def test_search_text_limit_parameterized(tmp_db):
    for i in range(5):
        tmp_db.upsert_page(
            _page(f"https://x.example/p{i}", clean_text=f"common keyword item number {i}")
        )
    hits = tmp_db.search_text("keyword", limit=3)
    assert len(hits) == 3


def test_edges_idempotent_and_provenance(tmp_db):
    u = "https://x.example/edge"
    tmp_db.upsert_page(_page(u))
    tmp_db.create_session("s1", topic="t", seed=u)
    tmp_db.create_session("s2", topic="t", seed=u)
    tmp_db.add_edge("s1", url_hash(u), depth=0)
    tmp_db.add_edge("s2", url_hash(u), depth=1)
    tmp_db.add_edge("s1", url_hash(u), depth=0)  # duplicate -> ignored
    st = tmp_db.stats()
    assert st["pages"] == 1
    assert st["edges"] == 2
    assert st["sessions"] == 2


def test_get_pages_by_session(tmp_db):
    tmp_db.create_session("sess", topic="t", seed="x")
    for i in range(3):
        u = f"https://x.example/s{i}"
        tmp_db.upsert_page(_page(u))
        tmp_db.add_edge("sess", url_hash(u), depth=0)
    # a page not in the session
    tmp_db.upsert_page(_page("https://x.example/other"))
    rows = tmp_db.get_pages(session_id="sess", status="done")
    assert len(rows) == 3
    assert all(r["url"].endswith(("s0", "s1", "s2")) for r in rows)


def test_schema_user_version_set(tmp_db):
    v = int(tmp_db.conn.execute("PRAGMA user_version").fetchone()[0])
    assert v == tmp_db.SCHEMA_VERSION
