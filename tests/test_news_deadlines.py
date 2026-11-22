from __future__ import annotations

import queue
import sys
import time
from types import SimpleNamespace

import pytest

import run_news_crawl as runner
from lazycrawler import CrawlerConfig, HTTPConfig, MLConfig, WebCrawler
from lazycrawler.http import FetchResult, HTTPClient


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
    worker = runner.PersistentWorker(config, startup_timeout=10, work_item=_busy_or_complete)
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
    # Two spawns (initial + after the kill) cost ~5 s each on Windows; the bound
    # only proves the run does not wait for the stalled item.
    assert time.monotonic() - started < 30.0


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
    monkeypatch.setattr(runner, "resolve_cost_db_path", lambda path: tmp_path / "cost.db")
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
    assert "skipping 2 remaining" in output
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
    assert report._load_meta("news") == {"https://example.test/article": {"name": "source"}}


def test_reused_crawler_emits_one_extraction_phase_per_fetch():
    class HTTP:
        def fetch(self, url):
            return url

    class Crawler:
        def __init__(self):
            self._http = HTTP()

        def crawl(self, url, **kwargs):
            observer = kwargs["fetch_observer"]
            started = time.perf_counter()
            observer("start", 0.0)
            self._http.fetch(url)
            observer("end", time.perf_counter() - started)
            return [SimpleNamespace(status="done", url=url)]

    messages = queue.Queue()
    crawler = Crawler()
    original_fetch = crawler._http.fetch
    for number in range(5):
        outcome = runner._crawl_item(
            _job(f"https://example.test/{number}"), messages, {"ml": crawler}
        )
        assert outcome.status == "done"

    phases = []
    while not messages.empty():
        phases.append(messages.get_nowait())
    assert [message[1] for message in phases].count("extraction") == 5
    assert crawler._http.fetch == original_fetch


@pytest.mark.parametrize("best_first", [True, False])
def test_ml_fetch_timing_uses_active_http_client(monkeypatch, best_first):
    delay = 0.05

    def sleeping_fetch(self, url):
        time.sleep(delay)
        return FetchResult(
            html="<html><body><p>Markets rallied after the policy announcement.</p></body></html>",
            text="Markets rallied after the policy announcement.",
            status=200,
            final_url=url,
        )

    monkeypatch.setattr(HTTPClient, "fetch", sleeping_fetch)
    monkeypatch.setattr("lazycrawler.ml.get_embedder", lambda *args: None)
    crawler = WebCrawler(
        crawler_cfg=CrawlerConfig(max_depth=0, respect_robots=False),
        http_cfg=HTTPConfig(allow_private_networks=True),
        ml_cfg=MLConfig(best_first=best_first),
    )
    messages = queue.Queue()

    outcome = runner._crawl_item(_job("https://example.test/article"), messages, {"ml": crawler})

    phases = []
    while not messages.empty():
        phases.append(messages.get_nowait()[1])
    assert outcome.status == "done"
    assert outcome.fetch_seconds >= delay * 0.8
    assert outcome.extraction_seconds == pytest.approx(
        outcome.total_seconds - outcome.fetch_seconds
    )
    assert phases == ["fetch", "fetch", "extraction"]


class _AlwaysFailingStartWorker(_FakeWorker):
    attempts = 0

    def article(self, job, deadline):
        type(self).attempts += 1
        raise RuntimeError("injected worker startup failure")


def test_repeated_worker_start_failure_still_writes_meta_and_session(monkeypatch, tmp_path, capsys):
    source = SimpleNamespace(
        name="source", mode="ml", url="feed", category="macro", region="US", lang="en"
    )
    _AlwaysFailingStartWorker.attempts = 0
    monkeypatch.setattr(runner, "REPORT_DIR", tmp_path)
    monkeypatch.setattr(runner, "resolve_cost_db_path", lambda path: tmp_path / "cost.db")
    monkeypatch.setattr(runner, "load_sources", lambda path: [source])
    monkeypatch.setattr(runner, "PersistentWorker", _AlwaysFailingStartWorker)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_news_crawl.py",
            "--sources-config",
            "unused.yaml",
            "--session-id",
            "failed-start-test",
            "--worker-start-attempts",
            "3",
            "--worker-restart-backoff-seconds",
            "0",
        ],
    )

    assert runner.main() == 0
    captured = capsys.readouterr()
    assert _AlwaysFailingStartWorker.attempts == 3
    assert "SESSION_ID=failed-start-test" in captured.out
    assert "ending partial run" in captured.err
    meta = (tmp_path / "failed-start-test_meta.json").read_text(encoding="utf-8")
    assert '"partial": true' in meta
    assert '"errors": 1' in meta
