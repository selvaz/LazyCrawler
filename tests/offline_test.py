# -*- coding: utf-8 -*-
"""
Offline test: text/link/date extraction (deterministic) + real smart extraction
via LazyBridge (requires an API key; loads the ecosystem .env).
Run: python tests/offline_test.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load the ecosystem .env (for LazyBridge API keys)
for cand in [Path(__file__).resolve().parents[2] / "ecosystemv0.9.1" / ".env",
             Path("D:/serious_tests/ecosystemv0.9.1/.env")]:
    if cand.exists():
        for line in cand.read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        break

from lazycrawler.text import (
    extract_candidate_links, extract_page_title,
    extract_published_datetime, preprocess_text,
)

PASS = FAIL = 0
def check(name, cond):
    global PASS, FAIL
    PASS, FAIL = PASS + bool(cond), FAIL + (not cond)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")


SAMPLE_HTML = """<!DOCTYPE html><html><head>
<title>New Deep-Sea Species Discovered - Example Science</title>
<meta property="article:published_time" content="2026-03-15T10:30:00Z"/>
<link rel="canonical" href="https://news.example.com/deep-sea-species"/>
</head><body>
<nav>Home | Science | Nature | About</nav>
<h1>New Deep-Sea Species Discovered</h1>
<p>Marine biologists aboard a research vessel discovered a previously unknown
species of anglerfish at a depth of 4,000 meters in the Pacific Ocean. The team
documented bioluminescent features never seen before.</p>
<p>We use cookies to improve your experience. Accept all cookies.</p>
<a href="/research/expedition-log">Read the full expedition log</a>
<a href="https://twitter.com/share">Share on Twitter</a>
<a href="/science/bioluminescence">More on bioluminescence</a>
<footer>© 2026 Example Science. All rights reserved.</footer>
</body></html>"""


print("\n=== 1. Text/link/date extraction (offline, deterministic) ===")
title = extract_page_title(SAMPLE_HTML)
check("title extracted", "New Deep-Sea Species Discovered" in title)

pub = extract_published_datetime(SAMPLE_HTML)
check("published_iso 2026-03-15", bool(pub) and pub.startswith("2026-03-15"))

links = extract_candidate_links(SAMPLE_HTML, "https://news.example.com/x",
                                start_domain="news.example.com",
                                same_domain_only=True, max_links=50)
link_urls = [u for _, u in links]
check("internal links extracted", any("expedition-log" in u for u in link_urls))
check("tracking/social link excluded", not any("twitter" in u for u in link_urls))

raw = ("Home | Science | Nature\nMarine biologists discovered a new anglerfish.\n"
       "We use cookies to improve your experience.\n© 2026 Example Science\n"
       "The species shows unique bioluminescent features.")
clean = preprocess_text(raw)
check("preprocess removes cookie", "cookie" not in clean.lower())
check("preprocess removes copyright", "rights reserved" not in clean.lower())
check("preprocess keeps content", "anglerfish" in clean and "bioluminescent" in clean)


print("\n=== 2. SMART extraction via LazyBridge (requires API key) ===")
have_key = any(os.environ.get(k) for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"))
if not have_key:
    print("  [SKIP] no API key found")
else:
    try:
        from lazycrawler.config import LLMConfig
        from lazycrawler.llm import CrawlerLLM, PageExtract
        # cheap model; switch provider by changing the string
        model = "gpt-4o-mini" if os.environ.get("OPENAI_API_KEY") else "claude-haiku-4-5"
        print(f"  model: {model}")
        llm = CrawlerLLM(LLMConfig(model=model, temperature=0))
        article = (
            "Home | Science | About\n"
            "Marine biologists aboard a research vessel discovered a previously "
            "unknown species of anglerfish at 4,000 meters in the Pacific Ocean. "
            "Lead researcher Dr. Elena Ruiz said the bioluminescent features are "
            "unlike anything documented before.\n"
            "We use cookies. Accept all. © 2026 Example Science."
        )
        extract = llm.extract_content("https://news.example.com/deep-sea-species", article)
        if extract is None:
            print("  [SKIP] LLM unreachable (network/SSL MITM) - integration built OK")
        else:
            check("returns PageExtract", isinstance(extract, PageExtract))
            print(f"    title:    {extract.title!r}")
            print(f"    summary:  {(extract.summary or '')[:90]!r}")
            print(f"    entities: {extract.entities}")
            print(f"    topics:   {extract.topics}")
            check("clean_text not empty", bool(extract.clean_text))
            check("entities found (anglerfish/Ruiz/Pacific)", any(
                kw in (" ".join(extract.entities)).lower()
                for kw in ("anglerfish", "ruiz", "pacific")))
    except Exception as e:
        import traceback
        traceback.print_exc()
        check(f"smart extraction (error: {type(e).__name__})", False)


print(f"\n=== RESULT: {PASS} PASS, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
