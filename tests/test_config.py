# -*- coding: utf-8 -*-
"""resolve_news_db_path: the single resolution chain CrawlerTools/WebTools route through."""

from __future__ import annotations

from lazycrawler.config import resolve_news_db_path


def test_explicit_wins_over_env(monkeypatch, tmp_path):
    monkeypatch.setenv("LAZYCRAWLER_NEWS_DB", str(tmp_path / "env.db"))
    explicit = str(tmp_path / "explicit.db")
    assert resolve_news_db_path(explicit) == explicit


def test_env_var_used_when_no_explicit(monkeypatch, tmp_path):
    env_path = str(tmp_path / "env.db")
    monkeypatch.setenv("LAZYCRAWLER_NEWS_DB", env_path)
    assert resolve_news_db_path() == env_path


def test_none_when_nothing_configured(monkeypatch):
    monkeypatch.delenv("LAZYCRAWLER_NEWS_DB", raising=False)
    assert resolve_news_db_path() is None
