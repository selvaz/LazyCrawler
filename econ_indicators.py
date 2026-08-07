# -*- coding: utf-8 -*-
"""econ_indicators.py -- registry of official-source economic indicators
monitored daily for fresh releases.

Scope (v1): only indicators with a genuine free structured API from their
own issuing agency (BLS, BEA, Census) -- see econ_fetch.py. Indicators whose
publisher distributes only via HTML/PDF press releases (ISM, ADP, Conference
Board, University of Michigan, NAR, S&P Case-Shiller) are deliberately out of
scope: each would need a bespoke, brittle press-release parser. FOMC/ECB
decisions are already partially covered by the existing Fed/ECB RSS sources
in news_sources.py.

Tiers reflect documented market-impact evidence (see the research behind
this feature), not a guess:
  1 -- moves markets within minutes / directly drives Fed policy
  2 -- closely watched, real reaction, often an input to tier-1 releases

Every BLS series_id and the BEA/Census table/category codes below were
checked against a live API response or the issuing agency's own published
example queries before being hardcoded here -- except HOUSING_STARTS_CENSUS'
category_code, which could not be verified without a live Census API key
(Census now requires a key for every request, even the metadata/XML
endpoints that used to be key-less). Run
``python run_econ_monitor.py --diagnose-census`` once CENSUS_API_KEY is set
to list every (category_code, data_type_code) pair Census actually returns
for that program/time range, and correct it here if needed.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EconIndicator:
    key: str
    name: str
    tier: int
    agency: str
    source: str  # "bls" | "bea" | "census"
    unit: str
    why_it_matters: str
    fetch: dict  # source-specific params consumed by econ_fetch.fetch_latest


INDICATORS: list[EconIndicator] = [
    EconIndicator(
        key="cpi_headline",
        name="CPI (All Items, SA)",
        tier=1,
        agency="U.S. Bureau of Labor Statistics",
        source="bls",
        unit="index (1982-84=100)",
        why_it_matters=(
            "The most consistently market-moving scheduled release; the primary data "
            "input the FOMC cites for near-term rate-path decisions since the 2022 "
            "tightening cycle."
        ),
        fetch={"series_id": "CUSR0000SA0"},
    ),
    EconIndicator(
        key="cpi_core",
        name="Core CPI (ex food & energy, SA)",
        tier=1,
        agency="U.S. Bureau of Labor Statistics",
        source="bls",
        unit="index (1982-84=100)",
        why_it_matters=(
            "Strips out volatile food/energy prices; the sub-index the Fed and markets "
            "weight most heavily when reading the headline CPI print."
        ),
        fetch={"series_id": "CUSR0000SA0L1E"},
    ),
    EconIndicator(
        key="ppi_final_demand",
        name="PPI Final Demand (SA)",
        tier=2,
        agency="U.S. Bureau of Labor Statistics",
        source="bls",
        unit="index (Nov 2009=100)",
        why_it_matters=(
            "Upstream producer-price pressure; feeds into BEA's PCE price index "
            "calculation, so it's watched as an early read on where PCE inflation is "
            "heading."
        ),
        fetch={"series_id": "WPSFD49207"},
    ),
    EconIndicator(
        key="nonfarm_payrolls",
        name="Total Nonfarm Payrolls (SA)",
        tier=1,
        agency="U.S. Bureau of Labor Statistics",
        source="bls",
        unit="thousands of jobs",
        why_it_matters=(
            "Standard reference point for labor-market health, explicit in FOMC "
            "dual-mandate discussions."
        ),
        fetch={"series_id": "CES0000000001"},
    ),
    EconIndicator(
        key="jolts_openings",
        name="JOLTS Job Openings, Total US (SA)",
        tier=2,
        agency="U.S. Bureau of Labor Statistics",
        source="bls",
        unit="thousands of openings",
        why_it_matters=(
            "Structural labor-market-tightness signal; the job-openings-to-unemployed "
            "ratio is a labor-slack metric cited directly in FOMC communications."
        ),
        fetch={"series_id": "JTS000000000000000JOL"},
    ),
    EconIndicator(
        key="gdp_real_pct_change",
        name="Real GDP, % Change (Advance/2nd/3rd estimate)",
        tier=1,
        agency="U.S. Bureau of Economic Analysis",
        source="bea",
        unit="% change, annualized",
        why_it_matters=(
            "Headline growth/recession signal; each of the 3 successive quarterly "
            "estimates gets its own market reaction."
        ),
        fetch={
            "dataset": "NIPA",
            "table_name": "T10101",
            "frequency": "Q",
            "line_description": "Percent change from preceding period",
        },
    ),
    EconIndicator(
        key="pce_price_index",
        name="PCE Price Index (Monthly)",
        tier=1,
        agency="U.S. Bureau of Economic Analysis",
        source="bea",
        unit="% change",
        why_it_matters=(
            "The Fed's explicitly preferred inflation gauge -- the 2% target is "
            "defined in PCE terms, not CPI."
        ),
        fetch={
            "dataset": "NIPA",
            "table_name": "T20804",
            "frequency": "M",
            "line_description": None,
        },
    ),
    EconIndicator(
        key="retail_sales",
        name="Advance Retail Sales (Retail & Food Services, SA)",
        tier=2,
        agency="U.S. Census Bureau",
        source="census",
        unit="$ millions",
        why_it_matters=(
            "Direct real-time read on consumer spending (~70% of GDP); the 'control "
            "group' sub-measure feeds directly into GDP's personal-consumption "
            "estimate."
        ),
        fetch={
            "program": "marts",
            "category_code": "44X72",
            "data_type_code": "SM",
            "seasonally_adj": "yes",
        },
    ),
    EconIndicator(
        key="durable_goods_core_capex",
        name="Durable Goods New Orders -- Core Capex (Nondefense Capital Goods ex-Aircraft, SA)",
        tier=2,
        agency="U.S. Census Bureau",
        source="census",
        unit="$ millions",
        why_it_matters=(
            "The specific sub-line economists watch as a proxy for business-investment "
            "intentions feeding into GDP -- more informative than the noisy headline "
            "(lumpy aircraft/defense orders)."
        ),
        fetch={
            "program": "mtis",
            "category_code": "NXA",
            "data_type_code": "VS",
            "seasonally_adj": "yes",
        },
    ),
    EconIndicator(
        key="housing_starts",
        name="Housing Starts, Total (SAAR)",
        tier=2,
        agency="U.S. Census Bureau",
        source="census",
        unit="thousands of units, annualized",
        why_it_matters=(
            "Leading indicator for the housing sector and residential-investment "
            "component of GDP; building permits are a component of the Conference "
            "Board's Leading Economic Index."
        ),
        fetch={
            "program": "resconst",
            # UNVERIFIED -- see module docstring. Run --diagnose-census to confirm/fix.
            "category_code": "TOTAL",
            "data_type_code": "R",
            "seasonally_adj": "yes",
        },
    ),
    EconIndicator(
        key="trade_balance_goods_services",
        name="U.S. International Trade in Goods and Services (Balance, SA)",
        tier=2,
        agency="U.S. Census Bureau / BEA (FT-900)",
        source="census",
        unit="$ millions",
        why_it_matters=(
            "Direct GDP-accounting component (net exports); used to nowcast GDP ahead "
            "of BEA's full quarterly release."
        ),
        fetch={
            "program": "ftd",
            "category_code": "BOPGS",
            "data_type_code": "BAL",
            "seasonally_adj": "yes",
        },
    ),
]

BY_KEY: dict[str, EconIndicator] = {i.key: i for i in INDICATORS}
