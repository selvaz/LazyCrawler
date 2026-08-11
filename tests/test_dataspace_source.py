# -*- coding: utf-8 -*-
"""The DataSpace adapter for this repository's crawl cache.

Skipped entirely when ``lazydataspace`` is not installed: it is an optional
extra, and the repo must keep working standalone without it.
"""

from __future__ import annotations

import sqlite3

import pytest

lazydataspace = pytest.importorskip("lazydataspace", reason="optional [lazydataspace] extra")

from lazydataspace import DataSpace, Health, Source, SourceInfo  # noqa: E402

from lazycrawler.dataspace_source import CrawlerSource  # noqa: E402


@pytest.fixture
def real_db(tmp_path):
    """A SQLite file carrying the sentinel table — stands in for a live cache.

    Carries every column the readiness probe requires (the ones
    ``search_text`` filters and orders on), not just any table with the
    right name: a name-only stand-in is exactly the wrong file the probe
    exists to reject.
    """
    path = tmp_path / "crawler.db"
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE pages (url_hash TEXT PRIMARY KEY, domain TEXT, "
        "status TEXT, title TEXT, clean_text TEXT, crawled_at TEXT)"
    )
    con.commit()
    con.close()
    return str(path)


@pytest.fixture(autouse=True)
def _no_ambient_env(monkeypatch):
    """The resolver reads LAZYCRAWLER_NEWS_DB; this machine has it set.

    Without this the "nothing configured" test would silently pass against
    the developer's real cache instead of an unconfigured resolver.
    """
    monkeypatch.delenv("LAZYCRAWLER_NEWS_DB", raising=False)


class TestProtocolConformance:
    def test_satisfies_the_source_protocol(self, real_db):
        assert isinstance(CrawlerSource(real_db), Source)

    def test_identity(self, real_db):
        source = CrawlerSource(real_db)
        assert source.name == "crawler"
        assert source.owner == "lazycrawler"

    def test_registrable_in_a_dataspace(self, real_db):
        space = DataSpace(CrawlerSource(real_db))
        assert space.list() == ["crawler"]

    def test_declares_documents_search(self, real_db):
        """The capability the cleanup plan expects a crawler source to have."""
        assert "documents.search" in CrawlerSource(real_db).capabilities
        space = DataSpace(CrawlerSource(real_db))
        assert space.for_capability("documents.search")


class TestDescribe:
    def test_returns_source_info(self, real_db):
        info = CrawlerSource(real_db).describe()
        assert isinstance(info, SourceInfo)
        assert info.owner == "lazycrawler"

    def test_description_does_not_leak_the_db_path(self, real_db, tmp_path):
        """No filesystem path reaches the description.

        Checked as *paths*, not as characters: a bare ".db" also matches the
        module reference "lazycrawler.db.CrawlerDB" and a bare "/" matches
        "SQLite/FTS5" — both documentation, not deployment information.
        """
        import re

        info = CrawlerSource(real_db).describe()
        assert real_db not in info.description
        assert str(tmp_path) not in info.description
        assert not re.search(r"[A-Za-z]:[\\/]", info.description), "no absolute path"


