"""What a crawled headline must not be able to do to the report.

The row embedded in the report carries titles, summaries and URLs taken from
arbitrary commentary pages. `json.dumps` leaves `</script>` intact, so a title
containing one closes the script element early and everything after it is
parsed as markup by whoever opens the file.

These run the escaping rather than reading it, which is the only way this was
going to be got right: the first two attempts wrote `"\\u003c"` with a single
backslash, which in Python source *is* `<`, so the code replaced a character
with itself and looked correct on the page.
"""

from __future__ import annotations

import json

import econ_report

HOSTILE = "Q2 GDP </script><img src=x onerror=alert(1)> revised"


def test_the_payload_cannot_end_the_script_element():
    out = econ_report._script_safe_json({"title": HOSTILE})
    assert "</script>" not in out
    assert "<" not in out and ">" not in out


def test_the_reader_still_gets_the_original_text():
    """Escaping that changed the value would be a different bug."""
    payload = {"title": HOSTILE, "nested": [{"summary": "a & b < c"}]}
    assert json.loads(econ_report._script_safe_json(payload)) == payload


def test_ampersands_go_too():
    """`&` matters because `&lt;script&gt;` in an attribute context is one
    HTML-decoding away from the same problem."""
    assert "&" not in econ_report._script_safe_json({"t": "M&A"})


def test_the_rendered_report_carries_no_live_markup_from_the_row():
    html = econ_report.render_html(
        {
            "date": "2026-08-15",
            "releases": [],
            "all_status": [],
            "title": HOSTILE,
        }
    )
    body = html.split("__ROW_JSON__")[0]
    assert HOSTILE not in html, "the hostile title reached the page verbatim"
    assert "onerror=alert(1)" not in body or "\\u003c" in html
