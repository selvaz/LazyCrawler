# -*- coding: utf-8 -*-
"""The economic-calendar enrichment, ported out of a scratchpad.

Nine files arrived here copied verbatim from a session temp directory. Their
own README listed what had to change before any of it could run; these tests
pin each of those items, so none of them can come back quietly.
"""
from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1] / "econ_calendar"
SORGENTI = sorted(PKG.glob("*.py"))


def test_the_package_exists_and_has_every_file():
    assert SORGENTI, "il pacchetto econ_calendar non c'e'"
    attesi = {"__init__", "agente_release", "arricchisci_giornata",
              "domini_bloccati", "fonti_locali", "render_report",
              "report_giornata", "run_week", "sintesi"}
    assert {f.stem for f in SORGENTI} == attesi


def test_no_absolute_path_is_inserted_on_import():
    """Four files inserted `C:/Users/Administrator/...` at import time.

    That made the code importable on exactly one machine, and said nothing
    about it anywhere else. Three of the four inserts pointed at
    market-data-hub and were dead: nothing in the package imports it.
    """
    for f in SORGENTI:
        albero = ast.parse(f.read_text(encoding="utf-8"))
        for nodo in ast.walk(albero):
            if not isinstance(nodo, ast.Call):
                continue
            bersaglio = ast.unparse(nodo.func)
            assert bersaglio != "sys.path.insert", f"{f.name} inserisce ancora nel path"
            assert not bersaglio.endswith("path.append"), f"{f.name} appende al path"
        assert "C:/Users" not in f.read_text(encoding="utf-8"), \
            f"{f.name} contiene ancora un percorso assoluto di questa macchina"


def test_intra_package_imports_are_relative():
    """They were flat -- `from fonti_locali import ...` -- which only resolved
    because the file's own directory had been put on the path."""
    interni = {"agente_release", "arricchisci_giornata", "domini_bloccati",
               "fonti_locali", "render_report", "report_giornata", "sintesi"}
    for f in SORGENTI:
        for nodo in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(nodo, ast.ImportFrom) and nodo.module in interni:
                assert nodo.level > 0, \
                    f"{f.name}: `from {nodo.module} import ...` e' ancora piatto"


def test_the_claude_code_engine_comes_from_lazybridge():
    """`lazybridge_claude_code` was the standalone prototype. It was absorbed
    into lazybridge, and importing it here would pin a package that is only
    still importable on this machine by accident."""
    testo = (PKG / "agente_release.py").read_text(encoding="utf-8")
    assert "lazybridge_claude_code" not in testo
    # Asked of the import, not of a line of text: `ruff --fix` merges the
    # lazybridge imports into one statement, and a test matching the literal
    # string went red on a formatting change while the code was correct.
    da_lazybridge = {
        alias.name
        for nodo in ast.walk(ast.parse(testo))
        if isinstance(nodo, ast.ImportFrom) and nodo.module == "lazybridge"
        for alias in nodo.names
    }
    assert "ClaudeCodeEngine" in da_lazybridge


def test_the_news_directory_is_an_argument_everywhere():
    """It was a module constant holding one machine's path."""
    fonti = importlib.import_module("econ_calendar.fonti_locali")
    assert not hasattr(fonti, "NEWS_DIR"), "NEWS_DIR e' tornata una costante"
    for nome in ("read_daily_digest", "search_collected_articles"):
        parametri = inspect.signature(getattr(fonti, nome)).parameters
        assert "news_dir" in parametri, f"{nome} non prende news_dir"

    arric = importlib.import_module("econ_calendar.arricchisci_giornata")
    assert "news_dir" in inspect.signature(arric.enrich_day).parameters


def test_a_missing_news_directory_says_so_instead_of_returning_nothing():
    """The failure that mattered: a wrong directory returned an empty string,
    which reads exactly like a day on which nothing was published."""
    fonti = importlib.import_module("econ_calendar.fonti_locali")
    with pytest.raises(NotADirectoryError):
        fonti.read_daily_digest("2026-08-13", PKG / "non-esiste")


def test_web_search_is_given_a_database_so_pages_persist():
    """`WebSearch()` was built bare: everything it downloaded was discarded
    when the `with` closed, so the same page was fetched again for the next
    release that mentioned it."""
    testo = (PKG / "agente_release.py").read_text(encoding="utf-8")
    assert "WebSearch(db=" in testo, "WebSearch e' di nuovo costruito nudo"
    assert "WebSearch()" not in testo


def test_the_heavy_import_stays_lazy():
    """lazybridge needs Python >= 3.11 and market-data-hub tests on 3.9. The
    rest of the package -- report, HTML, local sources -- must import without
    it, which is the same shape this repository already uses for `smart`."""
    for nome in ("fonti_locali", "render_report", "report_giornata",
                 "arricchisci_giornata", "domini_bloccati"):
        modulo = importlib.import_module(f"econ_calendar.{nome}")
        albero = ast.parse(Path(modulo.__file__).read_text(encoding="utf-8"))
        for nodo in albero.body:          # solo il livello di modulo
            if isinstance(nodo, (ast.Import, ast.ImportFrom)):
                sorgente = ast.unparse(nodo)
                assert "lazybridge" not in sorgente, \
                    f"{nome} importa lazybridge a livello di modulo: {sorgente}"


def test_the_tools_do_not_ask_the_model_for_configuration():
    """The tools the agent calls take only what the model can decide. When the
    news directory became an argument, the naive fix would have exposed it in
    the tool schema -- asking a language model to guess a filesystem path."""
    testo = (PKG / "agente_release.py").read_text(encoding="utf-8")
    albero = ast.parse(testo)
    for nodo in ast.walk(albero):
        if isinstance(nodo, ast.FunctionDef) and nodo.name in ("_leggi_digest",
                                                               "_cerca_articoli"):
            nomi = {a.arg for a in nodo.args.args}
            assert "news_dir" not in nomi, \
                f"{nodo.name} espone news_dir allo schema dello strumento"
