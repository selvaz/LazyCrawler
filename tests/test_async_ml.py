# -*- coding: utf-8 -*-
"""Async crawler ML parity: content="ml" / links="ml" (best-first), parallel
execution, artifacts and DB persistence — all sharing the synchronous pipeline.

Like ``test_ml.py`` these run with Model2Vec absent/unavailable (offline CI),
so semantic scoring degrades to lexical+structural — still topic-aware. The
async HTTP client and SSRF guard are stubbed so no network is touched.
"""

from __future__ import annotations

import asyncio

import pytest

from lazycrawler import CrawlerConfig, CrawlerDB, DBConfig, HTTPConfig, MLConfig

aiohttp = pytest.importorskip("aiohttp")  # the async extra

import lazycrawler.async_crawler as ac  # noqa: E402
from lazycrawler.async_crawler import AsyncWebCrawler, _AsyncFetchResult  # noqa: E402

SEED = "https://e.org/seed"
_BODY = "Independent generic article body, long enough to be treated as real text. " * 8

# A seed page linking a strongly on-topic child and an off-topic one.
_LINKS = (
    '<a href="https://e.org/lithium-battery-breakthrough">lithium battery breakthrough research</a>'
    '<a href="https://e.org/sports-news">sports news today</a>'
    '<a href="https://e.org/about">about</a>'
)
_TABLE = "<table><tr><th>City</th><th>Pop</th></tr><tr><td>Paris</td><td>2M</td></tr></table>"


def _install(monkeypatch, *, links_map=None, body_map=None, extra_html_map=None):
    """Stub the async fetch + SSRF guard. Returns a call-count dict."""
    links_map = links_map or {}
    body_map = body_map or {}
    extra_html_map = extra_html_map or {}
    state = {"n": 0, "by_url": {}}

    async def no_block(url: str) -> bool:
        return False

    monkeypatch.setattr(ac, "_is_blocked_async", no_block)

    async def fake_fetch(self, url):
        state["n"] += 1
        state["by_url"][url] = state["by_url"].get(url, 0) + 1
        body = body_map.get(url, _BODY)
        extra = links_map.get(url, "") + extra_html_map.get(url, "")
        html = (
            f"<html><head><title>{url[-12:]}</title></head><body><p>{body}</p>{extra}</body></html>"
        )
        return _AsyncFetchResult(html=html, text=body, status=200, final_url=url)

    monkeypatch.setattr(ac._AsyncHTTPClient, "fetch", fake_fetch)
    return state


def _run(crawler: AsyncWebCrawler, *args, **kwargs):
    async def go():
        try:
            return await crawler.crawl(*args, **kwargs)
        finally:
            await crawler.close()

    return asyncio.run(go())


def _crawler(**cfg):
    base = dict(respect_robots=False)
    base.update(cfg)
    return AsyncWebCrawler(
        CrawlerConfig(**base),
        HTTPConfig(verify_ssl=False, block_private_addresses=False),
        ml_cfg=MLConfig(),
    )


# -- content="ml" ----------------------------------------------------------


def test_async_content_ml_produces_ml_pages(monkeypatch):
    _install(monkeypatch)
    results = _run(_crawler(max_depth=0), SEED, content="ml")
    assert results and results[0].mode == "ml"
    assert results[0].text  # local clean text filled (no LLM, no tokens)
    assert results[0].topics  # YAKE / frequency keyphrases populated


def test_async_pure_mode_unchanged(monkeypatch):
    _install(monkeypatch)
    results = _run(_crawler(max_depth=0), SEED)  # default mode="pure"
    assert results and results[0].mode == "pure"
    assert results[0].text


# -- links="ml" best-first --------------------------------------------------


def test_async_links_ml_best_first(monkeypatch):
    _install(monkeypatch, links_map={SEED: _LINKS})
    results = _run(
        _crawler(max_depth=1, max_pages=2),
        SEED,
        links="ml",
        topic="lithium battery technology",
    )
    urls = {r.url for r in results if r.status == "done"}
    assert any("lithium-battery" in u for u in urls)  # best-scoring child crawled
    assert not any("sports-news" in u for u in urls)  # low-score child skipped under the cap


def test_async_links_ml_parallel_respects_cap(monkeypatch):
    _install(monkeypatch, links_map={SEED: _LINKS})
    results = _run(
        _crawler(max_depth=1, max_pages=5, max_workers=4),
        SEED,
        links="ml",
        topic="lithium battery technology",
    )
    done = [r for r in results if r.status == "done"]
    assert len(done) <= 5
    assert any("lithium-battery" in r.url for r in done)


def test_async_bfs_parallel_pure(monkeypatch):
    _install(monkeypatch, links_map={SEED: _LINKS})
    results = _run(_crawler(max_depth=1, max_pages=10, max_workers=4), SEED, mode="pure")
    urls = {r.url for r in results if r.status == "done"}
    assert SEED in urls
    assert any("lithium-battery" in u for u in urls)


# -- artifacts parity -------------------------------------------------------


def test_async_artifacts_extracted(monkeypatch):
    _install(monkeypatch, extra_html_map={SEED: _TABLE})
    crawler = _crawler(max_depth=0, extract_artifacts=True, artifact_types=("table",))
    results = _run(crawler, SEED, content="ml")
    assert results
    arts = results[0].artifacts
    assert any(a.artifact_type == "table" for a in arts)


# -- persistence / reporting parity -----------------------------------------


def test_async_persists_session_and_pages(monkeypatch, tmp_path):
    _install(monkeypatch, links_map={SEED: _LINKS})
    db = CrawlerDB(DBConfig(db_path=str(tmp_path / "async_ml.db")))
    try:
        crawler = AsyncWebCrawler(
            CrawlerConfig(max_depth=1, max_pages=5, respect_robots=False),
            HTTPConfig(verify_ssl=False, block_private_addresses=False),
            db=db,
            ml_cfg=MLConfig(),
        )
        results = asyncio.run(
            crawler.crawl_many([SEED], links="ml", content="ml", topic="lithium battery")
        )
        asyncio.run(crawler.close())
        assert results
        # the seed page was persisted with ml mode
        from lazycrawler.http import url_hash

        row = db.get_page(url_hash(SEED))
        assert row is not None
        assert row.get("mode") == "ml"
    finally:
        db.close()


# -- input validation -------------------------------------------------------


def test_async_rejects_smart_mode(monkeypatch):
    _install(monkeypatch)
    crawler = _crawler(max_depth=0)
    with pytest.raises(ValueError, match="smart"):
        asyncio.run(crawler.crawl(SEED, content="smart"))
    asyncio.run(crawler.close())
