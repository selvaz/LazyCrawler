"""Shared cost-log path and news-run attribution for the two news jobs."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def resolve_cost_db_path(report_dir: Path) -> Path:
    """Use the deployment override, or one cost database beside the reports."""
    return Path(os.environ.get("NEWS_COST_DB") or report_dir / "news_costs.db")


def register_cost_session(
    cost_db_path: Path, news_session_id: str, bridge_session_id: str, process: str
) -> None:
    """Map a process's LazyBridge UUID to its news run in a short transaction."""
    con = sqlite3.connect(str(cost_db_path), timeout=30.0)
    try:
        with con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS news_cost_sessions (
                    news_session_id TEXT NOT NULL,
                    bridge_session_id TEXT NOT NULL,
                    process TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (news_session_id, bridge_session_id)
                )
                """
            )
            con.execute(
                """
                INSERT INTO news_cost_sessions
                    (news_session_id, bridge_session_id, process, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (news_session_id, bridge_session_id) DO NOTHING
                """,
                (
                    news_session_id,
                    bridge_session_id,
                    process,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
    finally:
        con.close()
