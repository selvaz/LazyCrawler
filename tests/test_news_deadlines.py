from __future__ import annotations

import queue
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


def test_wall_budget_skips_remaining_items_but_exposes_session_for_digest(tmp_path, capsys):
    source = SimpleNamespace(
        name="source", mode="ml", url="feed", category="macro", region="US", lang="en"
    )
    assert (
        runner.main(
            [
                "--sources-config",
                "unused.yaml",
                "--session-id",
                "budget-test",
                "--wall-budget-minutes",
                "0.0001",
            ],
            worker_factory=_FakeWorker,
            sources_loader=lambda path: [source],
            report_dir=tmp_path,
            cost_db_resolver=lambda path: tmp_path / "cost.db",
        )
        == 4
    )
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


def test_repeated_worker_start_failure_still_writes_meta_and_session(tmp_path, capsys):
    source = SimpleNamespace(
        name="source", mode="ml", url="feed", category="macro", region="US", lang="en"
    )
    _AlwaysFailingStartWorker.attempts = 0
    assert (
        runner.main(
            [
                "--sources-config",
                "unused.yaml",
                "--session-id",
                "failed-start-test",
                "--worker-start-attempts",
                "3",
                "--worker-restart-backoff-seconds",
                "0",
            ],
            worker_factory=_AlwaysFailingStartWorker,
            sources_loader=lambda path: [source],
            report_dir=tmp_path,
            cost_db_resolver=lambda path: tmp_path / "cost.db",
        )
        == 4
    )
    captured = capsys.readouterr()
    assert _AlwaysFailingStartWorker.attempts == 3
    assert "SESSION_ID=failed-start-test" in captured.out
    assert "ending partial run" in captured.err
    meta = (tmp_path / "failed-start-test_meta.json").read_text(encoding="utf-8")
    assert '"partial": true' in meta
    assert '"errors": 1' in meta


class _TimeoutWorker(_FakeWorker):
    def article(self, job, deadline):
        return None, "db_write"


class _WritesThenTimeoutsWorker(_FakeWorker):
    def __init__(self, *args, **kwargs):
        self.calls = 0

    def feed(self, *args):
        return ([(str(i), f"https://e/{i}") for i in range(4)], 0)

    def article(self, job, deadline):
        self.calls += 1
        if self.calls <= 2:
            return (
                runner.ArticleOutcome("done", job.url, 0.01, 0.01, 0.02, new_page=True),
                "db_write",
            )
        return None, "db_write"


def test_main_drives_timeout_circuit_and_emits_one_summary_line(tmp_path, capsys):
    source = SimpleNamespace(
        name="source", mode="ml", url="feed", category="macro", region="US", lang="en"
    )
    exit_code = runner.main(
        [
            "--sources-config",
            "unused.yaml",
            "--session-id",
            "circuit-test",
            "--max-consecutive-timeouts",
            "2",
        ],
        worker_factory=_TimeoutWorker,
        sources_loader=lambda path: [source],
        report_dir=tmp_path,
        cost_db_resolver=lambda path: tmp_path / "cost.db",
    )

    captured = capsys.readouterr()
    summaries = [line for line in captured.out.splitlines() if line.startswith("_run_summary=")]
    assert exit_code == 4
    assert "TIMEOUT url=https://e/1 phase=db_write" in captured.err
    assert "TIMEOUT url=https://e/2 phase=db_write" in captured.err
    assert "Article circuit breaker opened" in captured.err
    assert len(summaries) == 1
    summary = __import__("json").loads(summaries[0].removeprefix("_run_summary="))
    assert summary["reason"] == "consecutive_timeouts"
    assert summary["timeouts"] == summary["attempted"] == 2


def test_open_circuit_fails_after_enough_pages_were_persisted(tmp_path, capsys):
    source = SimpleNamespace(
        name="source", mode="ml", url="feed", category="macro", region="US", lang="en"
    )
    exit_code = runner.main(
        [
            "--sources-config",
            "unused.yaml",
            "--session-id",
            "circuit-after-writes",
            "--max-consecutive-timeouts",
            "2",
            "--minimum-new-pages",
            "1",
            "--minimum-new-page-ratio",
            "0.1",
        ],
        worker_factory=_WritesThenTimeoutsWorker,
        sources_loader=lambda path: [source],
        report_dir=tmp_path,
        cost_db_resolver=lambda path: tmp_path / "cost.db",
    )

    captured = capsys.readouterr()
    summary_line = next(
        line for line in captured.out.splitlines() if line.startswith("_run_summary=")
    )
    summary = __import__("json").loads(summary_line.removeprefix("_run_summary="))
    assert exit_code == 4
    assert summary["new_pages"] == 2
    assert summary["new_page_ratio"] == 0.5
    assert summary["reason"] == "consecutive_timeouts"
    assert "Article circuit breaker opened" in captured.err


class _TimeoutThenCacheWorker(_FakeWorker):
    def __init__(self, *args, **kwargs):
        self.calls = 0

    def article(self, job, deadline):
        self.calls += 1
        if self.calls == 1:
            return None, "extraction"
        return (
            runner.ArticleOutcome("done", job.url, 0.01, 0.01, 0.02, cache_hit=True),
            "extraction",
        )


