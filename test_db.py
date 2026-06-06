# -*- coding: utf-8 -*-
"""
test_db.py  —  test completo della persistenza DB

Cosa testa:
  1. Crawl #1  -> scrive pagine nel DB
  2. Stats DB  -> sessioni / pagine / edge
  3. Crawl #2  -> stesso sito, deve usare la cache (dedup URL+TTL)
  4. Verifica dedup -> pagine non raddoppiate, aggiunto solo edge
  5. is_fresh() -> controlla se URL e' gia' in cache
  6. get_pages() -> legge pagine per sessione
  7. search_text() -> full-text search nel contenuto crawlato
  8. find_by_content_hash -> dedup livello 2

Funziona da Spyder (F5) e da terminale (python test_db.py).
"""

import logging
import os
import sys

# Forza UTF-8 su console Windows (cp1252 altrimenti rompe i caratteri Unicode)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from lazycrawler import CrawlerDB, WebCrawler, set_log_level
from lazycrawler.config import CrawlerConfig, DBConfig, HTTPConfig

# ================================================================
#  CONFIGURA QUI
# ================================================================
TEST_URL   = "https://quotes.toscrape.com"
DB_PATH    = "test_crawl.db"
TTL_HOURS  = 24        # ore prima che la cache scada
DEPTH      = 1
PAGES      = 4
LINKS      = 3
DELAY      = 0.5
SEARCH_Q   = "life"    # parola da cercare nel full-text search
VERBOSE    = False     # True = DEBUG crawler, False = solo output del test
# ================================================================


def sep(title=""):
    w = 60
    if title:
        print(f"\n{'-'*4} {title} {'-'*(w - len(title) - 6)}")
    else:
        print("-" * w)


def show_stats(db: CrawlerDB, label: str):
    s = db.stats()
    print(f"  [{label}]  sessioni={s['sessions']}  pagine={s['pages']} "
          f"(done={s['pages_done']})  edge={s['edges']}")


def show_page(p: dict, prefix="  "):
    url   = p.get("url", "")[:70]
    title = (p.get("title") or "")[:55]
    mode  = p.get("mode", "?")
    st    = p.get("status", "?")
    text  = p.get("clean_text") or ""
    print(f"{prefix}[{mode}/{st}] {url}")
    if title:
        print(f"{prefix}  title:  {title}")
    if text:
        preview = text[:100].replace("\n", " ").strip()
        print(f"{prefix}  text:   {len(text)} chars -> {preview!r}")


