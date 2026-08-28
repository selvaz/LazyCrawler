# -*- coding: utf-8 -*-
"""text.py: preprocessing, link extraction (+ exclude), canonical, date parsing."""

from __future__ import annotations

from lazycrawler.http import compile_exclude
from lazycrawler.text import (
    extract_candidate_links,
    extract_canonical_url,
    extract_page_title,
    extract_published_datetime,
    preprocess_text,
)


def test_preprocess_strips_noise():
    raw = "Real sentence one.\nWe use cookies to improve your experience\nReal sentence two."
    out = preprocess_text(raw)
    assert "Real sentence one." in out
    assert "Real sentence two." in out
    assert "cookies" not in out.lower()


def test_preprocess_keeps_prose_mentioning_boilerplate_terms():
    # Regression: unanchored terms (gdpr, advertisement, privacy policy) matched
    # inside real sentences and dropped the whole line.
    raw = (
        "The EU handed down a landmark GDPR ruling against the company this week.\n"
        "Regulators said the advertisement industry must overhaul its data practices.\n"
        "The firm updated its privacy policy in response to the sweeping decision.\n"
        "Cookie Settings\n"
        "Advertisement"
    )
    out = preprocess_text(raw)
    assert "landmark GDPR ruling" in out
    assert "advertisement industry must overhaul" in out
    assert "updated its privacy policy" in out
    # But the short banner/button lines are still removed.
    assert "Cookie Settings" not in out
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    assert "Advertisement" not in lines


def test_extract_links_dedup_and_absolute():
    html = (
        '<a href="/a">A</a><a href="/a">dup</a>'
        '<a href="https://e.org/b">B</a><a href="#frag">skip</a>'
    )
    links = extract_candidate_links(html, "https://e.org/", "e.org")
    urls = [u for _, u in links]
    assert "https://e.org/a" in urls
    assert "https://e.org/b" in urls
    assert urls.count("https://e.org/a") == 1


def test_extract_links_honors_custom_exclude():
    html = '<a href="/keep">keep</a><a href="/skipme">skip</a>'
    pat = compile_exclude([r"/skipme"])
    links = extract_candidate_links(html, "https://e.org/", "e.org", exclude_pattern=pat)
    urls = [u for _, u in links]
    assert "https://e.org/keep" in urls
    assert "https://e.org/skipme" not in urls


def test_extract_links_default_allows_about():
    html = '<a href="/about">About</a>'
    links = extract_candidate_links(html, "https://e.org/", "e.org")
    assert any(u.endswith("/about") for _, u in links)


def test_canonical_url():
    html = '<link rel="canonical" href="https://e.org/canon"/>'
    assert extract_canonical_url(html, "https://e.org/page") == "https://e.org/canon"


def test_canonical_url_absent():
    assert extract_canonical_url("<html></html>", "https://e.org/p") is None


def test_extract_page_title_unescapes_numeric_entities():
    """Measured live: a real page's own <title> carried numeric HTML
    entities ("w&#xE4;chst"), not raw accented characters -- without
    unescaping, a caller sees the literal entity text, not the actual word
    ("wächst"), for every accented title from a site that serves them
    this way (common in German/French-language markup)."""
    html = "<title>Cloud w&#xE4;chst 45% &#x2013; KI-CapEx dr&#xFC;ckt</title>"
    assert extract_page_title(html) == "Cloud wächst 45% – KI-CapEx drückt"


def test_extract_page_title_unescapes_named_entities_too():
    html = "<title>Tom &amp; Jerry &quot;Classic&quot;</title>"
    assert extract_page_title(html) == 'Tom & Jerry "Classic"'


def test_extract_page_title_falls_back_to_h1_and_still_unescapes():
    html = "<html><body><h1>Caf&eacute; Numbers</h1></body></html>"
    assert extract_page_title(html) == "Café Numbers"


def test_published_datetime_from_meta():
    html = '<meta property="article:published_time" content="2026-01-15T14:30:00Z">'
    iso = extract_published_datetime(html, "https://e.org/x")
    assert iso and iso.startswith("2026-01-15")


def test_published_datetime_none_when_missing():
    assert extract_published_datetime("<html></html>", "https://e.org/x") is None


