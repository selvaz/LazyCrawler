# -*- coding: utf-8 -*-
"""
lazycrawler.db
==============
SQLite persistence with a schema rethought for crawling (no more web pages
forced into an email schema).

SCHEMA
------
sessions      one row per crawl/search run
pages         global content cache, keyed by url_hash (cross-session)
crawl_edges   which session reached which page, from where and at what depth

3-LEVEL DEDUP
-------------
1) URL (pre-fetch):    page exists, status='done', crawled_at within TTL
                       -> skip fetch, just add the edge              [save HTTP]
2) Content (post-fetch, pre-LLM): content_hash = sha256(raw_text)
                       already present -> reuse the row, skip LLM    [save tokens]
3) Smart-on-pure:      a pure page can be enriched to smart without re-fetch
                       (raw_text is already stored)

The cache returns the clean page (pure) or the summary + structured fields
(smart) depending on the requested mode — see WebCrawler.

TTL configurable (DBConfig.ttl_hours). force_refresh bypasses the cache.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .config import DBConfig
from .http import get_base_domain, url_hash


def utc_now_iso() -> str:
    """Current UTC date/time in ISO 8601."""
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: str) -> Optional[datetime]:
    """Parse a stored ISO timestamp; None if unparseable."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


# =============================================================================
# SCHEMA
# =============================================================================

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
  session_id   TEXT PRIMARY KEY,
  created_at   TEXT NOT NULL,
  topic        TEXT,
  seed         TEXT,
  mode         TEXT NOT NULL,
  source       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pages (
  url_hash       TEXT PRIMARY KEY,
  url            TEXT NOT NULL,
  domain         TEXT,
  is_pdf         INTEGER NOT NULL DEFAULT 0,
  status         TEXT NOT NULL,
  mode           TEXT NOT NULL,
  error          TEXT,
  raw_text       TEXT,
  clean_text     TEXT,
  title          TEXT,
  summary        TEXT,
  entities_json  TEXT,
  topics_json    TEXT,
  published_iso  TEXT,
  content_hash   TEXT,
  extract_json   TEXT,
  crawled_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pages_domain        ON pages(domain);
CREATE INDEX IF NOT EXISTS idx_pages_status        ON pages(status);
CREATE INDEX IF NOT EXISTS idx_pages_content_hash  ON pages(content_hash);
CREATE INDEX IF NOT EXISTS idx_pages_crawled_at    ON pages(crawled_at);

CREATE TABLE IF NOT EXISTS crawl_edges (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id  TEXT NOT NULL,
  url_hash    TEXT NOT NULL,
  source_url  TEXT,
  depth       INTEGER NOT NULL DEFAULT 0,
  added_at    TEXT NOT NULL,
  UNIQUE(session_id, url_hash),
  FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE,
  FOREIGN KEY (url_hash)   REFERENCES pages(url_hash)      ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_edges_session ON crawl_edges(session_id);
CREATE INDEX IF NOT EXISTS idx_edges_urlhash ON crawl_edges(url_hash);
"""

_FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
  url_hash UNINDEXED,
  title,
  clean_text
);
"""

# "Page" fields managed by upsert.
_PAGE_FIELDS = [
    "url", "domain", "is_pdf", "status", "mode", "error", "raw_text",
    "clean_text", "title", "summary", "entities_json", "topics_json",
    "published_iso", "content_hash", "extract_json", "crawled_at",
]


# =============================================================================
# DB MANAGER
# =============================================================================

class CrawlerDB:
    """
    SQLite manager: schema, dedup, CRUD for sessions / pages / crawl_edges.

    Parameters
    ----------
    cfg : DBConfig
        DB path, cache TTL, force_refresh, enable_fts.
    """

    def __init__(self, cfg: Optional[DBConfig] = None):
        self.cfg = cfg or DBConfig()
        # check_same_thread=False + a reentrant lock: the connection is shared
        # across worker threads in parallel mode; every access is serialized by
        # ``self._lock`` (the DB is never the bottleneck — fetch/LLM are).
        self.conn = sqlite3.connect(self.cfg.db_path, check_same_thread=False)
        self._lock = threading.RLock()
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA foreign_keys=ON;")
        self.conn.executescript(_SCHEMA_SQL)
        # Migration for DBs created before extract_json existed.
        try:
            self.conn.execute("ALTER TABLE pages ADD COLUMN extract_json TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists
        if self.cfg.enable_fts:
            try:
                self.conn.executescript(_FTS_SQL)
            except sqlite3.OperationalError:
                # FTS5 not available in this SQLite build - degrade.
                self.cfg.enable_fts = False
        self.conn.commit()

    # -- Sessions -------------------------------------------------------------

    def create_session(
        self,
        session_id: str,
        *,
        topic: str = "",
        seed: str = "",
        mode: str = "pure",
        source: str = "crawl",
    ) -> str:
        """Create (or ignore if existing) a session and return its id."""
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO sessions (session_id, created_at, topic, seed, mode, source) "
                "VALUES (?,?,?,?,?,?)",
                (session_id, utc_now_iso(), topic, seed, mode, source),
            )
            self.conn.commit()
        return session_id

    # -- Dedup level 1: URL + TTL --------------------------------------------

    def get_fresh_page(self, url: str) -> Optional[Dict[str, Any]]:
        """
        Return the cached page for this URL if it is 'done' and within TTL
        (and force_refresh is False), else None. The caller decides whether the
        cached content satisfies the requested mode.
        """
        if self.cfg.force_refresh:
            return None
        row = self.get_page(url_hash(url))
        if not row or row.get("status") != "done":
            return None
        crawled = _parse_iso(row.get("crawled_at") or "")
        if not crawled:
            return None
        if datetime.now(timezone.utc) - crawled > timedelta(hours=self.cfg.ttl_hours):
            return None
        return row

    def is_fresh(self, url: str) -> bool:
        """True if a fresh 'done' page exists for this URL (see get_fresh_page)."""
        return self.get_fresh_page(url) is not None

    # -- Dedup level 2: content_hash -----------------------------------------

    def find_by_content_hash(self, content_hash: str) -> Optional[Dict[str, Any]]:
        """A 'done' page with the same content_hash (to skip the LLM)."""
        if not content_hash:
            return None
        with self._lock:
            cur = self.conn.execute(
                "SELECT * FROM pages WHERE content_hash=? AND status='done' LIMIT 1",
                (content_hash,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    # -- Pages ----------------------------------------------------------------

    def get_page(self, uh: str) -> Optional[Dict[str, Any]]:
        """Page row for url_hash (with entities/topics/data deserialized), or None."""
        with self._lock:
            cur = self.conn.execute("SELECT * FROM pages WHERE url_hash=?", (uh,))
            row = cur.fetchone()
        return self._row_to_page(dict(row)) if row else None

    def upsert_page(self, page: Dict[str, Any]) -> str:
        """
        Insert or update a page (keyed by url_hash). Returns url_hash.

        ``page`` accepts the _PAGE_FIELDS keys plus 'url' (required) and,
        optionally, 'url_hash' (else derived from url). entities/topics lists may
        be passed as 'entities'/'topics' (they are serialized into *_json).
        """
        url = page["url"]
        uh = page.get("url_hash") or url_hash(url)

        data = dict(page)
        data.setdefault("domain", get_base_domain(url))
        data.setdefault("crawled_at", utc_now_iso())
        data["is_pdf"] = int(bool(data.get("is_pdf", False)))

        # Normalize lists -> JSON
        if "entities" in data and "entities_json" not in data:
            data["entities_json"] = json.dumps(data.get("entities") or [], ensure_ascii=False)
        if "topics" in data and "topics_json" not in data:
            data["topics_json"] = json.dumps(data.get("topics") or [], ensure_ascii=False)
        if "data" in data and "extract_json" not in data:
            d = data.get("data")
            data["extract_json"] = json.dumps(d, ensure_ascii=False) if d else None

        cols = ["url_hash"] + _PAGE_FIELDS
        values = [uh] + [data.get(f) for f in _PAGE_FIELDS]
        placeholders = ",".join("?" for _ in cols)
        updates = ",".join(f"{c}=excluded.{c}" for c in _PAGE_FIELDS)

        with self._lock:
            self.conn.execute(
                f"INSERT INTO pages ({','.join(cols)}) VALUES ({placeholders}) "
                f"ON CONFLICT(url_hash) DO UPDATE SET {updates}",
                values,
            )
            if self.cfg.enable_fts:
                self._index_fts(uh, data.get("title") or "", data.get("clean_text") or "")
            self.conn.commit()
        return uh

    def _index_fts(self, uh: str, title: str, clean_text: str) -> None:
        """Reindex a page in the FTS table (delete + insert)."""
        try:
            self.conn.execute("DELETE FROM pages_fts WHERE url_hash=?", (uh,))
            self.conn.execute(
                "INSERT INTO pages_fts (url_hash, title, clean_text) VALUES (?,?,?)",
                (uh, title, clean_text),
            )
        except sqlite3.OperationalError:
            pass

    # -- Edges ----------------------------------------------------------------

    def add_edge(
        self,
        session_id: str,
        uh: str,
        *,
        source_url: Optional[str] = None,
        depth: int = 0,
    ) -> None:
        """Record that a session reached a page (idempotent)."""
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO crawl_edges (session_id, url_hash, source_url, depth, added_at) "
                "VALUES (?,?,?,?,?)",
                (session_id, uh, source_url, depth, utc_now_iso()),
            )
            self.conn.commit()

    # -- Queries --------------------------------------------------------------

    def get_pages(
        self,
        session_id: Optional[str] = None,
        status: Optional[str] = "done",
        limit: int = 0,
    ) -> List[Dict[str, Any]]:
        """
        Pages, optionally filtered by session (via crawl_edges) and status.
        """
        params: List[Any] = []
        if session_id:
            sql = (
                "SELECT p.* FROM pages p "
                "JOIN crawl_edges e ON e.url_hash = p.url_hash "
                "WHERE e.session_id=?"
            )
            params.append(session_id)
        else:
            sql = "SELECT * FROM pages p WHERE 1=1"
        if status:
            sql += " AND p.status=?"
            params.append(status)
        sql += " ORDER BY p.crawled_at DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._row_to_page(dict(r)) for r in rows]

    def search_text(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        """
        Full-text search over title + clean_text (FTS5). Falls back to LIKE if
        FTS is unavailable.
        """
        if self.cfg.enable_fts:
            try:
                with self._lock:
                    rows = self.conn.execute(
                        "SELECT p.* FROM pages_fts f JOIN pages p ON p.url_hash = f.url_hash "
                        "WHERE pages_fts MATCH ? ORDER BY rank LIMIT ?",
                        (query, limit),
                    ).fetchall()
                return [self._row_to_page(dict(r)) for r in rows]
            except sqlite3.OperationalError:
                pass
        like = f"%{query}%"
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM pages WHERE status='done' AND "
                "(title LIKE ? OR clean_text LIKE ?) ORDER BY crawled_at DESC LIMIT ?",
                (like, like, limit),
            ).fetchall()
        return [self._row_to_page(dict(r)) for r in rows]

    def stats(self) -> Dict[str, int]:
        """Quick counts: sessions, total/done pages, edges."""
        def _count(sql: str) -> int:
            with self._lock:
                return int(self.conn.execute(sql).fetchone()[0])
        return {
            "sessions": _count("SELECT COUNT(*) FROM sessions"),
            "pages": _count("SELECT COUNT(*) FROM pages"),
            "pages_done": _count("SELECT COUNT(*) FROM pages WHERE status='done'"),
            "edges": _count("SELECT COUNT(*) FROM crawl_edges"),
        }

    @staticmethod
    def _row_to_page(row: Dict[str, Any]) -> Dict[str, Any]:
        """Deserialize *_json fields into Python lists for the caller's convenience."""
        for src, dst in (("entities_json", "entities"), ("topics_json", "topics")):
            raw = row.get(src)
            if raw:
                try:
                    row[dst] = json.loads(raw)
                except Exception:
                    row[dst] = []
            else:
                row[dst] = []
        ej = row.get("extract_json")
        if ej:
            try:
                row["data"] = json.loads(ej)
            except Exception:
                row["data"] = None
        else:
            row["data"] = None
        return row

    def close(self) -> None:
        self.conn.close()
