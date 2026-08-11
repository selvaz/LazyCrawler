# -*- coding: utf-8 -*-
"""DataSpace adapter for this repository's crawl cache.

Makes LazyCrawler registrable in a :class:`lazydataspace.DataSpace` so a
workflow spanning several repositories can verify every source's readiness
together, before its first write.

Deliberately thin: no second path resolver and no second search API. The
path comes from :func:`lazycrawler.config.resolve_news_db_path` — the one
place that decides which cache DB a caller should use — and callers reach
the corpus through :class:`lazycrawler.db.CrawlerDB` exactly as they do
today. Registering this Source changes nothing about how the repo works
standalone.

``lazydataspace`` is an optional dependency (``pip install
lazycrawler[lazydataspace]``). Nothing else in this package imports this
module, so the repo installs and runs without it.

Example:
    from lazydataspace import DataSpace
    from lazycrawler.dataspace_source import CrawlerSource

    space = DataSpace(CrawlerSource())
    space.require_ready()
    hits = space.source("crawler").search("inflation expectations")
"""

from __future__ import annotations

import os
import sqlite3
from typing import Any, Dict, List, Optional

from lazydataspace import Health, SourceInfo

from lazycrawler.config import resolve_news_db_path
from lazycrawler.db import sqlite_ro_uri

#: What this endpoint offers. ``documents.search`` is the capability the
#: cleanup plan expects a crawler source to declare: it is backed by the
#: FTS5 index this repo already maintains, not by anything new.
CAPABILITIES = (
    "documents.search",
    "documents.get",
    "artifacts",
)

#: Presence of this table distinguishes "a readable SQLite file" from
#: "actually this repository's crawl cache" — being pointed at the wrong
#: database is the misconfiguration worth catching.
_SENTINEL_TABLE = "pages"

#: Columns the advertised search path actually touches: ``search_text``'s
#: LIKE fallback filters and orders on these, and the FTS join keys on
#: ``url_hash``. A table merely *named* pages is not identity — ready must
#: mean ``search()`` can actually run against this file.
_SENTINEL_COLUMNS = frozenset({"url_hash", "domain", "status", "title", "clean_text", "crawled_at"})


class CrawlerSource:
    """This repository's crawl cache, as a DataSpace ``Source``.

    Satisfies the ``lazydataspace.Source`` protocol structurally — no base
    class to inherit.

    Args:
        db_path: Explicit database path. Omit to use this repo's own
            resolution order (``LAZYCRAWLER_NEWS_DB``), the same one
            ``CrawlerTools`` and the crawl scripts use.
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = db_path

    @property
    def name(self) -> str:
        return "crawler"

    @property
    def owner(self) -> str:
        return "lazycrawler"

    @property
    def capabilities(self) -> tuple[str, ...]:
        return CAPABILITIES

    def describe(self) -> SourceInfo:
        """Return the non-sensitive self-description.

        Carries no path: ``SourceInfo`` has no field for one, and the
        description is written to be safe in a log.
        """
        return SourceInfo(
            name=self.name,
            owner=self.owner,
            capabilities=self.capabilities,
            description=(
                "Crawled page corpus with full-text search (SQLite/FTS5), "
                "crawl session graph and an optional artifact catalog. "
                "Read via lazycrawler.db.CrawlerDB."
            ),
        )

    def health(self) -> Health:
        """Resolve the cache path, open it read-only and confirm it is ours.

        A real check: it resolves, opens and queries. Three distinct
        failures are reported rather than collapsed into one:

        - nothing configured (the resolver returns ``None``);
        - configured but the file is absent;
        - readable but without the sentinel table (wrong database).

        Opens with ``mode=ro`` so a readiness probe can never create an
        empty cache and then report it ready.

        Failure details name the configuration knob but never its value:
        this report is logged, and SQLite errors quote the full path.
        """
        try:
            path = resolve_news_db_path(self._db_path)
        except Exception as exc:
            return Health(ready=False, detail=f"path resolution raised {type(exc).__name__}")

        if not path:
            return Health(
                ready=False, detail="no cache database configured (set LAZYCRAWLER_NEWS_DB)"
            )

        if path != ":memory:" and not os.path.exists(path):
            return Health(
                ready=False, detail="cache database does not exist (check LAZYCRAWLER_NEWS_DB)"
            )

        try:
            # mode=ro refuses to create; a missing file raises rather than
            # producing an empty database that would look healthy. The URI
            # helper percent-encodes the path so a filename containing ? or
            # # cannot be reparsed as URI query syntax.
            con = sqlite3.connect(sqlite_ro_uri(path), uri=True)
            try:
                row = con.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (_SENTINEL_TABLE,),
                ).fetchone()
                cols = {
                    r[1] for r in con.execute(f"PRAGMA table_info({_SENTINEL_TABLE})").fetchall()
                }
            finally:
                con.close()
        except Exception as exc:
            # Type only: SQLite errors quote the full file path.
            return Health(ready=False, detail=f"cannot open cache database: {type(exc).__name__}")

        if row is None:
            return Health(
                ready=False,
                detail=f"database is readable but has no {_SENTINEL_TABLE} table (wrong file?)",
            )
        # Table name alone is not identity: a foreign SQLite file with any
        # table called pages would pass, and the first search() would then
        # fail on the columns it filters and orders on.
        missing = _SENTINEL_COLUMNS - cols
        if missing:
            return Health(
                ready=False,
                detail=(
                    f"{_SENTINEL_TABLE} exists but lacks required column(s) "
                    f"{sorted(missing)} (legacy or foreign schema?)"
                ),
            )
        return Health(ready=True)

    def search(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Full-text search over the crawled corpus.

        The ``documents.search`` capability, delegated verbatim to
        :meth:`lazycrawler.db.CrawlerDB.search_text` — this adapter adds no
        query logic of its own and owns no second index.

        Args:
            query: FTS5 query string.
            limit: Maximum rows to return.

        Raises:
            RuntimeError: No cache database is configured. Explicit rather
                than returning an empty list, which would be
                indistinguishable from "no matches".
        """
        # The package's own public surface, not a submodule path.
        from lazycrawler import CrawlerDB, DBConfig

        path = resolve_news_db_path(self._db_path)
        if not path:
            raise RuntimeError("no cache database configured (set LAZYCRAWLER_NEWS_DB)")
        # read_only: a query-only consumer must match what health() promised.
        # The default constructor performs writes (WAL pragma, DDL,
        # migrations), which fails on a cache mounted read-only and could
        # mint an empty database at a mistyped path.
        db = CrawlerDB(DBConfig(db_path=path, read_only=True))
        try:
            return db.search_text(query, limit=limit)
        finally:
            db.close()


__all__ = ["CAPABILITIES", "CrawlerSource"]