def test_published_datetime_does_not_crash_on_leap_day(monkeypatch):
    # Regression: the future-date sanity ceiling used now.replace(year=year+2),
    # which raises ValueError on Feb 29 and killed date extraction (and the page).
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    import lazycrawler.text as text_mod

    class _FrozenLeapDay(_dt):
        @classmethod
        def now(cls, tz=None):
            return _dt(2028, 2, 29, 12, 0, 0, tzinfo=tz or _tz.utc)

    monkeypatch.setattr(text_mod, "datetime", _FrozenLeapDay)
    html = '<meta property="article:published_time" content="2026-01-15T14:30:00Z">'
    iso = extract_published_datetime(html, "https://e.org/x")
    assert iso and iso.startswith("2026-01-15")


def test_preprocess_still_strips_pipe_nav_lines():
    raw = "Real sentence one.\nHome | About | Contact | Careers\nReal sentence two."
    out = preprocess_text(raw)
    assert "Real sentence one." in out
    assert "Real sentence two." in out
    assert "About" not in out


def test_preprocess_keeps_a_pipe_line_that_is_prose():
    # A period makes it a sentence, not navigation, at any number of pipes.
    raw = "Revenue rose 4%. Margins fell | a bit | more than expected"
    assert "Margins fell" in preprocess_text(raw)


def test_preprocess_keeps_a_pipe_line_with_a_long_segment():
    # Deliberate: excluding "|" from the segments cannot preserve the old
    # classification in both directions, and this module errs toward keeping
    # text (see the _LINE_NOISE_SHORT note). A pipe line with a segment over
    # the bound is now KEPT. Widening the bound to strip it again would also
    # start stripping "A | B | <long> | C", which the old pattern kept -- and
    # gutting an article line is the worse of the two mistakes.
    raw = "Home | " + "Very Long Section Name " * 4 + "| About | Contact"
    assert "Very Long Section Name" in preprocess_text(raw)


def test_pipe_nav_does_not_backtrack_catastrophically():
    # Regression: the segment classes used to allow "|", so the prefix and the
    # repeated group could both consume a pipe. On a long, period-free,
    # pipe-heavy NON-matching line (a wiki "largest banks" table row) the
    # engine explored exponentially many splits -- measured 1.4s at 25 cells,
    # 14.8s at 30, 62.5s at 33, a factor of ~1.6 per added cell. `re` holds the
    # GIL throughout, so this froze the interpreter and made callers' own
    # join(timeout=) bounds unenforceable.
    #
    # Run in a KILLABLE SUBPROCESS, not inline: with the old pattern the call
    # never returns, so an inline timing assertion could not fail -- it would
    # hang the suite instead, which is the one outcome a regression test must
    # not have. A generous ceiling keeps this from flaking on a loaded box
    # while still separating "linear" from "exponential" by many orders.
    import os
    import subprocess
    import sys

    import lazycrawler.text as text_mod

    # The child compares paths itself and prints a fixed ASCII sentinel. Having
    # it print its own __file__ for the parent to compare looked equivalent but
    # is not: a non-ASCII checkout path, or anything else writing to stdout at
    # startup, would break the parse and fail a healthy run.
    programma = (
        "import os, sys\n"
        "import lazycrawler.text as m\n"
        # An explicit raise, not an assert: the child inherits the parent's
        # environment, so PYTHONOPTIMIZE would strip an assert and let it print
        # the sentinel without ever proving which module it loaded.
        "if not os.path.samefile(m.__file__, sys.argv[1]):\n"
        "    raise SystemExit('wrong module: ' + m.__file__)\n"
        "line = ' | '.join('Bank of Somewhere %d' % i for i in range(60)) + ' .'\n"
        "m.preprocess_text(line)\n"
        "sys.stdout.write('MATCHED')\n"
    )
    # Pin the child to the package this test imported. PYTHONPATH alone is not
    # enough -- `python -c` puts the child's cwd first on sys.path -- so cwd is
    # set as well, and the assert above proves which code actually ran.
    radice = os.path.dirname(os.path.dirname(os.path.abspath(text_mod.__file__)))
    ambiente = dict(os.environ, PYTHONPATH=radice)
    try:
        esito = subprocess.run(
            [sys.executable, "-c", programma, os.path.abspath(text_mod.__file__)],
            cwd=radice,
            capture_output=True,
            timeout=30,
            env=ambiente,
        )
    except subprocess.TimeoutExpired:
        raise AssertionError(
            "pipe-nav matching did not finish in 30s on a 60-cell line: "
            "the pattern is backtracking catastrophically again"
        ) from None
    # Checked on purpose: a child that dies instantly also "finishes within the
    # timeout", and reading only the timeout would score that as a pass.
    assert esito.returncode == 0, esito.stderr.decode(errors="replace")
    assert b"MATCHED" in esito.stdout, "the child never reached the match"
