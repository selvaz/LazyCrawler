# -*- coding: utf-8 -*-
"""CrawlerLLM link selection — envelope handling and fallback semantics.

These bypass CrawlerLLM.__init__ (which requires LazyBridge) via __new__ and
drive select_links with a fake selector agent, so they run with no LLM backend
installed. The behavior under test is pure Python: which envelope outcomes fall
back to first-N vs. return a genuine empty selection.
"""

from __future__ import annotations

import types

from lazycrawler.config import LLMConfig
from lazycrawler.llm import CrawlerLLM, LinkSelection

_CANDS = [
    ("alpha", "https://e.org/1"),
    ("beta", "https://e.org/2"),
    ("gamma", "https://e.org/3"),
]


def _llm():
    obj = CrawlerLLM.__new__(CrawlerLLM)
    obj.cfg = LLMConfig()
    return obj


def _env(ok, payload):
    return types.SimpleNamespace(ok=ok, payload=payload)


def test_select_links_valid_selection():
    llm = _llm()
    out = llm.select_links(lambda u: _env(True, LinkSelection(indices=[3, 1])), "x", _CANDS, 5)
    assert out == [_CANDS[2], _CANDS[0]]


def test_select_links_empty_selection_is_honored():
    # A valid, deliberate "no links" must NOT trigger the fallback.
    llm = _llm()
    out = llm.select_links(lambda u: _env(True, LinkSelection(indices=[])), "x", _CANDS, 5)
    assert out == []


def test_select_links_falls_back_when_envelope_not_ok():
    # Regression: env.ok False silently emptied the frontier. It must fall back
    # to the first max_links candidates, like the exception path does.
    llm = _llm()
    out = llm.select_links(lambda u: _env(False, None), "x", _CANDS, 2)
    assert out == _CANDS[:2]


def test_select_links_falls_back_on_wrong_payload_type():
    llm = _llm()
    out = llm.select_links(lambda u: _env(True, {"indices": [1]}), "x", _CANDS, 2)
    assert out == _CANDS[:2]


def test_select_links_falls_back_on_exception():
    def boom(_user):
        raise RuntimeError("model exploded")

    llm = _llm()
    assert llm.select_links(boom, "x", _CANDS, 2) == _CANDS[:2]


def test_select_links_falls_back_on_none_or_malformed_envelope():
    # A None return, or a duck-typed object missing ok/payload, must fall back —
    # not raise AttributeError past the try and abort the crawl.
    llm = _llm()
    assert llm.select_links(lambda u: None, "x", _CANDS, 2) == _CANDS[:2]

    class _Weird:  # no .ok, no .payload
        pass

    assert llm.select_links(lambda u: _Weird(), "x", _CANDS, 2) == _CANDS[:2]
    # ok True but no payload attribute at all.
    assert llm.select_links(lambda u: types.SimpleNamespace(ok=True), "x", _CANDS, 2) == _CANDS[:2]


def test_select_links_empty_candidates():
    llm = _llm()
    assert llm.select_links(lambda u: _env(True, LinkSelection(indices=[1])), "x", [], 5) == []
