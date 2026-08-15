"""One indicator failing must not end the run.

`run_econ_monitor.py` catches `EconFetchError` around each indicator, so that a
source going down costs that source and nothing else. The module's docstring
promised that every failure arrives as `EconFetchError`, and it did not: a
timeout arrives as `requests.Timeout`, an HTTP error as `requests.HTTPError`
from `raise_for_status()`, and a truncated body as a JSON error. None of those
are caught, so the first transient failure ended the whole monitor -- every
remaining indicator unfetched and no report written.

These check the promise rather than the wording of it. They are cheap: no
network, no fixtures, just the exceptions `requests` actually raises.
"""

from __future__ import annotations

import json

import pytest
import requests

import econ_fetch


class _Response:
    """Only what the fetchers touch."""

    def __init__(self, *, status: int = 200, payload=None, body: str = ""):
        self.status_code = status
        self._payload = payload
        self.text = body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Server Error")

    def json(self):
        if self._payload is _TRUNCATED:
            raise json.JSONDecodeError("Expecting value", "", 0)
        return self._payload


_TRUNCATED = object()


@pytest.mark.parametrize(
    ("boom", "expected_in_message"),
    [
        (requests.Timeout("timed out"), "Timeout"),
        (requests.ConnectionError("dns failure"), "ConnectionError"),
    ],
    ids=["timeout", "dns"],
)
def test_transport_failures_arrive_as_econfetcherror(monkeypatch, boom, expected_in_message):
    def explode(*args, **kwargs):
        raise boom

    monkeypatch.setattr(econ_fetch.requests, "get", explode)
    with pytest.raises(econ_fetch.EconFetchError) as caught:
        econ_fetch.fetch_bls("CUUR0000SA0")
    message = str(caught.value)
    assert "CUUR0000SA0" in message, "the message does not say which indicator failed"
    assert expected_in_message in message, (
        "the exception type is dropped, and a timeout and a 503 call for different reactions"
    )


def test_an_http_error_arrives_as_econfetcherror(monkeypatch):
    monkeypatch.setattr(econ_fetch.requests, "get", lambda *a, **k: _Response(status=503))
    with pytest.raises(econ_fetch.EconFetchError):
        econ_fetch.fetch_bls("CUUR0000SA0")


def test_a_body_that_is_not_json_arrives_as_econfetcherror(monkeypatch):
    monkeypatch.setattr(econ_fetch.requests, "get", lambda *a, **k: _Response(payload=_TRUNCATED))
    with pytest.raises(econ_fetch.EconFetchError) as caught:
        econ_fetch.fetch_bls("CUUR0000SA0")
    assert "not JSON" in str(caught.value)


def test_every_fetcher_keeps_the_promise(monkeypatch):
    """Not just BLS. The isolation is worth nothing if one source leaks."""

    def explode(*args, **kwargs):
        raise requests.Timeout("timed out")

    monkeypatch.setattr(econ_fetch.requests, "get", explode)
    monkeypatch.setenv("BEA_API_KEY", "x")
    monkeypatch.setenv("CENSUS_API_KEY", "x")

    calls = [
        lambda: econ_fetch.fetch_bls("CUUR0000SA0"),
        lambda: econ_fetch.fetch_bea("NIPA", "T10101", "Q"),
    ]
    for call in calls:
        with pytest.raises(econ_fetch.EconFetchError):
            call()


def test_no_module_imports_the_standalone_engine_package():
    """The Claude Code engine lives in LazyBridge now.

    `lazybridge_claude_code` is a separate package that was never declared in
    pyproject, requirements or the first-run verification: a machine set up by
    the documented path reached that import on every default digest and got
    ModuleNotFoundError. It is also a stale copy -- the engine moved into
    LazyBridge, which is declared, and whose class takes a superset of the
    same arguments.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    offenders = []
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            if any(n.startswith("lazybridge_claude_code") for n in names):
                offenders.append(path.name)
    assert not offenders, (
        "these import the standalone engine package, which is undeclared and "
        f"superseded by lazybridge: {sorted(set(offenders))}"
    )