class TestHealth:
    def test_ready_against_a_real_cache(self, real_db):
        health = CrawlerSource(real_db).health()
        assert isinstance(health, Health)
        assert health.ready is True

    def test_unready_when_nothing_is_configured(self):
        """The resolver returns None rather than a path — a distinct failure."""
        health = CrawlerSource().health()
        assert health.ready is False
        assert "LAZYCRAWLER_NEWS_DB" in health.detail

    def test_unready_when_the_file_is_absent(self, tmp_path):
        health = CrawlerSource(str(tmp_path / "missing.db")).health()
        assert health.ready is False
        assert "does not exist" in health.detail

    def test_absent_database_is_not_created_by_the_check(self, tmp_path):
        """A probe that creates an empty cache would report ready and hand the
        workflow an empty source."""
        missing = tmp_path / "missing.db"
        CrawlerSource(str(missing)).health()
        assert not missing.exists()

    def test_unready_when_the_file_is_not_a_database(self, tmp_path):
        junk = tmp_path / "not-a-db.db"
        junk.write_text("this is not sqlite", encoding="utf-8")
        health = CrawlerSource(str(junk)).health()
        assert health.ready is False
        assert "cannot open" in health.detail

    def test_unready_when_pointed_at_the_wrong_database(self, tmp_path):
        """Readable SQLite without `pages`: right format, wrong file."""
        other = tmp_path / "other.db"
        con = sqlite3.connect(str(other))
        con.execute("CREATE TABLE something_else (x INTEGER)")
        con.commit()
        con.close()
        health = CrawlerSource(str(other)).health()
        assert health.ready is False
        assert "pages" in health.detail

    def test_unready_when_the_sentinel_table_has_a_foreign_schema(self, tmp_path):
        """A table merely *named* pages is not identity: a foreign SQLite
        file would pass a name-only probe and then fail the first search()
        on the columns it filters and orders on."""
        foreign = tmp_path / "foreign.db"
        con = sqlite3.connect(str(foreign))
        con.execute("CREATE TABLE pages (id INTEGER PRIMARY KEY, body TEXT)")
        con.commit()
        con.close()
        health = CrawlerSource(str(foreign)).health()
        assert health.ready is False
        assert "clean_text" in health.detail or "url_hash" in health.detail

    def test_failure_detail_never_contains_the_path(self, tmp_path):
        junk = tmp_path / "secret-location.db"
        junk.write_text("junk", encoding="utf-8")
        detail = CrawlerSource(str(junk)).health().detail
        assert str(junk) not in detail
        assert "secret-location" not in detail

    def test_health_does_not_write(self, real_db):
        before = sqlite3.connect(real_db).execute("SELECT count(*) FROM sqlite_master").fetchone()
        source = CrawlerSource(real_db)
        source.health()
        source.health()
        after = sqlite3.connect(real_db).execute("SELECT count(*) FROM sqlite_master").fetchone()
        assert before == after


class TestSearch:
    def test_search_delegates_to_the_repos_own_fts(self, tmp_path):
        """No second index and no query logic of its own: the adapter hands
        the query to CrawlerDB.search_text and returns what it returns."""
        from lazycrawler import CrawlerDB, DBConfig

        path = str(tmp_path / "corpus.db")
        db = CrawlerDB(DBConfig(db_path=path))
        db.close()

        source = CrawlerSource(path)
        assert source.search("anything") == []  # empty corpus, not an error

    def test_search_without_configuration_raises(self):
        """Explicit, rather than an empty list indistinguishable from
        'no matches'."""
        with pytest.raises(RuntimeError, match="LAZYCRAWLER_NEWS_DB"):
            CrawlerSource().search("query")

    def test_search_does_not_write_to_the_cache(self, tmp_path):
        """search() must match what health() promised: a query-only
        consumer. The default CrawlerDB constructor performs writes (WAL
        pragma, DDL, migrations) that would fail on a cache mounted
        read-only and could mint an empty database at a mistyped path."""
        from lazycrawler import CrawlerDB, DBConfig

        path = str(tmp_path / "corpus.db")
        db = CrawlerDB(DBConfig(db_path=path))
        db.close()

        before = (tmp_path / "corpus.db").stat().st_mtime_ns
        CrawlerSource(path).search("anything")
        assert (tmp_path / "corpus.db").stat().st_mtime_ns == before
        # And a missing file raises instead of being created empty.
        with pytest.raises(sqlite3.OperationalError):
            CrawlerSource(str(tmp_path / "absent.db")).search("anything")
        assert not (tmp_path / "absent.db").exists()


class TestReadinessGate:
    def test_gate_passes_with_a_real_cache(self, real_db):
        DataSpace(CrawlerSource(real_db)).require_ready()

    def test_gate_fails_before_a_workflow_writes(self, tmp_path):
        space = DataSpace(CrawlerSource(str(tmp_path / "missing.db")))
        with pytest.raises(lazydataspace.SourceNotReadyError) as exc:
            space.require_ready()
        assert "crawler" in str(exc.value)


class TestStandaloneIndependence:
    def test_the_package_does_not_import_the_adapter(self):
        """Importing lazycrawler must not require lazydataspace."""
        import ast
        import pathlib

        import lazycrawler

        package_dir = pathlib.Path(lazycrawler.__file__).parent
        importers = []
        for module in package_dir.rglob("*.py"):
            if module.name == "dataspace_source.py":
                continue
            tree = ast.parse(module.read_text(encoding="utf-8", errors="ignore"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    root = node.module.split(".")[0]
                    if root == "lazydataspace" or node.module.endswith("dataspace_source"):
                        importers.append(module.name)
                elif isinstance(node, ast.Import):
                    if any(a.name.split(".")[0] == "lazydataspace" for a in node.names):
                        importers.append(module.name)
        assert not importers, f"these modules would make lazydataspace mandatory: {importers}"
