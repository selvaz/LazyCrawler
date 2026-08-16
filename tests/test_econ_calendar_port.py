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
    attesi = {
        "__init__",
        "agente_release",
        "arricchisci_giornata",
        "domini_bloccati",
        "fonti_locali",
        "render_report",
        "report_giornata",
        "run_week",
        "sintesi",
    }
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
        assert "C:/Users" not in f.read_text(encoding="utf-8"), (
            f"{f.name} contiene ancora un percorso assoluto di questa macchina"
        )


def test_intra_package_imports_are_relative():
    """They were flat -- `from fonti_locali import ...` -- which only resolved
    because the file's own directory had been put on the path."""
    interni = {
        "agente_release",
        "arricchisci_giornata",
        "domini_bloccati",
        "fonti_locali",
        "render_report",
        "report_giornata",
        "sintesi",
    }
    for f in SORGENTI:
        for nodo in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(nodo, ast.ImportFrom) and nodo.module in interni:
                assert nodo.level > 0, f"{f.name}: `from {nodo.module} import ...` e' ancora piatto"


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
    for nome in (
        "fonti_locali",
        "render_report",
        "report_giornata",
        "arricchisci_giornata",
        "domini_bloccati",
    ):
        modulo = importlib.import_module(f"econ_calendar.{nome}")
        albero = ast.parse(Path(modulo.__file__).read_text(encoding="utf-8"))
        for nodo in albero.body:  # solo il livello di modulo
            if isinstance(nodo, (ast.Import, ast.ImportFrom)):
                sorgente = ast.unparse(nodo)
                assert "lazybridge" not in sorgente, (
                    f"{nome} importa lazybridge a livello di modulo: {sorgente}"
                )


def test_the_tools_do_not_ask_the_model_for_configuration():
    """The tools the agent calls take only what the model can decide. When the
    news directory became an argument, the naive fix would have exposed it in
    the tool schema -- asking a language model to guess a filesystem path."""
    testo = (PKG / "agente_release.py").read_text(encoding="utf-8")
    albero = ast.parse(testo)
    for nodo in ast.walk(albero):
        if isinstance(nodo, ast.FunctionDef) and nodo.name in ("_leggi_digest", "_cerca_articoli"):
            nomi = {a.arg for a in nodo.args.args}
            assert "news_dir" not in nomi, (
                f"{nodo.name} espone news_dir allo schema dello strumento"
            )


# ---------------------------------------------------------------------------
# The four defects the review found in the ported code. They were in the
# original files, not introduced by the port, and each is pinned here.
# ---------------------------------------------------------------------------


def test_runs_are_chosen_by_coverage_date_not_by_global_recency(tmp_path):
    """`runs[:2]` counted runs, not days.

    The crawl does three cycles a day, so two runs could be two cycles of the
    same day -- and on a backfill, both from day+2, which is the run least
    likely to mention a release from two days earlier. The release day then
    contributed nothing and the agent fell through to a web search or a false
    `not_found`.
    """
    from econ_calendar.fonti_locali import _primi_giorni, runs_for

    for stamp in (
        "20260813_070000",
        "20260813_130000",
        "20260813_230000",
        "20260814_070000",
        "20260815_070000",
    ):
        (tmp_path / f"news_full_news_{stamp}_global.md").write_text("x", encoding="utf-8")

    runs = runs_for("2026-08-13", tmp_path)
    assert runs == sorted(runs), "i run devono essere cronologici, non dal piu' recente"
    assert runs[0].startswith("20260813"), "il giorno del rilascio deve venire per primo"

    scelti = _primi_giorni(runs)
    giorni = {r.split("_")[0] for r in scelti}
    assert giorni == {"20260813", "20260814"}, (
        "devono essere il giorno del rilascio e il successivo, non i due run piu' recenti"
    )
    assert len(scelti) == 4, "tutti i cicli di quei due giorni, non due run"


def test_save_returns_the_enrichment_it_persisted():
    """The caller used to call `salvage` a second time to get the payload back.

    That is a second nondeterministic model call, paid for, which could also
    return nothing and raise *after* the row was inserted -- counting the event
    failed while its note sat persisted, so later runs skipped it.
    """
    import ast

    fonte = (PKG / "arricchisci_giornata.py").read_text(encoding="utf-8")
    albero = ast.parse(fonte)
    save = next(n for n in ast.walk(albero) if isinstance(n, ast.FunctionDef) and n.name == "save")
    ritorni = [ast.unparse(n.value) for n in ast.walk(save) if isinstance(n, ast.Return)]
    # ast.unparse renders `return via, p` as `(via, p)`
    assert "(via, p)" in ritorni, "save non restituisce piu' il payload insieme al verdetto"

    enrich = next(
        n for n in ast.walk(albero) if isinstance(n, ast.FunctionDef) and n.name == "enrich_day"
    )
    corpo = ast.unparse(enrich)
    assert "salvage(result)[0]" not in corpo, "enrich_day rifa' salvage per il payload"


def test_the_fallback_agent_is_actually_reachable():
    """It was defined and referenced nowhere.

    The module documents it as the last resort for exactly the case where the
    primary agent delivers nothing usable -- and that case skipped the release
    instead.
    """
    import ast

    fonte = (PKG / "arricchisci_giornata.py").read_text(encoding="utf-8")
    assert "fallback_agent" in fonte, "il fallback non e' raggiungibile da nessuna parte"
    albero = ast.parse(fonte)
    enrich = next(
        n for n in ast.walk(albero) if isinstance(n, ast.FunctionDef) and n.name == "enrich_day"
    )
    chiamate = {ast.unparse(n.func) for n in ast.walk(enrich) if isinstance(n, ast.Call)}
    assert "fallback_agent" in chiamate, "enrich_day non invoca il fallback"
    # and its output must be distinguishable from a reviewed one
    assert "not reviewed" in ast.unparse(enrich).lower() or "FALLBACK" in ast.unparse(enrich)


def test_the_tier_filter_is_rendered_once():
    """Two elements shared id="filters"; the script binds through
    getElementById, so the second bar was visible and did nothing."""
    fonte = (PKG / "render_report.py").read_text(encoding="utf-8")
    corpo = fonte.split('return f"""<!doctype html>')[1]
    assert corpo.count("{filtri}") == 1, "la barra dei filtri e' interpolata piu' di una volta"
    # counted in the emitted markup, not in the file: the comment explaining
    # this defect names the id too, and a test that counts prose is a test that
    # goes red when someone documents something.
    assert corpo.count('id="filters"') == 0, "l'id e' cablato nel template invece che nella barra"
