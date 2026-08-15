"""The job wrappers take their interpreter; they do not choose one.

Each `*_with_telegram.ps1` used to open with

    $Python = 'C:\\ProgramData\\spyder-6\\python.exe'

which is one machine's shared development interpreter, written into a public
repository. The private scheduler that drives these jobs passes `-Python`
already, and the assignment silently outranked it: the caller chose one
interpreter and the wrapper ran another, against a different set of installed
packages, reporting success either way. That is how five daily jobs came to
run production against a checkout under active edit.

So the wrappers now declare `$Python` as a mandatory parameter. Mandatory
rather than defaulted for the same reason `-SourcesConfig` and `-Cycle` are:
a wrong value here does not fail, it produces a plausible result from the
wrong world.
"""

from __future__ import annotations

import pathlib
import re

RADICE = pathlib.Path(__file__).resolve().parent.parent

#: The wrappers a scheduler invokes. Each runs Python steps and so needs to be
#: told which Python.
WRAPPER = [
    "run_news_crawl_with_telegram.ps1",
    "run_digest_delta_with_telegram.ps1",
    "run_econ_monitor_with_telegram.ps1",
]

#: `$Python = '...'` -- an assignment of a literal, which is the shape that
#: overrides a parameter. A `$Python` *use* is fine and expected.
ASSEGNAZIONE = re.compile(r"^\s*\$Python\s*=\s*['\"]", re.MULTILINE)

#: Any absolute Windows path to an interpreter, wherever it appears. Catches
#: the assignment being spelled some other way, and catches a second
#: interpreter smuggled in beside the parameter.
PERCORSO_INTERPRETE = re.compile(r"[A-Za-z]:\\[^'\"\s]*python\.exe", re.IGNORECASE)


def leggi(nome: str) -> str:
    percorso = RADICE / nome
    assert percorso.is_file(), f"atteso il wrapper {nome}"
    return percorso.read_text(encoding="utf-8", errors="replace")


def test_nessun_wrapper_assegna_un_interprete() -> None:
    colpevoli = {n: ASSEGNAZIONE.findall(leggi(n)) for n in WRAPPER}
    colpevoli = {n: v for n, v in colpevoli.items() if v}
    assert not colpevoli, (
        f"questi wrapper assegnano $Python invece di riceverlo: {sorted(colpevoli)}"
    )


def test_nessun_wrapper_nomina_un_interprete_assoluto() -> None:
    colpevoli = {n: PERCORSO_INTERPRETE.findall(leggi(n)) for n in WRAPPER}
    colpevoli = {n: v for n, v in colpevoli.items() if v}
    assert not colpevoli, f"percorsi assoluti a un interprete dentro wrapper pubblici: {colpevoli}"


def test_ogni_wrapper_dichiara_python_obbligatorio() -> None:
    """Declared, and mandatory.

    The check above only says no interpreter is chosen inside. It would still
    pass if `$Python` were simply never mentioned, leaving the wrapper to run
    whatever `python` the PATH happens to offer -- which is the same failure
    wearing different clothes.
    """
    for nome in WRAPPER:
        contenuto = leggi(nome)
        i = contenuto.find("$Python")
        assert i >= 0, f"{nome} non dichiara $Python"
        blocco = contenuto[:i]
        assert "param(" in blocco, f"{nome} usa $Python senza dichiararlo come parametro"
        # The attribute sits on the line above the parameter, so look at the
        # tail of what precedes it rather than at the whole file.
        assert "[Parameter(Mandatory)]" in contenuto[max(0, i - 200) : i], (
            f"{nome} dichiara $Python ma non come obbligatorio"
        )


def test_lo_scheduler_lo_passa_ai_wrapper() -> None:
    """`setup_scheduler.ps1` hands the interpreter on to every task it registers.

    Making the wrappers mandatory without this would register tasks that stop
    at PowerShell parameter binding, noninteractively, with nothing in the log
    -- which this repository has already been bitten by twice, once for
    `-SourcesConfig` and once for the delta report's three parameters.
    """
    contenuto = leggi("setup_scheduler.ps1")
    # The invocations name the wrapper through a variable -- `& '$wrapper'`,
    # `& '$deltaWrapper'` -- so matching on the file name finds nothing and the
    # test passes for the wrong reason. Match the invocation shape instead.
    comandi = [r for r in contenuto.splitlines() if "& '$" in r and "rapper'" in r]
    assert comandi, "nessuna riga di setup_scheduler.ps1 invoca un wrapper"
    senza = [r.strip()[:80] for r in comandi if "-Python" not in r]
    assert not senza, f"queste invocazioni non passano -Python: {senza}"
