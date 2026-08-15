# -*- coding: utf-8 -*-
"""econ_state.py -- tracks the last-seen release period per indicator, so
run_econ_monitor.py can tell "this is a fresh release" apart from "same
value as yesterday" without parsing any release calendar. Reactive/diff-based
by design: no calendar to keep in sync, self-corrects for holiday shifts and
BEA/Census schedule changes automatically.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

DEFAULT_STATE_DB = Path(__file__).resolve().parent / "econ_state.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS indicator_state (
    indicator_key TEXT PRIMARY KEY,
    last_period_date TEXT NOT NULL,
    last_period_label TEXT NOT NULL,
    last_value REAL NOT NULL,
    last_seen_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class EconState:
    def __init__(self, db_path: Path | str = DEFAULT_STATE_DB):
        self.db_path = str(db_path)
        with closing(sqlite3.connect(self.db_path)) as con:
            con.execute(_SCHEMA)
            con.commit()

    def last_period_date(self, indicator_key: str) -> str | None:
        """ISO date string of the last-seen period, or None if this indicator
        has never been recorded (first run -- treat as always new)."""
        with closing(sqlite3.connect(self.db_path)) as con:
            row = con.execute(
                "SELECT last_period_date FROM indicator_state WHERE indicator_key = ?",
                (indicator_key,),
            ).fetchone()
        return row[0] if row else None

    def mark_seen(
        self, indicator_key: str, period_date: str, period_label: str, value: float
    ) -> None:
        with closing(sqlite3.connect(self.db_path)) as con:
            con.execute(
                """
                INSERT INTO indicator_state
                    (indicator_key, last_period_date, last_period_label, last_value, last_seen_at)
                VALUES (?, ?, ?, ?, datetime('now'))
                ON CONFLICT(indicator_key) DO UPDATE SET
                    last_period_date = excluded.last_period_date,
                    last_period_label = excluded.last_period_label,
                    last_value = excluded.last_value,
                    last_seen_at = excluded.last_seen_at
                """,
                (indicator_key, period_date, period_label, value),
            )
            con.commit()
