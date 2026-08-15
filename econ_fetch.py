# -*- coding: utf-8 -*-
"""econ_fetch.py -- normalized fetchers for the 3 official-source APIs backing
econ_indicators.py: BLS (no key needed, <=25 calls/day), BEA (BEA_API_KEY
required), Census (CENSUS_API_KEY required).

Each fetch_* returns a list of Observation, newest period first. A fetch
failure raises EconFetchError with a message specific enough to act on
(missing key, unknown series/table/category, network error) -- the caller
(run_econ_monitor.py) catches per-indicator so one bad indicator doesn't
abort the whole run.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from typing import Optional

import requests

from econ_indicators import EconIndicator

BLS_BASE = "https://api.bls.gov/publicAPI/v1/timeseries/data/"
BEA_BASE = "https://apps.bea.gov/api/data/"
CENSUS_BASE = "https://api.census.gov/data/timeseries/eits/"

_TIMEOUT = 20


def _get_json(url: str, params: dict, what: str):
    """One request, one parsed body, and every failure as EconFetchError.

    The module promises that a failure raises EconFetchError, and the monitor
    relies on that promise for per-indicator isolation: it catches
    EconFetchError so one source going down cannot take the rest of the run
    with it. `raise_for_status()` raises `requests.HTTPError`, a timeout raises
    `requests.Timeout`, and a truncated body raises a JSON error -- none of
    which are EconFetchError, so the isolation was not there. A DNS blip on the
    first indicator aborted the whole monitor, and the report never got built.

    The message keeps the exception type, because "BLS CUUR0000SA0:
    ConnectTimeout" and "BLS CUUR0000SA0: 503" call for different reactions.
    """
    try:
        resp = requests.get(url, params=params, timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as exc:
        raise EconFetchError(f"{what}: {type(exc).__name__}: {exc}") from exc
    except ValueError as exc:  # json.JSONDecodeError subclasses ValueError
        raise EconFetchError(f"{what}: response was not JSON: {exc}") from exc


class EconFetchError(RuntimeError):
    """A fetch failed in a way the caller should see and log, not swallow."""


@dataclass(frozen=True)
class Observation:
    period: str  # source's own period label, e.g. "2026-06", "2026Q2"
    period_date: date  # that period's first day -- for ordering/diffing
    value: float
    is_preliminary: bool = False


# -- BLS ----------------------------------------------------------------------


def _bls_period_to_date(year: int, period_code: str) -> date:
    """BLS period codes: M01-M12 monthly, Q01-Q04 quarterly, A01 annual."""
    if period_code.startswith("M"):
        return date(year, int(period_code[1:]), 1)
    if period_code.startswith("Q"):
        return date(year, (int(period_code[1:]) - 1) * 3 + 1, 1)
    return date(year, 1, 1)


def fetch_bls(series_id: str) -> list[Observation]:
    """BLS public API v1 -- no key needed at this indicator set's call volume
    (25 unauthenticated queries/day; this pipeline makes 5/day)."""
    data = _get_json(BLS_BASE + series_id, {"latest": "true"}, f"BLS {series_id}")
    if data.get("status") != "REQUEST_SUCCEEDED":
        raise EconFetchError(f"BLS {series_id}: {data.get('message') or data.get('status')}")
    series = data.get("Results", {}).get("series") or []
    if not series or not series[0].get("data"):
        raise EconFetchError(f"BLS {series_id}: no data returned")
    out = []
    for row in series[0]["data"]:
        if row.get("value") in (None, "-"):
            continue  # e.g. the 2025 appropriations-lapse publication gap
        out.append(
            Observation(
                period=f"{row['year']}-{row['period']}",
                period_date=_bls_period_to_date(int(row["year"]), row["period"]),
                value=float(row["value"]),
                is_preliminary=any(
                    fn.get("code") in ("P", "R") for fn in (row.get("footnotes") or []) if fn
                ),
            )
        )
    out.sort(key=lambda o: o.period_date, reverse=True)
    return out


# -- BEA ------------------------------------------------------------------------


def _bea_period_to_date(time_period: str) -> date:
    year = int(time_period[:4])
    if "Q" in time_period:
        q = int(time_period.split("Q")[1])
        return date(year, (q - 1) * 3 + 1, 1)
    if "M" in time_period:
        m = int(time_period.split("M")[1])
        return date(year, m, 1)
    return date(year, 1, 1)


def fetch_bea(
    dataset: str, table_name: str, frequency: str, *, line_description: Optional[str] = None
) -> list[Observation]:
    api_key = os.environ.get("BEA_API_KEY")
    if not api_key:
        raise EconFetchError("BEA_API_KEY is not set")
    params = {
        "UserID": api_key,
        "method": "GetData",
        "datasetname": dataset,
        "TableName": table_name,
        "Frequency": frequency,
        "Year": "ALL",
        "ResultFormat": "JSON",
    }
    payload = _get_json(BEA_BASE, params, f"BEA {dataset}/{table_name}")
    results = payload.get("BEAAPI", {}).get("Results", {})
    if isinstance(results, dict) and "Error" in results:
        raise EconFetchError(f"BEA {dataset}/{table_name}: {results['Error']}")
    rows = results.get("Data") or []
    if not rows:
        raise EconFetchError(f"BEA {dataset}/{table_name}: no data returned")
    if line_description:
        wanted = line_description.strip().lower()
        matched = [r for r in rows if (r.get("LineDescription") or "").strip().lower() == wanted]
        if matched:
            rows = matched
    out = []
    for row in rows:
        tp = row.get("TimePeriod")
        if not tp:
            continue
        try:
            value = float(str(row["DataValue"]).replace(",", ""))
        except (KeyError, ValueError, TypeError):
            continue
        out.append(Observation(period=tp, period_date=_bea_period_to_date(tp), value=value))
    if not out:
        raise EconFetchError(f"BEA {dataset}/{table_name}: rows returned but none parsed cleanly")
    out.sort(key=lambda o: o.period_date, reverse=True)
    return out


# -- Census EITS ------------------------------------------------------------------


def fetch_census(
    program: str,
    category_code: str,
    data_type_code: str,
    seasonally_adj: str,
    *,
    lookback_years: int = 3,
) -> list[Observation]:
    api_key = os.environ.get("CENSUS_API_KEY")
    if not api_key:
        raise EconFetchError("CENSUS_API_KEY is not set")
    start_year = date.today().year - lookback_years
    params = {
        "get": "cell_value,time_slot_date,error_data",
        "category_code": category_code,
        "data_type_code": data_type_code,
        "seasonally_adj": seasonally_adj,
        "time": f"from+{start_year}",
        "key": api_key,
    }
    what = f"Census {program} ({category_code}/{data_type_code})"
    try:
        resp = requests.get(CENSUS_BASE + program, params=params, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        raise EconFetchError(f"{what}: {type(exc).__name__}: {exc}") from exc
    # Its own status branch, kept: the body says which code combination is wrong,
    # which a bare status line does not.
    if resp.status_code != 200:
        raise EconFetchError(f"{what}: HTTP {resp.status_code} -- {resp.text[:300]}")
    try:
        rows = resp.json()
    except ValueError as exc:
        raise EconFetchError(f"{what}: response was not JSON: {exc}") from exc
    if not rows or len(rows) < 2:
        raise EconFetchError(
            f"Census {program} ({category_code}/{data_type_code}): no data returned -- "
            "the category_code/data_type_code combination may be wrong, run "
            "`python run_econ_monitor.py --diagnose-census` to list valid combinations"
        )
    header, *data_rows = rows
    idx = {name: i for i, name in enumerate(header)}
    out = []
    for r in data_rows:
        if idx.get("error_data") is not None and r[idx["error_data"]] == "yes":
            continue
        try:
            value = float(str(r[idx["cell_value"]]).replace(",", ""))
            period_date = date.fromisoformat(r[idx["time_slot_date"]][:10])
        except (KeyError, ValueError, IndexError):
            continue
        out.append(
            Observation(period=period_date.isoformat()[:7], period_date=period_date, value=value)
        )
    if not out:
        raise EconFetchError(
            f"Census {program} ({category_code}/{data_type_code}): rows returned but none parsed"
        )
    out.sort(key=lambda o: o.period_date, reverse=True)
    return out


def diagnose_census(
    program: str, seasonally_adj: str = "yes", lookback_years: int = 3
) -> list[tuple[str, str]]:
    """List every distinct (category_code, data_type_code) pair Census
    actually returns for ``program`` -- run this once with a real
    CENSUS_API_KEY to confirm/correct a guessed indicator code (see
    econ_indicators.py's module docstring for the current known gap:
    housing_starts' category_code)."""
    api_key = os.environ.get("CENSUS_API_KEY")
    if not api_key:
        raise EconFetchError("CENSUS_API_KEY is not set")
    start_year = date.today().year - lookback_years
    params = {
        "get": "category_code,data_type_code,time_slot_name",
        "seasonally_adj": seasonally_adj,
        "time": f"from+{start_year}",
        "key": api_key,
    }
    rows = _get_json(CENSUS_BASE + program, params, f"Census {program}")
    header, *data_rows = rows
    idx = {name: i for i, name in enumerate(header)}
    pairs = {(r[idx["category_code"]], r[idx["data_type_code"]]) for r in data_rows}
    return sorted(pairs)


# -- dispatcher ----------------------------------------------------------------


def fetch_latest(indicator: EconIndicator) -> list[Observation]:
    p = indicator.fetch
    if indicator.source == "bls":
        return fetch_bls(p["series_id"])
    if indicator.source == "bea":
        return fetch_bea(
            p["dataset"],
            p["table_name"],
            p["frequency"],
            line_description=p.get("line_description"),
        )
    if indicator.source == "census":
        return fetch_census(
            p["program"], p["category_code"], p["data_type_code"], p["seasonally_adj"]
        )
    raise EconFetchError(f"unknown source {indicator.source!r} for indicator {indicator.key!r}")
