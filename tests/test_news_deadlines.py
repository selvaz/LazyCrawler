from __future__ import annotations

import sys
import time
from types import SimpleNamespace

import run_news_crawl as runner


def _busy_or_complete(job, messages):
    messages.put(("phase", "extraction", 0.01))
    if "stall" in job.url:
        while True:
            pass
    messages.put(("outcome", runner.ArticleOutcome("done", job.url, 0.01, 0.01, 0.02)))


def _job(url: str) -> runner.ArticleJob:
    return runner.ArticleJob(url, "ml", "topic", "session", "test", "db", "cost-db")


def test_gil_holding_article_is_killed_and_next_article_completes():
    started = time.monotonic()
    stalled, phase = runner.run_article_with_deadline(
        _job("https://example.test/stall"), 0.5, worker=_busy_or_complete
    )
    completed, _ = runner.run_article_with_deadline(
        _job("https://example.test/good"), 0.5, worker=_busy_or_complete
    )

    assert stalled is None
    assert phase == "extraction"
    assert completed is not None and completed.status == "done"
    assert time.monotonic() - started < 2.0


def test_wall_budget_skips_remaining_items_but_exposes_session_for_digest(
    monkeypatch, tmp_path, capsys
):
    source = SimpleNamespace(
        name="source", mode="ml", url="feed", category="macro", region="US", lang="en"
    )
    monkeypatch.setattr(runner, "REPORT_DIR", tmp_path)
    monkeypatch.setattr(runner, "resolve_cost_db_path", lambda path: tmp_path / "cost.db")
    monkeypatch.setattr(runner, "load_sources", lambda path: [source])
    monkeypatch.setattr(
        runner,
        "_feed_items",
        lambda *args: ([("one", "https://e/1"), ("two", "https://e/2")], 0),
    )

    def slow_success(job, deadline):
        time.sleep(0.02)
        return runner.ArticleOutcome("done", job.url, 0.0, 0.02, 0.02), "extraction"

    monkeypatch.setattr(runner, "run_article_with_deadline", slow_success)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_news_crawl.py",
            "--sources-config",
            "unused.yaml",
            "--session-id",
            "budget-test",
            "--wall-budget-minutes",
            "0.0001",
        ],
    )

    assert runner.main() == 0
    output = capsys.readouterr().out
    assert "skipping 1 remaining" in output
    assert "SESSION_ID=budget-test" in output  # wrapper therefore invokes digest stage
