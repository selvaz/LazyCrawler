"""The source list contract.

The list itself is gone from this repository; what remains is what a source
must look like and what makes a list unusable. The checks are mostly about
the refusals, because those are the failures that would otherwise be silent:
a mistyped region forms a category of one, a repeated feed is counted twice,
and a crawl with no list at all would happily run against someone else's
curation and produce a plausible digest of the wrong world.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from news_sources import FIELDS, SourcesError, load_sources

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "news_sources.example.yaml"


def entry(**overrides):
    base = {"name": "Example Wire", "url": "https://example.invalid/a.xml",
            "category": "financial", "region": "global", "lang": "en", "mode": "ml"}
    base.update(overrides)
    return base


def write(tmp_path: Path, entries) -> Path:
    p = tmp_path / "sources.yaml"
    p.write_text(yaml.safe_dump({"sources": entries}, sort_keys=False), encoding="utf-8")
    return p


class TestALoadedList:
    def test_the_shipped_example_loads(self):
        """A broken example is worse than none: it is what a new user
        copies."""
        sources = load_sources(EXAMPLE)
        assert len(sources) == 3
        assert {s.mode for s in sources} == {"ml", "smart"}

    def test_the_example_names_no_reachable_feed(self):
        """Fictitious on purpose. An example that crawled something real
        would make a first run do something nobody asked for."""
        for source in load_sources(EXAMPLE):
            assert "example.invalid" in source.url

    def test_order_is_preserved(self, tmp_path):
        """Part of the contract: the crawl walks the list in order and the
        digest groups what it finds, so reordering changes what a reader
        sees first."""
        names = ["First", "Second", "Third"]
        path = write(tmp_path, [entry(name=n, url=f"https://example.invalid/{n}.xml")
                                for n in names])
        assert [s.name for s in load_sources(path)] == names

    def test_every_field_is_carried_through(self, tmp_path):
        path = write(tmp_path, [entry(lang="pt", mode="smart", region="latam",
                                      category="geopolitical")])
        source = load_sources(path)[0]
        assert (source.lang, source.mode, source.region, source.category) == (
            "pt", "smart", "latam", "geopolitical")


class TestRefusals:
    @pytest.mark.parametrize("field", FIELDS)
    def test_a_missing_field_is_refused(self, tmp_path, field):
        body = entry()
        del body[field]
        with pytest.raises(SourcesError, match=field):
            load_sources(write(tmp_path, [body]))

    @pytest.mark.parametrize("bad,match", [
        ({"category": "opinion"}, "category"),
        ({"region": "antarctica"}, "region"),
        ({"mode": "clever"}, "mode"),
    ])
    def test_a_value_outside_its_vocabulary_is_refused(self, tmp_path, bad, match):
        """A free-text value cannot be grouped; it quietly forms a category
        of one."""
        with pytest.raises(SourcesError, match=match):
            load_sources(write(tmp_path, [entry(**bad)]))

    def test_an_unknown_field_is_refused(self, tmp_path):
        with pytest.raises(SourcesError, match="unknown field"):
            load_sources(write(tmp_path, [{**entry(), "priority": "high"}]))

    def test_surrounding_whitespace_is_refused_not_trimmed(self, tmp_path):
        """Trimming would make ' CNBC' and 'CNBC' the same source and hide a
        typo in a hand-edited file."""
        with pytest.raises(SourcesError, match="whitespace"):
            load_sources(write(tmp_path, [entry(name=" Example Wire")]))

    def test_a_repeated_name_is_refused(self, tmp_path):
        """Refused rather than de-duplicated: in a hand-edited list a repeat
        is a mistake, and dropping one silently hides which."""
        with pytest.raises(SourcesError, match="repeats the name"):
            load_sources(write(tmp_path, [entry(), entry(url="https://example.invalid/b.xml")]))

    def test_a_repeated_url_is_refused(self, tmp_path):
        with pytest.raises(SourcesError, match="repeats the url"):
            load_sources(write(tmp_path, [entry(), entry(name="Another Wire")]))

    def test_an_empty_list_is_refused(self, tmp_path):
        with pytest.raises(SourcesError, match="non-empty list"):
            load_sources(write(tmp_path, []))

    def test_a_missing_file_names_the_path(self, tmp_path):
        with pytest.raises(SourcesError, match="not found"):
            load_sources(tmp_path / "absent.yaml")

    def test_malformed_yaml_names_the_file(self, tmp_path):
        p = tmp_path / "sources.yaml"
        p.write_text("sources: [unclosed", encoding="utf-8")
        with pytest.raises(SourcesError, match="not valid YAML"):
            load_sources(p)

    def test_a_file_without_a_sources_key_is_refused(self, tmp_path):
        p = tmp_path / "sources.yaml"
        p.write_text(yaml.safe_dump([entry()]), encoding="utf-8")
        with pytest.raises(SourcesError, match="'sources' key"):
            load_sources(p)


class TestTheListIsNoLongerHere:
    def test_the_module_carries_no_source_list(self):
        """The point of the extraction. A leftover list would be a second
        source of truth, and the one nobody remembers to update."""
        import news_sources

        assert not hasattr(news_sources, "SOURCES")

    def test_the_module_names_no_real_feed(self):
        source = (ROOT / "news_sources.py").read_text(encoding="utf-8")
        for host in ("cnbc.com", "ft.com", "reuters", "bloomberg", "ecb.europa.eu"):
            assert host not in source.lower()

    def test_the_crawl_runner_requires_a_list(self):
        """No default and no search path."""
        runner = (ROOT / "run_news_crawl.py").read_text(encoding="utf-8")
        assert "--sources-config" in runner
        assert "required=True" in runner
        assert "from news_sources import load_sources" in runner


class TestOrdinaryImportsAreEnough:
    """No module here rewrites the interpreter's search path.

    The two runners used to insert their own directory into sys.path before
    importing their siblings. Running from the repository root — which is
    what every launcher does — that line was already redundant, and it made
    an import work for reasons a reader could not see from the import.
    """

    @pytest.mark.parametrize("name", ["run_news_crawl", "make_digest_delta_report"])
    def test_the_runner_does_not_touch_sys_path(self, name):
        source = (ROOT / f"{name}.py").read_text(encoding="utf-8")
        assert "sys.path" not in source

    @pytest.mark.parametrize("name", ["run_news_crawl", "make_digest_delta_report"])
    def test_it_still_imports_in_a_fresh_process(self, name):
        """A fresh process, because this session has already imported these
        modules and would prove nothing about a cold start."""
        import subprocess
        import sys

        probe = (
            "import importlib.util, sys\n"
            f"spec = importlib.util.spec_from_file_location('{name}', '{name}.py')\n"
            "m = importlib.util.module_from_spec(spec)\n"
            f"sys.modules['{name}'] = m\n"
            "spec.loader.exec_module(m)\n"
            "print('OK')\n"
        )
        result = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                                text=True, cwd=str(ROOT))
        assert result.returncode == 0, result.stderr
        assert "OK" in result.stdout

    @pytest.mark.parametrize("name", ["run_news_crawl", "make_digest_delta_report"])
    def test_no_noqa_survives_the_removal(self, name):
        """The E402 suppressions existed only because the inserted line sat
        between the imports. Leaving them would silence a real finding
        later."""
        source = (ROOT / f"{name}.py").read_text(encoding="utf-8")
        assert "noqa: E402" not in source
