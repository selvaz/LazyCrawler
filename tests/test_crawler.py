# -*- coding: utf-8 -*-
"""WebCrawler: 3-level dedup, TTL, parallel safety, session ids, cache recursion."""

from __future__ import annotations

from lazycrawler import CrawlerConfig, HTTPConfig, WebCrawler
from lazycrawler.http import url_hash

U = "https://site.example/article"


# -- DEDUP level 1: URL + TTL + force_refresh --------------------------------


def test_url_cache_hit_skips_fetch(stub_fetch, tmp_db, make_crawler):
    state = stub_fetch()
    c = make_crawler(db=tmp_db)
    r1 = c.crawl(U, mode="pure", session_id="s1")
    assert state["n"] == 1 and r1[0].status == "done" and r1[0].from_cache is False
    r2 = c.crawl(U, mode="pure", session_id="s1")
    assert state["n"] == 1 and r2[0].from_cache is True


def test_ttl_zero_refetches(stub_fetch, db_factory, make_crawler):
    state = stub_fetch()
    db = db_factory(ttl_hours=0.0)
    make_crawler(db=db).crawl(U, mode="pure")
    make_crawler(db=db).crawl(U, mode="pure")
    assert state["n"] == 2


def test_force_refresh_refetches(stub_fetch, db_factory, make_crawler):
    state = stub_fetch()
    db = db_factory()
    make_crawler(db=db).crawl(U, mode="pure")
    n_after = state["n"]
    db2 = db_factory(force_refresh=True)
    make_crawler(db=db2).crawl(U, mode="pure")
    assert state["n"] == n_after + 1


# -- DEDUP level 2: content hash ---------------------------------------------


def test_content_hash_dedup_across_urls(stub_fetch, tmp_db, make_crawler):
    state = stub_fetch()  # identical body for every URL
    c = make_crawler(db=tmp_db)
    c.crawl("https://a.example/x", mode="pure", session_id="s1")
    rb = c.crawl("https://b.example/y", mode="pure", session_id="s1")
    assert rb[0].from_cache is True  # reused content
    assert state["n"] == 2  # but both were fetched (level-2 is post-fetch)
    rows = tmp_db.get_pages(status="done")
    assert len(rows) == 2  # per-URL provenance preserved
    ha = tmp_db.get_page(url_hash("https://a.example/x"))["content_hash"]
    hb = tmp_db.get_page(url_hash("https://b.example/y"))["content_hash"]
    assert ha and ha == hb


# -- no_text -----------------------------------------------------------------


def test_no_text_status(stub_fetch, make_crawler):
    stub_fetch(content_map={"https://e.org/empty": ""})
    c = make_crawler()
    r = c.crawl("https://e.org/empty", mode="pure")
    assert r[0].status == "no_text"


# -- pure output format ------------------------------------------------------


def test_pure_result_shape(stub_fetch, make_crawler):
    stub_fetch()
    r = make_crawler().crawl(U, mode="pure")[0]
    assert r.text and r.summary is None and r.sentiment is None and r.notes is None
    assert r.entities == [] and r.topics == []


# -- markdown output (emit_markdown) -----------------------------------------


def test_markdown_off_by_default(stub_fetch, make_crawler):
    stub_fetch()
    r = make_crawler().crawl(U, mode="pure")[0]
    assert r.markdown is None


def test_markdown_emitted_when_enabled(stub_fetch, make_crawler):
    # markdownify may be absent -> html_to_markdown degrades to a basic strip,
    # which still yields the body text, so this holds either way.
    state = stub_fetch(content_map={U: "Renewable energy adoption accelerated in 2026."})
    r = make_crawler(emit_markdown=True).crawl(U, mode="pure")[0]
    assert r.markdown and "Renewable energy adoption" in r.markdown
    assert state["n"] == 1


