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
import weakref
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from ._log import log
from .config import DBConfig
from .http import _quiet_close, get_base_domain, url_hash


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
        log.debug("could not parse stored timestamp %r", value)
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
  sentiment      TEXT,
  notes          TEXT,
  markdown       TEXT,
  published_iso  TEXT,
  content_hash   TEXT,
  extract_json   TEXT,
  links_json     TEXT,
  requested_url  TEXT,
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

CREATE TABLE IF NOT EXISTS artifacts (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  url_hash       TEXT NOT NULL,
  position       INTEGER NOT NULL DEFAULT 0,
  artifact_type  TEXT NOT NULL,
  src_url        TEXT,
  alt            TEXT,
  caption        TEXT,
  context        TEXT,
  content        TEXT,
  content_format TEXT,
  data_json      TEXT,
  summary        TEXT,
  mime           TEXT,
  width          INTEGER,
  height         INTEGER,
  bytes_hash     TEXT,
  size_bytes     INTEGER,
  blob           BLOB,
  meta_json      TEXT,
  content_hash   TEXT,
  created_at     TEXT NOT NULL,
  UNIQUE(url_hash, content_hash),
  FOREIGN KEY (url_hash) REFERENCES pages(url_hash) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_artifacts_urlhash ON artifacts(url_hash);
CREATE INDEX IF NOT EXISTS idx_artifacts_type ON artifacts(artifact_type);
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
    "url",
    "domain",
    "is_pdf",
    "status",
    "mode",
    "error",
    "raw_text",
    "clean_text",
    "title",
    "summary",
    "entities_json",
    "topics_json",
    "sentiment",
    "notes",
    "markdown",
    "published_iso",
    "content_hash",
    "extract_json",
    "links_json",
    "requested_url",
    "crawled_at",
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
        self._migrate()
        if self.cfg.enable_fts:
            try:
                self.conn.executescript(_FTS_SQL)
            except sqlite3.OperationalError:
                # FTS5 not available in this SQLite build - degrade to LIKE search.
                log.warning(
                    "FTS5 unavailable in this SQLite build - search_text() falls back to LIKE"
                )
                self.cfg.enable_fts = False
        self.conn.commit()
        # Close the SQLite connection automatically on GC / interpreter exit, so
        # callers never strictly need db.close() (it stays available for
        # deterministic release / WAL checkpoint).
        self._finalizer = weakref.finalize(self, _quiet_close, self.conn)

    # -- Schema versioning / migrations ---------------------------------------

    SCHEMA_VERSION = 2

    def _migrate(self) -> None:
        """Apply forward migrations gated by ``PRAGMA user_version`` so each step
        runs once. Legacy column adds stay idempotent (older DBs created before
        versioning had these applied via ``ADD COLUMN`` already)."""
        version = int(self.conn.execute("PRAGMA user_version").fetchone()[0])
        if version < 1:
            for col in ("extract_json", "sentiment", "notes", "links_json", "markdown"):
                try:
                    self.conn.execute(f"ALTER TABLE pages ADD COLUMN {col} TEXT")
                except sqlite3.OperationalError:
                    log.debug("pages.%s already present (no migration needed)", col)
        if version < 2:
            try:
                self.conn.execute("ALTER TABLE pages ADD COLUMN requested_url TEXT")
            except sqlite3.OperationalError:
                log.debug("pages.requested_url already present (no migration needed)")
        if version < self.SCHEMA_VERSION:
            self.conn.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")
            self.conn.commit()
            log.debug("db schema migrated %d -> %d", version, self.SCHEMA_VERSION)

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

    def get_fresh_page(self, url: str, *, bypass_cache: bool = False) -> Optional[Dict[str, Any]]:
        """
        Return the cached page for this URL if it is 'done' and within TTL
        (and force_refresh is False), else None. The caller decides whether the
        cached content satisfies the requested mode.
        """
        # ``bypass_cache`` is intentionally per-call.  Agent-facing callers must
        # never flip ``cfg.force_refresh`` on a shared database because that races
        # with other crawls using the same cache.
        if bypass_cache or self.cfg.force_refresh:
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

    def is_fresh(self, url: str, *, bypass_cache: bool = False) -> bool:
        """True if a fresh 'done' page exists for this URL (see get_fresh_page)."""
        return self.get_fresh_page(url, bypass_cache=bypass_cache) is not None

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
        if "links" in data and "links_json" not in data:
            lk = data.get("links")
            data["links_json"] = json.dumps(lk, ensure_ascii=False) if lk else None

        cols = ["url_hash"] + _PAGE_FIELDS
        values = [uh] + [data.get(f) for f in _PAGE_FIELDS]
        placeholders = ",".join("?" for _ in cols)
        updates = ",".join(f"{c}=excluded.{c}" for c in _PAGE_FIELDS)

        with self._lock:
            # Don't let a transient error emission (fetch_error / robots_blocked /
            # no_text) clobber a previously successful crawl: if a 'done' row
            # already exists and the incoming row is an error, preserve the good
            # content instead of wiping raw_text/clean_text/title. A fresh 'done'
            # still overwrites, and error-over-error updates normally.
            if data.get("status") != "done":
                cur = self.conn.execute("SELECT status FROM pages WHERE url_hash=?", (uh,))
                existing = cur.fetchone()
                if existing is not None and existing[0] == "done":
                    return uh
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
            log.debug("FTS index update skipped for %s", uh, exc_info=True)

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
                "SELECT p.*, e.source_url AS source_url, e.depth AS depth FROM pages p "
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
            sql += " LIMIT ?"
            params.append(int(limit))
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
                log.debug("FTS MATCH query failed - falling back to LIKE", exc_info=True)
        # Escape LIKE special characters so '%' and '_' in the query are treated
        # as literals, not wildcards. SQLite LIKE is case-insensitive for ASCII.
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{escaped}%"
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM pages WHERE status='done' AND "
                "(title LIKE ? ESCAPE '\\' OR clean_text LIKE ? ESCAPE '\\') "
                "ORDER BY crawled_at DESC LIMIT ?",
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

    def get_crawl_graph(self, session_id: str, *, limit: int = 200) -> Dict[str, Any]:
        """Bounded node/edge view of one crawl session's persisted provenance."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT p.url, p.status, e.source_url, e.depth "
                "FROM crawl_edges e JOIN pages p ON p.url_hash=e.url_hash "
                "WHERE e.session_id=? ORDER BY e.depth, p.url LIMIT ?",
                (session_id, max(1, int(limit))),
            ).fetchall()
        nodes, edges = [], []
        for row in rows:
            d = dict(row)
            nodes.append({"url": d["url"], "depth": d["depth"], "status": d["status"]})
            if d.get("source_url"):
                edges.append(
                    {"source_url": d["source_url"], "target_url": d["url"], "depth": d["depth"]}
                )
        return {"session_id": session_id, "nodes": nodes, "edges": edges}

    @staticmethod
    def _row_to_page(row: Dict[str, Any]) -> Dict[str, Any]:
        """Deserialize *_json fields into Python lists for the caller's convenience."""
        for src, dst in (("entities_json", "entities"), ("topics_json", "topics")):
            raw = row.get(src)
            if raw:
                try:
                    row[dst] = json.loads(raw)
                except Exception:
                    log.debug("could not deserialize %s for a page row", src)
                    row[dst] = []
            else:
                row[dst] = []
        ej = row.get("extract_json")
        if ej:
            try:
                row["data"] = json.loads(ej)
            except Exception:
                log.debug("could not deserialize extract_json for a page row")
                row["data"] = None
        else:
            row["data"] = None
        lj = row.get("links_json")
        if lj:
            try:
                row["links"] = json.loads(lj)
            except Exception:
                log.debug("could not deserialize links_json for a page row")
                row["links"] = []
        else:
            row["links"] = []
        return row

    # -- Artifacts ------------------------------------------------------------

    def add_artifacts(self, url_hash: str, artifacts: List[Any]) -> int:
        """
        Persist a page's artifacts (tables/images/figures/charts/svg). Idempotent
        per (url_hash, content_hash). ``artifacts`` is a list of ``Artifact``
        objects (or dicts with the same keys). Returns the number inserted.
        """
        if not artifacts:
            return 0
        rows = []
        for a in artifacts:
            d = a.model_dump() if hasattr(a, "model_dump") else dict(a)
            blob = getattr(a, "blob", None) if hasattr(a, "blob") else d.get("blob")
            rows.append(
                (
                    url_hash,
                    int(d.get("position") or 0),
                    d.get("artifact_type"),
                    d.get("src_url"),
                    d.get("alt"),
                    d.get("caption"),
                    d.get("context"),
                    d.get("content"),
                    d.get("content_format"),
                    json.dumps(d.get("data"), ensure_ascii=False)
                    if d.get("data") is not None
                    else None,
                    d.get("summary"),
                    d.get("mime"),
                    d.get("width"),
                    d.get("height"),
                    d.get("bytes_hash"),
                    d.get("size_bytes"),
                    blob,
                    json.dumps(d.get("meta"), ensure_ascii=False) if d.get("meta") else None,
                    d.get("content_hash"),
                    utc_now_iso(),
                )
            )
        with self._lock:
            cur = self.conn.executemany(
                "INSERT OR IGNORE INTO artifacts ("
                "url_hash, position, artifact_type, src_url, alt, caption, context, content, "
                "content_format, data_json, summary, mime, width, height, bytes_hash, size_bytes, "
                "blob, meta_json, content_hash, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            self.conn.commit()
            return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    def get_artifacts(
        self,
        url_hash: Optional[str] = None,
        session_id: Optional[str] = None,
        artifact_type: Optional[str] = None,
        include_blob: bool = False,
        limit: int = 0,
    ) -> List[Dict[str, Any]]:
        """Artifacts for a page (url_hash) or a whole session, optionally by type.

        ``blob`` (raw image bytes) is dropped unless ``include_blob=True``.
        """
        params: List[Any] = []
        if session_id:
            sql = (
                "SELECT a.* FROM artifacts a "
                "JOIN crawl_edges e ON e.url_hash = a.url_hash WHERE e.session_id=?"
            )
            params.append(session_id)
        else:
            sql = "SELECT a.* FROM artifacts a WHERE 1=1"
            if url_hash:
                sql += " AND a.url_hash=?"
                params.append(url_hash)
        if artifact_type:
            sql += " AND a.artifact_type=?"
            params.append(artifact_type)
        sql += " ORDER BY a.url_hash, a.position"
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if not include_blob:
                d.pop("blob", None)
            for src, dst in (("data_json", "data"), ("meta_json", "meta")):
                raw = d.pop(src, None)
                if raw:
                    try:
                        d[dst] = json.loads(raw)
                    except Exception:
                        d[dst] = None
                else:
                    d[dst] = None
            out.append(d)
        return out

    def close(self) -> None:
        self.conn.close()
        self._finalizer.detach()

    def __enter__(self) -> "CrawlerDB":
        return self

    def __exit__(self, *exc) -> bool:
        self.close()
        return False
