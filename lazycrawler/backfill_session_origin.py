"""Classify historical NULL origins: python -m lazycrawler.backfill_session_origin.

Rules are a JSON array of {origin, source?, start?, end?}. Selectors within a
rule are ANDed, rules run in file order, and the first match wins. Time bounds
are UTC ISO 8601 timestamps; start is inclusive and end is exclusive.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Sequence

from .config import DBConfig
from .db import CrawlerDB, _parse_iso
from .session_origin import valid_session_origin


def _utc_bound(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("time bounds must be UTC ISO 8601 strings")
    try:
        # Python 3.10 does not accept the Z suffix directly.
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid UTC time bound: {value!r}") from exc
    if dt.tzinfo is None or dt.utcoffset() != timedelta(0):
        raise ValueError(f"time bound must specify UTC (Z or +00:00): {value!r}")
    return dt


@dataclass(frozen=True)
class OriginRule:
    origin: str
    source: Optional[str] = None
    start: Optional[datetime] = None
    end: Optional[datetime] = None

    def matches(self, created_at: str, source: str) -> bool:
        if self.source is not None and source != self.source:
            return False
        if self.start is None and self.end is None:
            return True
        # Stored Z timestamps also need normalization on Python 3.10.
        timestamp = _parse_iso(created_at.replace("Z", "+00:00"))
        if timestamp is None:
            return False
        return (self.start is None or timestamp >= self.start) and (
            self.end is None or timestamp < self.end
        )


def load_rules(path: Path) -> list[OriginRule]:
    """Validate the entire rules file before opening the target database.

    Invalid rule origins are errors, rather than guessed classifications.
    Source-only rules can classify sessions with unparseable created_at values;
    time rules cannot. Legacy naive stored timestamps are interpreted as UTC,
    consistent with CrawlerDB's timestamp parser.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("rules must be a JSON array")
    rules = []
    for index, item in enumerate(data, 1):
        if not isinstance(item, dict) or set(item) - {"origin", "source", "start", "end"}:
            raise ValueError(f"rule {index}: expected origin and source/start/end selectors")
        if not valid_session_origin(item.get("origin")):
            raise ValueError(
                f"rule {index}: invalid origin (1–64 printable characters, no whitespace)"
            )
        if not {"source", "start", "end"} & item.keys():
            raise ValueError(f"rule {index}: at least one source/start/end selector is required")
        source = item.get("source")
        if "source" in item and (not isinstance(source, str) or not source):
            raise ValueError(f"rule {index}: source must be a non-empty string")
        start = _utc_bound(item["start"]) if "start" in item else None
        end = _utc_bound(item["end"]) if "end" in item else None
        if start is not None and end is not None and start >= end:
            raise ValueError(f"rule {index}: start must be before end")
        rules.append(OriginRule(origin=item["origin"], source=source, start=start, end=end))
    return rules


def classify_sessions(db: CrawlerDB, rules: Sequence[OriginRule], *, apply: bool = False) -> dict:
    """Preview or atomically apply first-match rules to NULL origins only.

    Use the normal writer's RLock and SQLite busy timeout. BEGIN IMMEDIATE
    holds the writer reservation across selection and update, so concurrent
    classifiers/writers cannot change the candidate set mid-transaction.
    """
    counts: Counter[str] = Counter()
    unmatched = 0
    with db._lock:
        try:
            db.conn.execute("BEGIN IMMEDIATE")
            rows = db.conn.execute(
                "SELECT session_id, created_at, source FROM sessions WHERE origin IS NULL"
            ).fetchall()
            for row in rows:
                rule = next((r for r in rules if r.matches(row["created_at"], row["source"])), None)
                if rule is None:
                    unmatched += 1
                    continue
                counts[rule.origin] += 1
                if apply:
                    db.conn.execute(
                        "UPDATE sessions SET origin=? WHERE session_id=? AND origin IS NULL",
                        (rule.origin, row["session_id"]),
                    )
            if apply:
                db.conn.commit()
            else:
                db.conn.rollback()
        except BaseException:
            db.conn.rollback()
            raise
    return {"origins": dict(sorted(counts.items())), "unmatched_null": unmatched}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path, help="Existing crawl SQLite database")
    parser.add_argument("--rules", required=True, type=Path, help="JSON rules file")
    parser.add_argument(
        "--apply", action="store_true", help="Write classifications (default: preview)"
    )
    args = parser.parse_args(argv)
    try:
        rules = load_rules(args.rules)
        if not args.db.is_file():
            raise ValueError(f"database must already exist: {args.db}")
        # The regular writer performs migrations and configures WAL, foreign keys
        # and busy timeout. No alternative sqlite connection or lock bypass.
        with CrawlerDB(DBConfig(db_path=str(args.db), enable_fts=False)) as db:
            result = classify_sessions(db, rules, apply=args.apply)
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(2, f"error: {exc}\n")
    print(json.dumps({"mode": "apply" if args.apply else "dry-run", **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
