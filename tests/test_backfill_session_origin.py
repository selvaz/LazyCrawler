"""Rule-driven backfill: UTC bounds, preview, idempotency and writer locking."""

from __future__ import annotations

import json
import sqlite3

import pytest

from lazycrawler import CrawlerDB, DBConfig
from lazycrawler.backfill_session_origin import OriginRule, classify_sessions, load_rules, main
from lazycrawler.db import _SCHEMA_SQL, BUSY_TIMEOUT_S


def _rules(tmp_path, data):
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _historical(db, sid, created_at, source="crawl", origin=None):
    db.conn.execute(
        "INSERT INTO sessions (session_id, created_at, mode, source, origin) VALUES (?, ?, 'pure', ?, ?)",
        (sid, created_at, source, origin),
    )
    db.conn.commit()


def test_cli_preview_apply_and_idempotency(tmp_path, tmp_db, capsys):
    for sid, timestamp, source, origin in [
        ("before", "2026-01-01T23:59:59+00:00", "crawl", None),
        ("start", "2026-01-02T00:00:00Z", "crawl", None),
        ("offset", "2026-01-01T19:00:00-05:00", "crawl", None),
        ("inside", "2026-01-02T12:00:00+00:00", "search:duckduckgo", None),
        ("end", "2026-01-03T00:00:00+00:00", "crawl", None),
        ("source-only", "bad timestamp", "search:duckduckgo", None),
        ("invalid-time", "bad timestamp", "crawl", None),
        ("preserve", "2026-01-02T12:00:00+00:00", "crawl", "unknown"),
    ]:
        _historical(tmp_db, sid, timestamp, source, origin)
    rules_path = _rules(
        tmp_path,
        [
            {
                "origin": "job:window",
                "start": "2026-01-02T00:00:00Z",
                "end": "2026-01-03T00:00:00+00:00",
                "source": "crawl",
            },
            {"origin": "interactive:search", "source": "search:duckduckgo"},
            # Overlaps first rule: first match wins.
            {"origin": "job:later", "start": "2026-01-02T00:00:00Z", "end": "2026-01-03T00:00:00Z"},
        ],
    )
    args = ["--db", tmp_db.cfg.db_path, "--rules", str(rules_path)]
    expected = {"origins": {"job:window": 2, "interactive:search": 2}, "unmatched_null": 3}
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out) == {"mode": "dry-run", **expected}
    assert all(r["origin"] is None for r in tmp_db.get_sessions() if r["session_id"] != "preserve")
    assert main(args + ["--apply"]) == 0
    assert json.loads(capsys.readouterr().out) == {"mode": "apply", **expected}
    assert tmp_db.get_session("start")["origin"] == "job:window"
    assert tmp_db.get_session("offset")["origin"] == "job:window"
    assert tmp_db.get_session("inside")["origin"] == "interactive:search"
    assert tmp_db.get_session("preserve")["origin"] == "unknown"
    assert main(args + ["--apply"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "mode": "apply",
        "origins": {},
        "unmatched_null": 3,
    }


@pytest.mark.parametrize(
    "rule",
    [
        {"origin": "job:a", "start": "2026-01-02T00:00:00Z"},
        {"origin": "job:a", "end": "2026-01-03T00:00:00Z"},
    ],
)
def test_open_ended_windows_and_naive_legacy_timestamps(tmp_path, tmp_db, rule):
    _historical(tmp_db, "legacy", "2026-01-02T12:00:00")
    rules = load_rules(_rules(tmp_path, [rule]))
    assert classify_sessions(tmp_db, rules)["origins"] == {"job:a": 1}


@pytest.mark.parametrize(
    "data",
    [
        {},
        [1],
        [{"origin": "job:a"}],
        [{"origin": "job:a", "typo": "crawl"}],
        [{"source": "crawl"}],
        [{"origin": "bad value", "source": "crawl"}],
        [{"origin": "job:a", "source": None}],
        [{"origin": "job:a", "source": ""}],
        [{"origin": "job:a", "start": "2026-01-01"}],
        [{"origin": "job:a", "start": "2026-01-01T00:00:00-05:00"}],
        [{"origin": "job:a", "start": "bad"}],
        [{"origin": "job:a", "start": None}],
        [{"origin": "job:a", "start": "2026-01-02T00:00:00Z", "end": "2026-01-01T00:00:00Z"}],
        [{"origin": "job:a", "start": "2026-01-01T00:00:00Z", "end": "2026-01-01T00:00:00Z"}],
    ],
)
def test_invalid_rules_rejected_before_opening_database(tmp_path, data):
    path = tmp_path / "must-not-create.db"
    rules_path = _rules(tmp_path, data)
    with pytest.raises(SystemExit) as exc:
        main(["--db", str(path), "--rules", str(rules_path), "--apply"])
    assert exc.value.code == 2
    assert not path.exists()


def test_missing_db_and_malformed_json(tmp_path):
    args = ["--db", str(tmp_path / "absent.db"), "--rules", str(_rules(tmp_path, []))]
    with pytest.raises(SystemExit):
        main(args)
    (tmp_path / "rules.json").write_text("not json", encoding="utf-8")
    with pytest.raises(SystemExit):
        main(args)
    assert not (tmp_path / "absent.db").exists()


def test_cli_uses_normal_writer_and_migrates_legacy_db(tmp_path, capsys, monkeypatch):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as con:
        con.executescript(_SCHEMA_SQL.replace(",\n  origin       TEXT", ""))
        con.execute("PRAGMA user_version=2")
        con.execute("INSERT INTO sessions VALUES ('old', '2026-01-01', '', '', 'pure', 'crawl')")
    opened = []

    def writer(cfg):
        db = CrawlerDB(cfg)
        opened.append(cfg)
        assert db.conn.execute("PRAGMA busy_timeout").fetchone()[0] == int(BUSY_TIMEOUT_S * 1000)
        assert db.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        return db

    monkeypatch.setattr("lazycrawler.backfill_session_origin.CrawlerDB", writer)
    rules_path = _rules(tmp_path, [{"origin": "job:legacy", "source": "crawl"}])
    main(["--db", str(path), "--rules", str(rules_path)])
    assert len(opened) == 1 and not opened[0].read_only
    assert json.loads(capsys.readouterr().out)["origins"] == {"job:legacy": 1}
    with CrawlerDB(DBConfig(db_path=str(path), read_only=True)) as db:
        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 3
        assert db.get_session("old")["origin"] is None


def test_backfill_respects_other_writer_lock(tmp_db):
    _historical(tmp_db, "old", "2026-01-01")
    with CrawlerDB(DBConfig(db_path=tmp_db.cfg.db_path)) as other:
        other.conn.execute("PRAGMA busy_timeout=1")  # Fail promptly under the held lock.
        tmp_db.conn.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                classify_sessions(other, [OriginRule("job:locked", source="crawl")], apply=True)
        finally:
            tmp_db.conn.rollback()
        assert other.get_session("old")["origin"] is None
        assert classify_sessions(other, [OriginRule("job:locked", source="crawl")], apply=True)[
            "origins"
        ] == {"job:locked": 1}


def test_backfill_rolls_back_entire_batch(tmp_db):
    _historical(tmp_db, "a", "2026-01-01", "first")
    _historical(tmp_db, "b", "2026-01-01", "second")
    tmp_db.conn.execute(
        "CREATE TRIGGER reject_second BEFORE UPDATE ON sessions WHEN NEW.origin='job:second' BEGIN SELECT RAISE(ABORT, 'test failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="test failure"):
        classify_sessions(
            tmp_db,
            [OriginRule("job:first", source="first"), OriginRule("job:second", source="second")],
            apply=True,
        )
    assert all(row["origin"] is None for row in tmp_db.get_sessions())
    assert not tmp_db.conn.in_transaction