def test_main_low_write_exit_and_finished_dispositions_add_up(tmp_path, capsys):
    source = SimpleNamespace(
        name="source", mode="ml", url="feed", category="macro", region="US", lang="en"
    )
    exit_code = runner.main(
        ["--sources-config", "unused.yaml", "--session-id", "low-write-test"],
        worker_factory=_TimeoutThenCacheWorker,
        sources_loader=lambda path: [source],
        report_dir=tmp_path,
        cost_db_resolver=lambda path: tmp_path / "cost.db",
    )
    captured = capsys.readouterr()
    summaries = [line for line in captured.out.splitlines() if line.startswith("_run_summary=")]
    summary = __import__("json").loads(summaries[0].removeprefix("_run_summary="))

    assert exit_code == 4
    assert "TIMEOUT url=https://e/1 phase=extraction" in captured.err
    assert len(summaries) == 1
    done = summary["ml"] + summary["smart"]
    dispositions = summary["new_pages"] + summary["refetched_identical"] + summary["cache_hits"]
    assert dispositions == done == 1


def test_partial_write_safety_exit_code_contract():
    assert (
        runner._partial_run_exit_code(
            partial=True, new_pages=0, attempted=18, minimum_new_pages=1, minimum_ratio=0.1
        )
        == 4
    )
    assert (
        runner._partial_run_exit_code(
            partial=True, new_pages=1, attempted=20, minimum_new_pages=1, minimum_ratio=0.1
        )
        == 4
    )
    assert (
        runner._partial_run_exit_code(
            partial=True, new_pages=2, attempted=20, minimum_new_pages=1, minimum_ratio=0.1
        )
        == 0
    )
    assert (
        runner._partial_run_exit_code(
            partial=False, new_pages=0, attempted=20, minimum_new_pages=1, minimum_ratio=0.1
        )
        == 0
    )


def test_consecutive_timeout_circuit_resets_and_opens_at_limit():
    circuit = runner.ConsecutiveTimeoutCircuit(3)
    assert not circuit.record(True)
    assert not circuit.record(True)
    assert not circuit.record(False)
    assert not circuit.record(True)
    assert not circuit.record(True)
    assert circuit.record(True)


@pytest.mark.parametrize(
    ("from_cache", "disposition", "new_page", "refetched_identical", "cache_hit"),
    [
        (False, None, True, False, False),
        (True, "refetched_identical", False, True, False),
        (True, "cache_hit", False, False, True),
    ],
)
def test_article_outcome_separates_new_writes_from_identical_reuse(
    from_cache, disposition, new_page, refetched_identical, cache_hit
):
    class Crawler:
        def crawl(self, url, **kwargs):
            kwargs["fetch_observer"]("start", 0.0)
            kwargs["fetch_observer"]("end", 0.01)
            kwargs["phase_observer"]("db_write")
            return [
                SimpleNamespace(
                    status="done",
                    url=url,
                    from_cache=from_cache,
                    write_disposition=disposition,
                )
            ]

    messages = queue.Queue()
    outcome = runner._crawl_item(_job("https://example.test/page"), messages, {"ml": Crawler()})
    phases = []
    while not messages.empty():
        phases.append(messages.get_nowait()[1])
    assert "db_write" in phases
    assert outcome.new_page is new_page
    assert outcome.refetched_identical is refetched_identical
    assert outcome.cache_hit is cache_hit


def test_failed_page_write_is_not_counted_as_new(stub_fetch, make_crawler, tmp_db):
    class FailingStore:
        def __getattr__(self, name):
            return getattr(tmp_db, name)

        def upsert_page(self, page):
            raise OSError("injected page write failure")

    stub_fetch()
    # The production news crawler uses multiple workers.  That traversal logs
    # and swallows an individual worker exception, while returning results the
    # worker appended before the failed persistence call.
    crawler = make_crawler(db=FailingStore(), max_workers=2)
    messages = queue.Queue()

    outcome = runner._crawl_item(
        _job("https://example.test/write-failure"), messages, {"ml": crawler}
    )

    assert outcome.status == "write_failed", outcome.error
    assert outcome.write_failed is True
    assert outcome.new_page is False


def test_wrapper_runs_stub_python_and_propagates_write_safety_exit(tmp_path):
    import os
    import shutil
    import subprocess

    powershell = shutil.which("powershell.exe")
    if powershell is None:
        pytest.skip("powershell.exe is absent; wrapper execution is covered on Windows")

    log = tmp_path / "python-invocations.txt"
    stub = tmp_path / "stub-python.cmd"
    stub.write_text(
        '@echo off\r\necho %*>>"%STUB_LOG%"\r\necho SESSION_ID=wrapper-test\r\nexit /b 4\r\n',
        encoding="utf-8",
    )
    env = {**os.environ, "STUB_LOG": str(log)}
    completed = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(runner.ROOT / "run_news_crawl_with_telegram.ps1"),
            "-SourcesConfig",
            "unused.yaml",
            "-DigestEngines",
            "stub",
            "-Cycle",
            "stub",
            "-Python",
            str(stub),
        ],
        cwd=runner.ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    invocations = log.read_text(encoding="utf-8")
    assert completed.returncode == 4, completed.stdout + completed.stderr
    assert "write-safety check (exit 4)" in completed.stdout + completed.stderr
    assert "send_telegram_news_report.py" in invocations
    assert "--failure-message" in invocations
    assert "make_news_report.py" not in invocations
