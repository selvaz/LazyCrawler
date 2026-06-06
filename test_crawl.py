# -*- coding: utf-8 -*-
"""
test_crawl.py  —  crawl diagnostico con verbose completo

Funziona sia da Spyder/IPython (usa i DEFAULT qui sotto) che da terminale.

Uso da terminale:
    python test_crawl.py https://tuo-sito.com
    python test_crawl.py https://tuo-sito.com --depth 2 --pages 20
    python test_crawl.py https://tuo-sito.com --smart --topic "machine learning"
    python test_crawl.py https://tuo-sito.com --cross-domain
    python test_crawl.py https://tuo-sito.com --quiet
"""

import argparse
import logging
import sys

from lazycrawler import WebCrawler, set_log_level
from lazycrawler.config import CrawlerConfig, HTTPConfig, LLMConfig


# ================================================================
#  CONFIGURA QUI  (valori usati quando lanci da Spyder / IPython)
# ================================================================
DEFAULT_URL          = "https://lazybridge.com"
DEFAULT_DEPTH        = 3
DEFAULT_PAGES        = 10
DEFAULT_LINKS        = 5
DEFAULT_DELAY        = 1.0
DEFAULT_SMART        = False
DEFAULT_TOPIC        = ""
DEFAULT_CROSS_DOMAIN = False
DEFAULT_NO_ROBOTS    = False
DEFAULT_JS           = False
DEFAULT_MODEL        = "gpt-4o-mini"
DEFAULT_QUIET        = False        # True = solo INFO, False = DEBUG completo
# ================================================================


def _running_in_spyder():
    """True se siamo dentro Spyder o un ambiente interattivo senza argomenti CLI."""
    if len(sys.argv) <= 1:
        return True
    # Spyder inietta il path dello script come argv[0]; nessun URL viene passato
    known_launchers = ("spyder", "ipykernel", "jupyter", "_jb_", "pydev")
    return any(kw in sys.argv[0].lower() for kw in known_launchers)


def parse_args():
    if _running_in_spyder():
        # Costruiamo un namespace finto con i DEFAULT
        class _Defaults:
            url          = DEFAULT_URL
            depth        = DEFAULT_DEPTH
            pages        = DEFAULT_PAGES
            links        = DEFAULT_LINKS
            delay        = DEFAULT_DELAY
            smart        = DEFAULT_SMART
            topic        = DEFAULT_TOPIC
            cross_domain = DEFAULT_CROSS_DOMAIN
            no_robots    = DEFAULT_NO_ROBOTS
            js           = DEFAULT_JS
            model        = DEFAULT_MODEL
            quiet        = DEFAULT_QUIET
        print("[Spyder mode] uso DEFAULT_URL:", DEFAULT_URL)
        return _Defaults()

    p = argparse.ArgumentParser(description="LazyCrawler diagnostic test")
    p.add_argument("url",                                        help="URL di partenza")
    p.add_argument("--depth",        type=int,   default=2,     help="max_depth (default 2)")
    p.add_argument("--pages",        type=int,   default=10,    help="max_pages (default 10)")
    p.add_argument("--links",        type=int,   default=10,    help="max_links_per_level (default 10)")
    p.add_argument("--delay",        type=float, default=1.0,   help="link_delay secondi (default 1.0)")
    p.add_argument("--smart",        action="store_true",       help="abilita LLM (mode=smart)")
    p.add_argument("--cross-domain", action="store_true",       help="segui link cross-domain")
    p.add_argument("--no-robots",    action="store_true",       help="ignora robots.txt")
    p.add_argument("--js",           action="store_true",       help="render JS con browser headless")
    p.add_argument("--model",        default="gpt-4o-mini",     help="modello LLM (default gpt-4o-mini)")
    p.add_argument("--topic",        default="",                help="topic per smart link selection")
    p.add_argument("--quiet",        action="store_true",       help="solo INFO, no DEBUG")
    return p.parse_args()


def main():
    args = parse_args()

    set_log_level(logging.INFO if args.quiet else logging.DEBUG)

    crawler_cfg = CrawlerConfig(
        max_depth=args.depth,
        max_pages=args.pages,
        max_links_per_level=args.links,
        same_domain_only=not args.cross_domain,
        respect_robots=not args.no_robots,
    )
    http_cfg = HTTPConfig(
        link_delay=args.delay,
        verify_ssl=False,       # disabilita SSL check (Avast MITM)
        render_js=args.js,
    )
    llm_cfg = LLMConfig(model=args.model) if args.smart else None
    mode    = "smart" if args.smart else "pure"

    print(f"\n{'='*60}")
    print(f"  URL:         {args.url}")
    print(f"  mode:        {mode}")
    print(f"  depth:       {args.depth}  |  pages: {args.pages}  |  links/level: {args.links}")
    print(f"  same-domain: {not args.cross_domain}  |  robots: {not args.no_robots}  |  JS: {args.js}")
    if args.smart:
        print(f"  model:       {args.model}  |  topic: {args.topic!r}")
    print(f"{'='*60}\n")

    crawler = WebCrawler(crawler_cfg=crawler_cfg, http_cfg=http_cfg, llm_cfg=llm_cfg)
    try:
        results = crawler.crawl(args.url, mode=mode, topic=args.topic)
    finally:
        crawler.close()

    # --- risultati ---
    print(f"\n{'='*60}")
    print(f"  RISULTATI: {len(results)} pagine")
    print(f"{'='*60}")

    status_counts = {}
    for r in results:
        status_counts[r.status] = status_counts.get(r.status, 0) + 1

    for r in results:
        icon = {"done": "✓", "fetch_error": "✗", "no_text": "~",
                "llm_error": "!", "robots_blocked": "⊘", "blacklisted": "⊘"}.get(r.status, "?")
        print(f"\n  {icon} [d{r.depth}] {r.url}")
        print(f"       status:  {r.status}")
        if r.title:
            print(f"       title:   {r.title[:80]}")
        if r.summary:
            print(f"       summary: {r.summary[:120]}")
        if r.text:
            preview = r.text[:120].replace("\n", " ").strip()
            print(f"       text:    {len(r.text)} chars -> {preview!r}")
        if r.entities:
            print(f"       entities:{r.entities[:6]}")
        if r.topics:
            print(f"       topics:  {r.topics[:6]}")
        if r.error:
            print(f"       error:   {r.error}")

    print(f"\n  Riepilogo status: { {k: v for k, v in sorted(status_counts.items())} }")
    print(f"  Pagine 'done':    {status_counts.get('done', 0)}/{len(results)}")
    print()


if __name__ == "__main__":
    main()
else:
    # Supporto run diretta da Spyder con F5 / runfile()
    main()