def main():
    set_log_level(logging.DEBUG if VERBOSE else logging.WARNING)

    # pulizia db precedente per test pulito
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
        print(f"[setup] rimosso DB precedente: {DB_PATH}")

    db_cfg = DBConfig(db_path=DB_PATH, ttl_hours=TTL_HOURS, enable_fts=True)
    db     = CrawlerDB(db_cfg)

    crawler_cfg = CrawlerConfig(
        max_depth=DEPTH, max_pages=PAGES, max_links_per_level=LINKS,
    )
    http_cfg = HTTPConfig(link_delay=DELAY, verify_ssl=False)

    # ── 1. CRAWL #1 ──────────────────────────────────────────────
    sep("1. CRAWL #1  (scrive nel DB)")
    crawler = WebCrawler(crawler_cfg=crawler_cfg, http_cfg=http_cfg, db=db)
    results1 = crawler.crawl(TEST_URL, mode="pure", session_id="session_A")
    crawler.close()

    print(f"  Pagine restituite: {len(results1)}")
    for r in results1:
        icon = "OK" if r.status == "done" else "FAIL"
        print(f"    {icon} [d{r.depth}] {r.url[:65]}  [{r.status}]  from_cache={r.from_cache}")

    sep("Stats dopo crawl #1")
    show_stats(db, "dopo crawl #1")

    # ── 2. is_fresh() ────────────────────────────────────────────
    sep("2. is_fresh()  - verifica cache per ogni URL crawlato")
    for r in results1:
        fresh = db.is_fresh(r.url)
        print(f"  is_fresh({r.url[:55]!r}) -> {fresh}")

    # ── 3. get_pages() per sessione ──────────────────────────────
    sep("3. get_pages(session_id='session_A')")
    pages_A = db.get_pages(session_id="session_A")
    print(f"  Trovate {len(pages_A)} pagine per session_A:")
    for p in pages_A:
        show_page(p)

    # ── 4. CRAWL #2 — stessa URL, deve usare CACHE ───────────────
    sep("4. CRAWL #2  (stesso sito -> deve usare cache, no fetch)")
    crawler2 = WebCrawler(crawler_cfg=crawler_cfg, http_cfg=http_cfg, db=db)
    results2 = crawler2.crawl(TEST_URL, mode="pure", session_id="session_B")
    crawler2.close()

    print(f"  Pagine restituite: {len(results2)}")
    cache_hits = sum(1 for r in results2 if r.from_cache)
    live_fetch = sum(1 for r in results2 if not r.from_cache)
    for r in results2:
        icon = "CACHE" if r.from_cache else "FETCH"
        print(f"    {icon} [d{r.depth}] {r.url[:65]}  from_cache={r.from_cache}")
    print(f"\n  Cache hits: {cache_hits}  |  Live fetch: {live_fetch}")
    if cache_hits == len(results2):
        print("  OK DEDUP OK: tutti i risultati venivano dalla cache, nessuna richiesta HTTP")
    else:
        print(f"  WARN {live_fetch} pagine ri-fetchate (TTL scaduto o URL nuovo)")

    sep("Stats dopo crawl #2 (pagine non devono raddoppiare, solo edges)")
    show_stats(db, "dopo crawl #2")

    # ── 5. Confronto pagine totali ────────────────────────────────
    sep("5. Tutte le pagine nel DB")
    all_pages = db.get_pages(status="done")
    print(f"  Totale pagine 'done': {len(all_pages)}")
    for p in all_pages:
        show_page(p)

    # ── 6. full-text search ───────────────────────────────────────
    sep(f"6. search_text('{SEARCH_Q}')")
    hits = db.search_text(SEARCH_Q, limit=5)
    print(f"  Trovate {len(hits)} pagine con '{SEARCH_Q}':")
    for p in hits:
        show_page(p, prefix="    ")

    # ── 7. find_by_content_hash (dedup L2) ───────────────────────
    sep("7. find_by_content_hash  (dedup livello 2)")
    from lazycrawler.http import content_hash as _chash
    if results1:
        # prende il testo della prima pagina done e ne calcola l'hash
        r0 = next((r for r in results1 if r.status == "done" and r.text), None)
        if r0:
            # ricrea l'hash dal testo grezzo (clean_text è già il preprocessed)
            row = db.get_page(r0.url_hash)
            raw = (row or {}).get("raw_text") or r0.text or ""
            chash = _chash(raw)
            found = db.find_by_content_hash(chash)
            if found:
                print(f"  OK content_hash trovato per: {found.get('url','')[:65]}")
                print(f"    hash: {chash[:20]}...")
            else:
                print(f"  FAIL content_hash non trovato (hash: {chash[:20]}...)")
        else:
            print("  (nessuna pagina done con testo per il test)")

    # ── 8. Sessioni nel DB ────────────────────────────────────────
    sep("8. Sessioni registrate")
    with db._lock:
        rows = db.conn.execute(
            "SELECT session_id, created_at, topic, seed, mode, source FROM sessions"
        ).fetchall()
    for row in rows:
        print(f"  session: {row[0]}")
        print(f"    created: {row[1]}  mode: {row[4]}  source: {row[5]}")
        print(f"    seed:    {row[3][:60]}")

    # ── FINE ──────────────────────────────────────────────────────
    sep()
    show_stats(db, "FINALE")
    db.close()
    print(f"\n  DB salvato in: {os.path.abspath(DB_PATH)}")
    print()


if __name__ == "__main__":
    main()
else:
    main()   # Spyder runfile()
