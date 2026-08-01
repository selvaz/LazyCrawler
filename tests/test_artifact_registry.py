# -*- coding: utf-8 -*-
"""Tests for artifact_registry.py and its wiring into make_news_report.py.

Distinct from tests/test_artifacts.py, which covers the unrelated
lazycrawler.artifacts.Artifact extraction concept -- this file only covers
the best-effort lazytools.registry cataloging plumbing.

lazytools.registry itself (register_artifact/search_artifacts) is exercised
for real against a temporary sqlite file (CRAWLER_ARTIFACTS_DB) -- no mocks
there, since it's cheap stdlib-sqlite3. Only the report-writing call sites
in make_news_report.py are monkeypatched, to check the exact
kind/title/tags/summary they produce without needing a full crawl session,
a real news.db, or a live DeepSeek digest call.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lazytools.registry import search_artifacts  # noqa: E402

import artifact_registry  # noqa: E402
import make_news_report as mnr  # noqa: E402


@pytest.fixture
def artifacts_db(tmp_path, monkeypatch):
    """A real temporary sqlite file wired up as CRAWLER_ARTIFACTS_DB."""
    db_path = tmp_path / "artifacts.sqlite"
    monkeypatch.setenv("CRAWLER_ARTIFACTS_DB", str(db_path))
    return str(db_path)


# --------------------------------------------------------------------------
# register_report_artifact: real sqlite round-trip
# --------------------------------------------------------------------------


def test_register_report_artifact_report_kind_is_retrievable(artifacts_db):
    artifact_id = artifact_registry.register_report_artifact(
        kind="report",
        title="News full report news_20260801_070000 (us)",
        summary="12 articles for region us; top sources: Reuters, AP",
        tags=["daily", "region:us"],
        content_uri=str(Path("reports/news/news_full_x_us.md")),
    )
    assert artifact_id is not None

    results = search_artifacts(artifacts_db, kind="report")
    assert len(results) == 1
    row = results[0]
    assert row["kind"] == "report"
    assert row["title"] == "News full report news_20260801_070000 (us)"
    assert set(row["tags"]) == {"daily", "region:us"}


def test_register_report_artifact_digest_kind_is_retrievable(artifacts_db):
    artifact_id = artifact_registry.register_report_artifact(
        kind="digest",
        title="News digest news_20260801_070000",
        summary="Executive digest of 40 articles grouped by theme: Monetary Policy, Growth",
        tags=["daily", "digest"],
        content_uri=str(Path("reports/news/news_digest_x.md")),
    )
    assert artifact_id is not None

    results = search_artifacts(artifacts_db, kind="digest")
    assert len(results) == 1
    row = results[0]
    assert row["kind"] == "digest"
    assert row["title"] == "News digest news_20260801_070000"
    assert set(row["tags"]) == {"daily", "digest"}


def test_summary_keywords_are_searchable(artifacts_db):
    artifact_registry.register_report_artifact(
        kind="digest",
        title="News digest news_20260801_070000",
        summary="Themes: Monetary Policy, Geopolitical Risk",
        tags=["daily", "digest"],
        content_uri="reports/news/x.md",
    )
    results = search_artifacts(artifacts_db, query="Geopolitical")
    assert len(results) == 1


# --------------------------------------------------------------------------
# Failure modes: never raise, never write when not configured
# --------------------------------------------------------------------------


def test_unset_env_var_is_a_silent_noop(monkeypatch):
    monkeypatch.delenv("CRAWLER_ARTIFACTS_DB", raising=False)
    result = artifact_registry.register_report_artifact(
        kind="report",
        title="t",
        summary="s",
        tags=["daily"],
        content_uri="uri",
    )
    assert result is None  # resolve_db("crawler_artifacts") -> None -> skip, no file ever opened


def test_import_guard_short_circuits(monkeypatch, artifacts_db):
    # Simulates lazytools not being importable at all: both names go None.
    monkeypatch.setattr(artifact_registry, "resolve_db", None)
    monkeypatch.setattr(artifact_registry, "register_artifact", None)
    result = artifact_registry.register_report_artifact(
        kind="report",
        title="t",
        summary="s",
        tags=["daily"],
        content_uri="uri",
    )
    assert result is None


def test_register_artifact_failure_does_not_propagate(monkeypatch, artifacts_db):
    def _boom(*args, **kwargs):
        raise RuntimeError("simulated sqlite failure")

    monkeypatch.setattr(artifact_registry, "register_artifact", _boom)
    result = artifact_registry.register_report_artifact(
        kind="report",
        title="t",
        summary="s",
        tags=["daily"],
        content_uri="uri",
    )
    assert result is None  # swallowed, not raised


# --------------------------------------------------------------------------
# make_news_report.py: summary/theme helpers
# --------------------------------------------------------------------------


def test_region_summary_includes_region_count_and_top_sources_and_topics():
    pages = [
        {"source_name": "Reuters", "topics": ["rates", "inflation"]},
        {"source_name": "Reuters", "topics": ["rates"]},
        {"source_name": "AP", "topics": ["equities"]},
        {"source_name": "Bloomberg", "topics": []},
    ]
    summary = mnr._region_summary("us", pages)
    assert "4 articles for region us" in summary
    assert "Reuters" in summary
    assert "rates" in summary


def test_digest_themes_extracts_markdown_headers():
    text = "## Monetary Policy\ntext here\n## Geopolitical Risk\nmore text\n"
    assert mnr._digest_themes(text) == ["Monetary Policy", "Geopolitical Risk"]


def test_digest_themes_empty_when_no_headers():
    assert mnr._digest_themes("just plain text, no markdown headers at all") == []


def test_digest_summary_carries_theme_names():
    text = "## Monetary Policy\nfoo\n## Growth And Inflation\nbar\n"
    summary = mnr._digest_summary(text, n_articles=25)
    assert "Monetary Policy" in summary
    assert "Growth And Inflation" in summary
    assert "25" in summary


def test_digest_summary_falls_back_to_preview_without_headers():
    text = "No headers here, just a lead paragraph about markets.\n\nMore text."
    summary = mnr._digest_summary(text, n_articles=5)
    assert "lead paragraph about markets" in summary


# --------------------------------------------------------------------------
# make_news_report.py: the actual report-writing call sites produce the
# right kind/title/tags/summary (register_report_artifact itself is
# monkeypatched here -- no sqlite/DB/DeepSeek involved).
# --------------------------------------------------------------------------


def test_register_region_artifact_call_args(monkeypatch, tmp_path):
    captured = {}

    def _capture(**kwargs):
        captured.update(kwargs)
        return "fake-id"

    monkeypatch.setattr(mnr, "register_report_artifact", _capture)
    region_pages = [
        {"source_name": "Reuters", "topics": ["rates"]},
        {"source_name": "AP", "topics": ["equities"]},
    ]
    region_path = tmp_path / "news_full_news_20260801_070000_us.md"

    mnr._register_region_artifact("news_20260801_070000", "us", region_pages, region_path)

    assert captured["kind"] == "report"
    assert captured["title"] == "News full report news_20260801_070000 (us)"
    assert captured["tags"] == ["daily", "region:us"]
    assert "2 articles" in captured["summary"]
    assert captured["content_uri"] == str(region_path)


def test_register_digest_artifact_call_args(monkeypatch, tmp_path):
    captured = {}

    def _capture(**kwargs):
        captured.update(kwargs)
        return "fake-id"

    monkeypatch.setattr(mnr, "register_report_artifact", _capture)
    digest_text = "## Monetary Policy\nfoo\n"
    digest_path = tmp_path / "news_digest_news_20260801_070000.md"

    mnr._register_digest_artifact("news_20260801_070000", digest_text, digest_path, 10)

    assert captured["kind"] == "digest"
    assert captured["title"] == "News digest news_20260801_070000"
    assert captured["tags"] == ["daily", "digest"]
    assert "Monetary Policy" in captured["summary"]
    assert captured["content_uri"] == str(digest_path)