def test_markdown_persisted_and_restored_from_cache(stub_fetch, tmp_db, make_crawler):
    stub_fetch(content_map={U: "Solid-state batteries reached pilot production."})
    c = make_crawler(db=tmp_db, emit_markdown=True)
    c.crawl(U, mode="pure", session_id="m1")
    row = tmp_db.get_page(url_hash(U))
    assert row.get("markdown") and "Solid-state batteries" in row["markdown"]
    # cache hit repopulates PageResult.markdown from the stored row
    r2 = c.crawl(U, mode="pure", session_id="m1")[0]
    assert r2.from_cache is True and r2.markdown and "Solid-state batteries" in r2.markdown


# -- context-manager cleanup -------------------------------------------------


def test_webcrawler_context_manager_closes(stub_fetch, monkeypatch):
    import lazycrawler.http as http_mod

    released = {"n": 0}
    orig = http_mod.HTTPClient.release

    def counting_release(self):
        released["n"] += 1
        return orig(self)

    monkeypatch.setattr(http_mod.HTTPClient, "release", counting_release)
    stub_fetch()
    with WebCrawler(
        CrawlerConfig(max_depth=0, respect_robots=False),
        HTTPConfig(verify_ssl=False, allow_private_networks=True),
    ) as c:
        c.crawl(U, mode="pure")
        assert c._http._session is not None  # session alive while in use
    assert released["n"] >= 1  # __exit__ -> close() -> release() ran
    assert c._http._session is None  # sockets freed on exit


# -- max_depth runtime override (no shared-config mutation) ------------------


def test_max_depth_override_does_not_mutate_cfg(stub_fetch, make_crawler):
    stub_fetch()
    c = make_crawler(max_depth=2)
    assert c.cfg.max_depth == 2
    c.crawl(U, mode="pure", max_depth=0)
    assert c.cfg.max_depth == 2  # unchanged


# -- session id uniqueness (B8) ----------------------------------------------


def test_default_session_ids_unique():
    ids = {WebCrawler._default_session_id("topic", "pure") for _ in range(200)}
    assert len(ids) == 200


# -- parallel dedup safety ---------------------------------------------------


def test_parallel_dedup_and_fk(stub_fetch, tmp_db):
    n = 10
    links = "".join(f'<a href="https://site.example/p{i}">P{i}</a>' for i in range(n))
    seed = "https://site.example/seed"
    stub_fetch(links_map={seed: links})
    c = WebCrawler(
        CrawlerConfig(
            max_depth=1, max_pages=50, max_links_per_level=20, max_workers=6, respect_robots=False
        ),
        HTTPConfig(verify_ssl=False, link_delay=0, allow_private_networks=True),
        db=tmp_db,
    )
    res = c.crawl(seed, mode="pure", session_id="par")
    c.close()
    done = [r for r in res if r.status == "done"]
    assert len({r.url for r in done}) == n + 1
    assert tmp_db.stats()["pages"] == n + 1
    assert tmp_db.stats()["edges"] == n + 1


# -- recurse_from_cache ------------------------------------------------------


def test_recurse_from_cache(stub_fetch, tmp_db):
    seed = "https://site.example/seed"
    child = "https://site.example/child"
    links = f'<a href="{child}">child</a>'
    state = stub_fetch(links_map={seed: links})

    # cold run: depth 1, follows the link -> seed + child fetched and stored
    cold = WebCrawler(
        CrawlerConfig(max_depth=1, max_pages=20, respect_robots=False, recurse_from_cache=True),
        HTTPConfig(verify_ssl=False, link_delay=0, allow_private_networks=True),
        db=tmp_db,
    )
    r_cold = cold.crawl(seed, mode="pure", session_id="cold")
    cold.close()
    cold_urls = {r.url for r in r_cold if r.status == "done"}
    n_cold = state["n"]
    assert url_hash(seed) and len(cold_urls) == 2

    # warm run: seed is a cache hit; with recurse_from_cache the child is still
    # reached (from stored links), and the child is itself cached -> no new fetch.
    warm = WebCrawler(
        CrawlerConfig(max_depth=1, max_pages=20, respect_robots=False, recurse_from_cache=True),
        HTTPConfig(verify_ssl=False, link_delay=0, allow_private_networks=True),
        db=tmp_db,
    )
    r_warm = warm.crawl(seed, mode="pure", session_id="warm")
    warm.close()
    warm_urls = {r.url for r in r_warm if r.status == "done"}
    assert warm_urls == cold_urls  # same frontier warm vs cold
    assert state["n"] == n_cold  # nothing re-fetched


