"""Offline coverage of shared news costs, including real LazyBridge Sessions."""

from __future__ import annotations

import json
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import make_news_report as report  # noqa: E402
from news_costs import register_cost_session, resolve_cost_db_path  # noqa: E402

ZERO_USAGE = {
    "total": {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0},
    "by_agent": {},
}


@pytest.mark.parametrize("override", [None, "", "custom/costs.db"])
def test_cost_db_path(monkeypatch, tmp_path, override):
    monkeypatch.delenv("NEWS_COST_DB", raising=False)
    if override is not None:
        monkeypatch.setenv("NEWS_COST_DB", override)
    expected = Path(override) if override else tmp_path / "news_costs.db"
    assert resolve_cost_db_path(tmp_path) == expected


def test_usage_is_scoped_to_mapped_sessions(tmp_path):
    """Exercise attribution even in CI environments without the smart extra."""
    path = tmp_path / "news_costs.db"
    register_cost_session(path, "news_a", "crawl-a", "crawl")
    register_cost_session(path, "news_a", "report-a", "report")
    register_cost_session(path, "news_a", "report-a", "report")
    register_cost_session(path, "news_b", "crawl-b", "crawl")
    with closing(sqlite3.connect(path)) as con, con:
        con.execute("CREATE TABLE events (session_id TEXT, event_type TEXT, payload TEXT)")
        con.executemany(
            "INSERT INTO events VALUES (?, ?, ?)",
            [
                (
                    session_id,
                    event_type,
                    json.dumps(
                        {
                            "agent_name": agent,
                            "input_tokens": tokens,
                            "output_tokens": 2 * tokens,
                            "cost_usd": tokens / 100,
                        }
                    ),
                )
                for session_id, event_type, agent, tokens in [
                    ("crawl-a", "model_response", "extraction", 10),
                    ("report-a", "model_response", "extraction", 20),
                    ("report-a", "model_response", "digest", 30),
                    ("crawl-b", "model_response", "extraction", 1000),
                    ("unregistered", "model_response", "other", 2000),
                    ("crawl-a", "model_request", "extraction", 3000),
                ]
            ],
        )
    summary = report._usage_from_cost_db(path, "news_a")
    assert summary == {
        "total": {"input_tokens": 60, "output_tokens": 120, "cost_usd": pytest.approx(0.6)},
        "by_agent": {
            "extraction": {"input_tokens": 30, "output_tokens": 60, "cost_usd": pytest.approx(0.3)},
            "digest": {"input_tokens": 30, "output_tokens": 60, "cost_usd": 0.3},
        },
    }
    assert report._usage_from_cost_db(path, "news_b")["total"]["input_tokens"] == 1000
    assert report._usage_from_cost_db(path, "missing") == ZERO_USAGE
    text = report.build_cost_report("news_a", 2, 1, path)
    assert "**Total cost: $0.6000**" in text
    assert "| extraction | 30 | 60 | $0.3000 |" in text


def test_missing_cost_db_returns_zero_without_creating_file(tmp_path):
    path = tmp_path / "missing.db"
    assert report._usage_from_cost_db(path, "news_a") == ZERO_USAGE
    assert not path.exists()


def test_concurrent_registration_is_idempotent(tmp_path):
    path = tmp_path / "news_costs.db"

    def register(i):
        register_cost_session(path, "news_a", f"bridge-{i % 4}", "crawl")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(register, range(32)))
    with closing(sqlite3.connect(path)) as con:
        rows = con.execute(
            "SELECT news_session_id, bridge_session_id, process, created_at FROM news_cost_sessions"
        ).fetchall()
    assert len(rows) == 4
    assert {row[1] for row in rows} == {f"bridge-{i}" for i in range(4)}
    for news_id, _, process, created_at in rows:
        assert (news_id, process) == ("news_a", "crawl")
        assert datetime.fromisoformat(created_at).utcoffset().total_seconds() == 0


