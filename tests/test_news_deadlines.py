from __future__ import annotations

import sys
import time
from types import SimpleNamespace

import run_news_crawl as runner


def _busy_or_complete(job, messages, crawlers):
    messages.put(("phase", "extraction", 0.01))
    if "stall" in job.url:
        while True:
            pass
    return runner.ArticleOutcome("done", job.url, 0.01, 0.01, 0.02)


def _job(url: str) -> runner.ArticleJob:
    return runner.ArticleJob(url, "ml", "topic", "session", "test", "db", "cost-db")


def test_gil_holding_article_is_killed_and_next_article_completes():
    config = runner.WorkerConfig("db", "cost-db", "session")
    worker = runner.PersistentWorker(
        config, startup_timeout=10, work_item=_busy_or_complete
    )
    started = time.monotonic()
    try:
        stalled, phase = worker.article(_job("https://example.test/stall"), 0.5)
        completed, _ = worker.article(_job("https://example.test/good"), 0.5)
    finally:
        worker.close()

    assert stalled is None
    assert phase == "extraction"
    assert completed is not None and completed.status == "done"
    # Worker import/spawn is explicitly not part of either 0.5s item deadline.
    assert time.monotonic() - started < 10.0


class _FakeWorker:
    def __init__(self, *args, **kwargs):
        pass

    def feed(self, *args):
        return ([("one", "https://e/1"), ("two", "https://e/2")], 0)

    def article(self, job, deadline):
        time.sleep(0.02)
        return runner.ArticleOutcome("done", job.url, 0.0, 0.02, 0.02), "extraction"

    def close(self):
        pass


def test_wall_budget_skips_remaining_items_but_exposes_session_for_digest(
    monkeypatch, tmp_path, capsys
):
    source = SimpleNamespace(
        name="source", mode="ml", url="feed", category="macro", region="US", lang="en"
    )
    monkeypatch.setattr(runner, "REPORT_DIR", tmp_path)
    monkeypatch.setattr(
        runner, "resolve_cost_db_path", lambda path: tmp_path / "cost.db"
    )
    monkeypatch.setattr(runner, "load_sources", lambda path: [source])
    monkeypatch.setattr(runner, "PersistentWorker", _FakeWorker)
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
    assert "SESSION_ID=budget-test" in output


def test_report_meta_reader_skips_run_summary(monkeypatch, tmp_path):
    import json
    import make_news_report as report

    monkeypatch.setattr(report, "REPORT_DIR", tmp_path)
    (tmp_path / "news_meta.json").write_text(
        json.dumps(
            {
                "https://example.test/article": {"name": "source"},
                "_run_summary": {"partial": True},
            }
        ),
        encoding="utf-8",
    )
    assert report._load_meta("news") == {
        "https://example.test/article": {"name": "source"}
    }