def test_cache_hit_terminal_without_recurse(stub_fetch, tmp_db):
    seed = "https://site.example/seed2"
    child = "https://site.example/child2"
    links = f'<a href="{child}">child</a>'
    stub_fetch(links_map={seed: links})
    # cold run populates cache (default recurse_from_cache=False)
    c1 = WebCrawler(
        CrawlerConfig(max_depth=1, respect_robots=False),
        HTTPConfig(verify_ssl=False, link_delay=0, allow_private_networks=True),
        db=tmp_db,
    )
    c1.crawl(seed, mode="pure", session_id="a")
    c1.close()
    c2 = WebCrawler(
        CrawlerConfig(max_depth=1, respect_robots=False),
        HTTPConfig(verify_ssl=False, link_delay=0, allow_private_networks=True),
        db=tmp_db,
    )
    r = c2.crawl(seed, mode="pure", session_id="b")
    c2.close()
    # seed served from cache -> terminal -> only the seed comes back this run
    assert {p.url for p in r} == {seed}


# -- PDF single download -----------------------------------------------------


def test_pdf_downloaded_once(stub_fetch, make_crawler, monkeypatch):
    import lazycrawler.crawler as crawler_mod

    extract_calls = {"bytes": 0, "url": 0}

    def fake_bytes(data):
        extract_calls["bytes"] += 1
        return ("Extracted PDF text content that is sufficiently long here.", "PDF Title", None)

    def fake_url(*a, **k):
        extract_calls["url"] += 1
        return ("should-not-be-called", "", None)

    monkeypatch.setattr(crawler_mod, "extract_pdf_bytes", fake_bytes)
    monkeypatch.setattr(crawler_mod, "extract_pdf", fake_url)

    stub_fetch(pdf_map={"https://e.org/doc.pdf": b"%PDF-1.7 fake bytes"})
    r = make_crawler().crawl("https://e.org/doc.pdf", mode="pure")[0]
    assert r.status == "done" and r.is_pdf is True
    assert "Extracted PDF text" in (r.text or "")
    assert extract_calls["bytes"] == 1  # extracted from already-fetched bytes
    assert extract_calls["url"] == 0  # no second download via extract_pdf(url)


# -- audit fixes: same_host_only + hard max_pages cap in parallel ----------


def test_same_host_only_blocks_parent_domain(stub_fetch, make_crawler):
    links = (
        '<a href="https://example.com/parent">parent site</a>'
        '<a href="https://news.example.com/sibling">sibling page</a>'
    )
    stub_fetch(links_map={"https://news.example.com/seed": links})
    c = make_crawler(max_depth=1, same_domain_only=True, same_host_only=True)
    res = c.crawl("https://news.example.com/seed", mode="pure")
    urls = {r.url for r in res}
    assert not any(u.startswith("https://example.com/") for u in urls)  # parent blocked


def test_max_pages_hard_cap_parallel(stub_fetch, make_crawler):
    fanout = "".join(f'<a href="https://e.org/p{i}">p{i}</a>' for i in range(20))
    stub_fetch(links_map={"https://e.org/seed": fanout})
    c = make_crawler(max_depth=1, max_pages=3, max_workers=4)
    res = c.crawl("https://e.org/seed", mode="pure")
    done = [r for r in res if r.status == "done"]
    assert len(done) <= 3  # atomic slot reservation -> hard cap even in parallel
