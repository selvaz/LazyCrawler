# -*- coding: utf-8 -*-
"""Audit round 2: render_js/SSRF mutual exclusion, redirect-target robots,
canonical-URL SSRF validation, prompt-injection coverage, figure contract."""

from __future__ import annotations

import pytest

from lazycrawler import HTTPConfig
from lazycrawler.http import FetchResult, HTTPClient

# -- render_js vs SSRF guard are mutually exclusive -------------------------


def test_render_js_with_ssrf_guard_raises():
    with pytest.raises(ValueError):
        HTTPClient(HTTPConfig(render_js=True, block_private_addresses=True))


def test_render_js_alone_is_fine():
    HTTPClient(HTTPConfig(render_js=True, verify_ssl=False)).close()  # no guard -> ok
    HTTPClient(
        HTTPConfig(block_private_addresses=True, verify_ssl=False)
    ).close()  # guard alone -> ok


# -- robots re-checked on the final host after a redirect -------------------


def test_robots_blocked_on_redirect_target(monkeypatch, make_crawler):
    c = make_crawler(respect_robots=True)
    # robots allows everything except evil.example
    monkeypatch.setattr(c._robots, "allowed", lambda u: "evil.example" not in u)

    def fetch(self, url, extra_headers=None):
        body = "real article body long enough to clear the threshold. " * 4
        return FetchResult(
            html=f"<html><body><p>{body}</p></body></html>",
            text=body,
            status=200,
            final_url="https://evil.example/blocked",  # redirected here
        )

    monkeypatch.setattr("lazycrawler.http.HTTPClient.fetch", fetch)
    r = c.crawl("https://e.org/page", mode="pure")
    assert r[0].status == "robots_blocked"


# -- canonical URL cannot poison the cache with a private target ------------


def test_canonical_to_private_address_is_ignored(stub_fetch, monkeypatch, make_crawler):
    import lazycrawler.crawler as cm

    # only block the loopback target; seed host stays reachable (no real DNS)
    monkeypatch.setattr(cm, "is_blocked_address", lambda u: "127.0.0.1" in u)

    canon = '<link rel="canonical" href="http://127.0.0.1/admin">'
    stub_fetch(links_map={"https://e.org/page": canon})
    c = make_crawler(
        http_cfg=HTTPConfig(verify_ssl=False, link_delay=0, block_private_addresses=True)
    )
    r = c.crawl("https://e.org/page", mode="pure")
    # the page is NOT re-keyed under the private canonical
    assert "127.0.0.1" not in r[0].url
    assert r[0].url == "https://e.org/page"


# -- prompt-injection hardening covers every external-content prompt --------


def test_all_external_content_prompts_marked_untrusted():
    from lazycrawler import prompts as p

    for name in (
        "CONTENT_EXTRACTION_SYSTEM",
        "CUSTOM_EXTRACTION_SYSTEM",
        "LARGE_DOC_SUMMARY_SYSTEM",
        "ARTIFACT_TABLE_SYSTEM",
        "ARTIFACT_VISION_SYSTEM",
        "TOPIC_EXPANSION_SYSTEM",
    ):
        assert "UNTRUSTED" in getattr(p, name), name
    assert "UNTRUSTED" in p.build_link_selection_system("topic", 5)


# -- "figure" fully removed from the public artifact contract --------------


def test_figure_removed_from_artifact_type():
    import typing

    from lazycrawler.artifacts import ArtifactType

    assert "figure" not in typing.get_args(ArtifactType)
