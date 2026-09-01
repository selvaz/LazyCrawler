# -*- coding: utf-8 -*-
"""Which cycle gets which digest format, and what the header promises.

Pure routing and string assembly -- no model is called here. That is the
point: the choice of format is made once, from (cycle, engine), and a wrong
answer does not fail loudly. It writes a perfectly well-formed digest in the
wrong shape, which the delta report and the Investment Committee's newsfeed
then read as if nothing had changed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

make_news_report = pytest.importorskip("make_news_report")

ASSET_CLASS_SECTIONS = make_news_report.ASSET_CLASS_SECTIONS
DIGEST_FORMATS = make_news_report.DIGEST_FORMATS
GEO_COUNTRY_SECTIONS = make_news_report.GEO_COUNTRY_SECTIONS
_digest_header = make_news_report._digest_header
_select_digest_format = make_news_report._select_digest_format


@pytest.mark.parametrize(
    ("cycle", "engine", "expected"),
    [
        # The two cycles whose shape other code depends on. usclose is the
        # delta report's baseline and the committee's news context;
        # morning/claude is the delta report's query side.
        ("usclose", "claude", "thematic"),
        ("usclose", "deepseek", "thematic"),
        ("morning", "claude", "thematic"),
        # The two new formats.
        ("morning", "deepseek", "geographic"),
        ("europeclose", "claude", "asset-class"),
        # europeclose is keyed on the cycle alone: whichever engine is
        # configured for it, the digest is what that cycle is *for*.
        ("europeclose", "deepseek", "asset-class"),
        # An ad-hoc manual run declares no cycle and must not silently
        # acquire a scheduled cycle's format.
        (None, "claude", "thematic"),
        (None, "deepseek", "thematic"),
    ],
)
def test_format_routing(cycle, engine, expected):
    assert _select_digest_format(cycle, engine) == expected


def test_every_format_is_defined():
    for cycle in (None, "morning", "europeclose", "usclose"):
        for engine in ("claude", "deepseek"):
            assert _select_digest_format(cycle, engine) in DIGEST_FORMATS


def test_prompts_accept_their_placeholders():
    """Each prompt is built by concatenation around a shared rule block, so a
    stray brace in any part would only surface at format() time -- i.e. at
    23:00, mid-run."""
    for name, (template, _sections) in DIGEST_FORMATS.items():
        rendered = template.format(n=2, items="- an item")
        assert "- an item" in rendered, name
        assert "{items}" not in rendered, name


def test_fixed_skeleton_prompts_ask_for_every_section_they_advertise():
    """The header names the sections and the prompt orders them. If the two
    drift, the header promises a section the report never had."""
    for fmt, sections in (
        ("geographic", GEO_COUNTRY_SECTIONS),
        ("asset-class", ASSET_CLASS_SECTIONS),
    ):
        template, declared = DIGEST_FORMATS[fmt]
        assert declared == sections
        for section in sections:
            assert f"## {section}" in template, (fmt, section)


def test_thematic_format_has_no_fixed_skeleton():
    """It lets the model choose its own themes; that is what it is for."""
    _template, sections = DIGEST_FORMATS["thematic"]
    assert sections is None


def test_no_fabrication_rule_reaches_both_new_formats():
    marker = "NEVER invent anything"
    assert marker in DIGEST_FORMATS["geographic"][0]
    assert marker in DIGEST_FORMATS["asset-class"][0]


def test_asset_class_prompt_does_not_ask_for_delta_tagging():
    """It is shown one cycle and no baseline, so a NEW/UPDATED tag would be
    invented. Diffing against recent coverage belongs to
    make_digest_delta_report.py, which is handed the digests to diff."""
    template, _ = DIGEST_FORMATS["asset-class"]
    assert "[NEW]" not in template
    assert "[UPDATED" not in template


def test_header_states_structure_session_and_sections():
    header = _digest_header(
        "news_20260901_153016",
        cycle="europeclose",
        engine_name="claude",
        n_articles=451,
        digest_format="asset-class",
    )
    assert "europeclose" in header
    assert "news_20260901_153016" in header
    assert "451" in header
    assert "asset-class" in header
    for section in ASSET_CLASS_SECTIONS:
        assert section in header


def test_header_for_thematic_format_omits_a_section_list():
    """There is no fixed skeleton to promise."""
    header = _digest_header(
        "news_20260901_200016",
        cycle="usclose",
        engine_name="claude",
        n_articles=464,
        digest_format="thematic",
    )
    assert "**Sections:**" not in header


def test_header_labels_a_cycleless_run():
    header = _digest_header(
        "news_20260901_110000",
        cycle=None,
        engine_name="deepseek",
        n_articles=10,
        digest_format="geographic",
    )
    assert "ad-hoc" in header
