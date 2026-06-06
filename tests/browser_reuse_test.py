# -*- coding: utf-8 -*-
"""
Deterministic test for HTTPClient browser renderer reuse.
No Playwright and no network: BrowserRenderer is replaced with a fake.
Run: python tests/browser_reuse_test.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lazycrawler.config import HTTPConfig
from lazycrawler.http import HTTPClient
from lazycrawler import browser as browser_mod

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    PASS, FAIL = PASS + bool(cond), FAIL + (not cond)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")


BODY = "Browser-rendered content, long enough for text extraction. " * 8


class FakeRenderer:
    created = 0
    renders = []
    closed = 0

    def __init__(self, **kwargs):
        type(self).created += 1
        self.kwargs = kwargs

    def render(self, url):
        type(self).renders.append(url)
        return f"<html><head><title>{url}</title></head><body><p>{BODY}</p></body></html>"

    def close(self):
        type(self).closed += 1


browser_mod.BrowserRenderer = FakeRenderer

client = HTTPClient(HTTPConfig(render_js=True, user_agent="LazyCrawlerTest/1.0"))
try:
    html1, text1, status1 = client.fetch("https://site.example/a")
    html2, text2, status2 = client.fetch("https://site.example/b")
    check("single renderer instance reused", FakeRenderer.created == 1)
    check("both URLs rendered", FakeRenderer.renders == [
        "https://site.example/a", "https://site.example/b",
    ])
    check("rendered responses returned", html1 and html2 and status1 == 200 and status2 == 200)
    check("text extracted from rendered HTML", text1 and text2 and "Browser-rendered" in text1)
finally:
    client.close()

check("renderer closed with HTTPClient", FakeRenderer.closed == 1)

print(f"\n=== RESULT: {PASS} PASS, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
