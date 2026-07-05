# -*- coding: utf-8 -*-
"""JS-rendering fetch path (render_js) — without launching a real browser."""

from __future__ import annotations

from lazycrawler.config import HTTPConfig
from lazycrawler.http import HTTPClient

_RENDERED = "<html><body><p>" + ("rendered content " * 8) + "</p></body></html>"


class _StubRenderer:
    """Stand-in for BrowserRenderer that returns canned HTML (or None)."""

    def __init__(self, html):
        self._html = html
        self.calls = 0

    def render(self, url):
        self.calls += 1
        return self._html


def test_render_js_uses_browser_html(monkeypatch):
    client = HTTPClient(HTTPConfig(render_js=True, min_text_chars=10, verify_ssl=False))
    stub = _StubRenderer(_RENDERED)
    monkeypatch.setattr(client, "_browser_renderer", lambda: stub)

    def boom(url, **kw):
        raise AssertionError("requests must not be used when the browser renders")

    monkeypatch.setattr(client._session, "get", boom)
    fr = client.fetch("https://spa.example/app")
    assert stub.calls == 1
    assert fr.status == 200 and fr.text and "rendered content" in fr.text


def test_render_js_falls_back_to_requests(monkeypatch):
    client = HTTPClient(HTTPConfig(render_js=True, min_text_chars=10, verify_ssl=False))
    stub = _StubRenderer(None)  # browser unavailable / failed
    monkeypatch.setattr(client, "_browser_renderer", lambda: stub)

    class _Resp:
        status_code = 200
        headers = {"Content-Type": "text/html"}
        content = b"<html><body><p>" + b"plain requests body " * 4 + b"</p></body></html>"
        text = "<html><body><p>" + "plain requests body " * 4 + "</p></body></html>"
        is_redirect = False
        url = "https://spa.example/app"

        def iter_content(self, chunk_size=0):
            yield self.content

        def close(self):
            pass

        def raise_for_status(self):
            pass

    monkeypatch.setattr(client._session, "get", lambda url, **kw: _Resp())
    fr = client.fetch("https://spa.example/app")
    assert stub.calls == 1
    assert fr.status == 200 and fr.text and "plain requests body" in fr.text


def test_browser_refuses_non_web_schemes(monkeypatch):
    # Regression: render_js disables the SSRF guard, so the browser must not load
    # file://, chrome://, etc. (local-file disclosure). The scheme guard rejects
    # them before any Playwright work.
    from lazycrawler.browser import BrowserRenderer, _is_web_url

    assert _is_web_url("https://ok.example/x") is True
    assert _is_web_url("http://ok.example/x") is True
    assert _is_web_url("file:///etc/passwd") is False
    assert _is_web_url("chrome://version") is False
    assert _is_web_url("view-source:https://x") is False

    r = BrowserRenderer()

    def boom(url):
        raise AssertionError("must not reach Playwright for a non-web scheme")

    monkeypatch.setattr(r, "_render_sync", boom)
    try:
        # Non-web schemes are rejected before _render_sync (which would raise).
        assert r.render("file:///etc/passwd") is None
        assert r.render("chrome://version") is None
    finally:
        r.close()
