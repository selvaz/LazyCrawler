# -*- coding: utf-8 -*-
"""render_econ_report.py -- standalone CLI proving econ_report.render_html
reconstructs a report byte-for-byte from its saved JSON alone, with no live
API access. Mirrors market-data-hub's render_regime_report.py.

Usage:
    python render_econ_report.py --latest
    python render_econ_report.py --path reports/econ/econ_daily_2026-08-03.json
    python render_econ_report.py --latest --out my_report.html
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from econ_report import render_html  # noqa: E402

ROOT = Path(__file__).resolve().parent
REPORT_DIR = ROOT / "reports" / "econ"


def _latest_json() -> Path | None:
    candidates = sorted(REPORT_DIR.glob("econ_daily_*.json"), key=lambda p: p.stat().st_mtime)
    return candidates[-1] if candidates else None


def main() -> int:
    p = argparse.ArgumentParser(description="Re-render a saved econ_daily_report row as HTML")
    p.add_argument("--latest", action="store_true", help="Use the most recently saved report")
    p.add_argument("--path", help="Path to a specific econ_daily_*.json file")
    p.add_argument("--out", help="Output HTML path (default: same name, .html)")
    args = p.parse_args()

    if args.path:
        json_path = Path(args.path)
    elif args.latest:
        json_path = _latest_json()
        if json_path is None:
            print(f"No econ_daily_*.json files found in {REPORT_DIR}", file=sys.stderr)
            return 1
    else:
        print("Pass --latest or --path <file>", file=sys.stderr)
        return 1

    if not json_path.exists():
        print(f"File not found: {json_path}", file=sys.stderr)
        return 1

    row = json.loads(json_path.read_text(encoding="utf-8"))
    html = render_html(row)

    out_path = Path(args.out) if args.out else json_path.with_suffix(".html")
    out_path.write_text(html, encoding="utf-8")
    print(f"Rendered {json_path} -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
