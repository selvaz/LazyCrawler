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

        def raise_for_status(self):
            pass

    monkeypatch.setattr(client._session, "get", lambda url, **kw: _Resp())
    fr = client.fetch("https://spa.example/app")
    assert stub.calls == 1
    assert fr.status == 200 and fr.text and "plain requests body" in fr.text