@pytest.mark.parametrize("override", [False, True])
def test_both_jobs_share_real_sessions_and_exclude_another_run(monkeypatch, tmp_path, override):
    """Run both entry points with local stubs; Session/event persistence is real."""
    bridge = pytest.importorskip("lazybridge.session")
    import run_news_crawl as crawl

    report_dir = tmp_path / "reports"
    path = tmp_path / "custom" / "costs.db" if override else report_dir / "news_costs.db"
    monkeypatch.delenv("NEWS_COST_DB", raising=False)
    if override:
        monkeypatch.setenv("NEWS_COST_DB", str(path))
    monkeypatch.setattr(crawl, "REPORT_DIR", report_dir)
    monkeypatch.setattr(report, "REPORT_DIR", report_dir)
    pages = [{"mode": "smart"}]
    db = SimpleNamespace(close=lambda: None, get_pages=lambda **kwargs: pages)
    monkeypatch.setattr(crawl, "CrawlerDB", lambda cfg: db)
    monkeypatch.setattr(report, "CrawlerDB", lambda cfg: db)
    source = SimpleNamespace(
        name="stub", mode="smart", url="feed", category="macro", region="US", lang="en"
    )
    monkeypatch.setattr(crawl, "load_sources", lambda path: [source])
    monkeypatch.setattr(crawl, "_feed_items", lambda *args: ([("title", "article")], 0))

    def emit(session, agent, tokens):
        session.emit(
            bridge.EventType.MODEL_RESPONSE,
            {
                "agent_name": agent,
                "input_tokens": tokens,
                "output_tokens": tokens,
                "cost_usd": tokens / 100,
            },
        )

    def crawler_factory(**kwargs):
        def do_crawl(*args, **unused):
            emit(kwargs["llm_cfg"].session, "extraction", 10)
            return [SimpleNamespace(status="done", url="article")]

        return SimpleNamespace(crawl=do_crawl)

    monkeypatch.setattr(crawl, "WebCrawler", crawler_factory)
    monkeypatch.setattr(report, "_load_meta", lambda session_id: {})
    monkeypatch.setattr(report, "_enrich", lambda pages, meta: pages)
    monkeypatch.setattr(report, "_group_by_region", lambda pages: {})
    monkeypatch.setattr(
        report,
        "generate_index_summaries",
        lambda pages, cost_session: emit(cost_session, "index", 20),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_news_crawl.py", "--sources-config", "stub.yaml", "--session-id", "news_a"],
    )
    assert crawl.main() == 0

    # Another news run and an unregistered Session must not enter news_a's report.
    with bridge.Session(db=str(path)) as other:
        register_cost_session(path, "news_b", other.session_id, "crawl")
        emit(other, "extraction", 1000)
    with bridge.Session(db=str(path)) as unrelated:
        emit(unrelated, "other", 2000)

    monkeypatch.setattr(
        sys, "argv", ["make_news_report.py", "--session-id", "news_a", "--no-digest"]
    )
    assert report.main() == 0
    summary = report._usage_from_cost_db(path, "news_a")
    assert summary["total"] == {
        "input_tokens": 30,
        "output_tokens": 30,
        "cost_usd": pytest.approx(0.3),
    }
    assert set(summary["by_agent"]) == {"extraction", "index"}
    assert report._usage_from_cost_db(path, "news_b")["total"]["input_tokens"] == 1000
    with closing(sqlite3.connect(path)) as con:
        mappings = con.execute(
            "SELECT bridge_session_id, process FROM news_cost_sessions WHERE news_session_id = 'news_a'"
        ).fetchall()
    assert len({row[0] for row in mappings}) == 2
    assert {row[1] for row in mappings} == {"crawl", "report"}
    assert "**Total cost: $0.3000**" in (report_dir / "news_cost_news_a.md").read_text(
        encoding="utf-8"
    )
    assert list(tmp_path.rglob("*_cost.db")) == []
    assert set(tmp_path.rglob("*.db")) == {path}
