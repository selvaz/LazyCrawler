"""Rule-driven backfill: UTC bounds, preview, idempotency and writer locking."""

from __future__ import annotations

import json
import sqlite3

import pytest

from lazycrawler import CrawlerDB, DBConfig
from lazycrawler.backfill_session_origin import (
    OriginRule,
    _normalize_iso,
    classify_sessions,
    load_rules,
    main,
)
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
        if not cfg.read_only:
            assert db.conn.execute("PRAGMA busy_timeout").fetchone()[0] == int(
                BUSY_TIMEOUT_S * 1000
            )
            assert db.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert db.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        return db

    monkeypatch.setattr("lazycrawler.backfill_session_origin.CrawlerDB", writer)
    rules_path = _rules(tmp_path, [{"origin": "job:legacy", "source": "crawl"}])
    main(["--db", str(path), "--rules", str(rules_path), "--apply"])
    assert [cfg.read_only for cfg in opened] == [True, False]
    assert json.loads(capsys.readouterr().out)["origins"] == {"job:legacy": 1}
    with CrawlerDB(DBConfig(db_path=str(path), read_only=True)) as db:
        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 3
        assert db.get_session("old")["origin"] == "job:legacy"


@pytest.mark.parametrize("legacy", [False, True])
def test_dry_run_read_only_and_byte_identical(tmp_path, monkeypatch, capsys, legacy):
    path = tmp_path / "snapshot#origin.db"
    with sqlite3.connect(path) as con:
        schema = _SCHEMA_SQL.replace(",\n  origin       TEXT", "") if legacy else _SCHEMA_SQL
        con.executescript(schema)
        con.execute(f"PRAGMA user_version={2 if legacy else 3}")
        con.execute(
            "INSERT INTO sessions (session_id, created_at, mode, source) VALUES ('old', '2026-01-01', 'pure', 'crawl')"
        )
    rules_path = _rules(tmp_path, [{"origin": "job:preview", "source": "crawl"}])
    before = path.read_bytes(), path.stat().st_mtime_ns, set(tmp_path.iterdir())
    sql = []

    def reader(cfg):
        assert cfg.read_only, "dry-run must never open the writer"
        db = CrawlerDB(cfg)
        db.conn.set_trace_callback(sql.append)
        return db

    monkeypatch.setattr("lazycrawler.backfill_session_origin.CrawlerDB", reader)
    args = ["--db", str(path), "--rules", str(rules_path)]
    if legacy:
        with pytest.raises(SystemExit) as exc:
            main(args)
        assert exc.value.code == 2
        assert "sessions.origin is missing (user_version=2)" in capsys.readouterr().err
    else:
        main(args)
        assert json.loads(capsys.readouterr().out)["origins"] == {"job:preview": 1}
    assert (path.read_bytes(), path.stat().st_mtime_ns, set(tmp_path.iterdir())) == before
    assert all(statement.startswith(("SELECT", "PRAGMA")) for statement in sql)


@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize("kind", ["empty", "foreign", "impostor", "view", "garbage"])
def test_cli_refuses_non_crawler_files_without_writing(tmp_path, monkeypatch, apply, kind):
    path = tmp_path / "foreign.db"
    if kind in ("empty", "garbage"):
        path.write_bytes(b"" if kind == "empty" else b"not SQLite")
    else:
        with sqlite3.connect(path) as con:
            con.execute(
                {
                    "foreign": "CREATE TABLE other (value TEXT)",
                    "impostor": "CREATE TABLE sessions (session_id TEXT)",
                    "view": "CREATE VIEW sessions AS SELECT 1 AS session_id",
                }[kind]
            )
    rules_path = _rules(tmp_path, [])
    before = path.read_bytes(), set(tmp_path.iterdir())

    def reader(cfg):
        assert cfg.read_only, "invalid files must never open a writer"
        return CrawlerDB(cfg)

    monkeypatch.setattr("lazycrawler.backfill_session_origin.CrawlerDB", reader)
    with pytest.raises(SystemExit) as exc:
        main(["--db", str(path), "--rules", str(rules_path)] + (["--apply"] if apply else []))
    assert exc.value.code == 2
    assert (path.read_bytes(), set(tmp_path.iterdir())) == before


def test_dry_run_does_not_acquire_writer_reservation(tmp_db, tmp_path, capsys):
    _historical(tmp_db, "old", "2026-01-01")
    rules_path = _rules(tmp_path, [{"source": "crawl", "origin": "job:preview"}])
    tmp_db.conn.execute("BEGIN IMMEDIATE")
    try:
        assert main(["--db", tmp_db.cfg.db_path, "--rules", str(rules_path)]) == 0
        assert json.loads(capsys.readouterr().out)["origins"] == {"job:preview": 1}
    finally:
        tmp_db.conn.rollback()


@pytest.mark.parametrize("fraction", ["5", "12", "123", "1234", "12345", "123456", "1234567"])
def test_fractional_seconds_normalized_for_python310(tmp_path, tmp_db, fraction):
    microseconds = fraction[:6].ljust(6, "0")
    start = f"2026-01-02T12:00:00.{fraction}Z"
    assert _normalize_iso(start) == f"2026-01-02T12:00:00.{microseconds}+00:00"
    _historical(tmp_db, "fraction", start)
    rules = load_rules(
        _rules(
            tmp_path, [{"origin": "job:fraction", "start": start, "end": "2026-01-03T00:00:00Z"}]
        )
    )
    assert classify_sessions(tmp_db, rules)["origins"] == {"job:fraction": 1}


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
