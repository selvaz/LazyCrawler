"""Production callers supply defaults without overriding the deployment label."""

from __future__ import annotations

import importlib.util
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from lazycrawler import CrawlerDB, DBConfig


@pytest.fixture(params=[None, "job:deployment", ""])
def deployment_origin(monkeypatch, request):
    value = request.param
    monkeypatch.delenv("LAZYCRAWLER_SESSION_ORIGIN", raising=False)
    if value is not None:
        monkeypatch.setenv("LAZYCRAWLER_SESSION_ORIGIN", value)
    return value


@pytest.fixture
def fake_bridge(monkeypatch):
    bridge = SimpleNamespace(
        Agent=MagicMock(), ClaudeCodeEngine=MagicMock(), LLMEngine=MagicMock(), Tool=MagicMock()
    )
    monkeypatch.setitem(sys.modules, "lazybridge", bridge)
    monkeypatch.setitem(
        sys.modules,
        "lazybridge.session",
        SimpleNamespace(
            Session=lambda **kwargs: SimpleNamespace(session_id="cost", close=lambda: None)
        ),
    )
    return bridge


def _expected(value, script):
    return f"job:lazycrawler-{script}" if value is None else (value or "unknown")


def test_news_crawl_labels_both_content_modes(
    tmp_path, monkeypatch, deployment_origin, fake_bridge
):
    import run_news_crawl as runner

    path = tmp_path / "news.db"
    monkeypatch.setattr(runner, "REPORT_DIR", tmp_path / "reports")
    monkeypatch.setattr(runner, "resolve_cost_db_path", lambda *args: tmp_path / "costs.db")
    monkeypatch.setattr(runner, "register_cost_session", lambda *args: None)
    monkeypatch.setattr(
        runner,
        "load_sources",
        lambda path: [
            SimpleNamespace(
                name=mode, mode=mode, url="feed", category="macro", region="global", lang="en"
            )
            for mode in ("ml", "smart")
        ],
    )
    monkeypatch.setattr(
        runner, "_feed_items", lambda *args: ([("Title", "https://e.example/news")], 0)
    )
    modes = []

    def crawler_factory(*, db, **config):
        def crawl(url, *, mode, topic, session_id, origin):
            modes.append(mode)
            db.create_session(session_id, mode=mode, topic=topic, seed=url, origin=origin)
            return [SimpleNamespace(status="done", url=url)]

        return SimpleNamespace(crawl=crawl)

    monkeypatch.setattr(runner, "WebCrawler", crawler_factory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_news_crawl.py",
            "--db",
            str(path),
            "--sources-config",
            "unused.yaml",
            "--session-id",
            "news-test",
        ],
    )
    assert runner.main() == 0
    assert modes == ["ml", "smart"]
    with CrawlerDB(DBConfig(db_path=str(path), read_only=True)) as db:
        assert db.get_session("news-test")["origin"] == _expected(
            deployment_origin, "run_news_crawl"
        )


def test_calendar_search_labels_sessions(tmp_db, monkeypatch, deployment_origin, fake_bridge):
    # Load with a separate name so fake agents never replace a cached real
    # calendar module or leak into other offline tests.
    path = Path(__file__).resolve().parents[1] / "econ_calendar" / "agente_release.py"
    name = "econ_calendar._origin_probe"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    module._NEWS_DB = tmp_db

    @contextmanager
    def search_factory(*, db):
        def run(query, *, origin, **kwargs):
            db.create_session(
                "calendar-test", seed=query, source="search:duckduckgo", origin=origin
            )
            return {"results": []}

        yield SimpleNamespace(run=run)

    monkeypatch.setattr(module, "WebSearch", search_factory)
    monkeypatch.setattr(module, "domini_bloccati", lambda: [])
    module.search_web("economic release")
    assert tmp_db.get_session("calendar-test")["origin"] == _expected(
        deployment_origin, "agente_release"
    )


def test_delta_report_labels_agent_searches(tmp_db, monkeypatch, deployment_origin, fake_bridge):
    import make_digest_delta_report as runner

    fake_bridge.Agent.return_value = lambda prompt: SimpleNamespace(text=lambda: "delta report")
    monkeypatch.setattr("lazycrawler.CrawlerDB", lambda cfg: tmp_db)

    def tools_factory(*, db, origin):
        # The host-controlled constructor label is propagated by CrawlerTools;
        # exercise the production caller without running a real report agent.
        db.create_session("delta-test", source="search:duckduckgo", origin=origin)
        return SimpleNamespace(as_tools=lambda: [], close=lambda: None)

    monkeypatch.setattr("lazycrawler.tools.CrawlerTools", tools_factory)
    query = {"session_id": "news-test", "engine": "claude", "n_articles": 1, "text": "digest"}
    assert runner.build_delta_report([], query) == "delta report"
    assert tmp_db.get_session("delta-test")["origin"] == _expected(
        deployment_origin, "make_digest_delta_report"
    )
