# -*- coding: utf-8 -*-
"""
setup_paths.py — make LazyCrawler usable in Spyder (and standalone scripts).

The package is not pip-installed, so before importing it you must put it (and
LazyBridge, for smart mode / tools) on sys.path and load the API keys.

USE IN SPYDER — run this file once at the start of a session, or set it as the
console startup file (Preferences > IPython console > Startup > Run a file):

    runfile(r"D:\\LazyCrawler\\setup_paths.py")

or from any script / test:

    import setup_paths            # noqa: F401  (side effects: paths + .env)

After it runs:  from lazycrawler import WebCrawler, CrawlerTools   # just works
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# ── 1. LazyCrawler repo root = the folder containing this file ────────────────
try:
    LAZYCRAWLER_ROOT = Path(__file__).resolve().parent
except NameError:  # pasted into a console where __file__ is undefined
    LAZYCRAWLER_ROOT = Path(r"D:\LazyCrawler")

if str(LAZYCRAWLER_ROOT) not in sys.path:
    sys.path.insert(0, str(LAZYCRAWLER_ROOT))

# ── 2. LazyBridge (needed only for smart mode / CrawlerTools.as_tools) ────────
# Try, in order: env override, the ecosystem location, a few sensible guesses.
_LAZYBRIDGE_CANDIDATES = [
    os.environ.get("LAZYBRIDGE_PATH", ""),
    r"D:\serious_tests\ecosystemv0.9.1\LazyBridge",
    str(LAZYCRAWLER_ROOT.parent / "LazyBridge"),
    str(LAZYCRAWLER_ROOT.parent / "ecosystemv0.9.1" / "LazyBridge"),
]
_ECOSYSTEM_ROOT = Path(r"D:\serious_tests\ecosystemv0.9.1")
for _cand in _LAZYBRIDGE_CANDIDATES:
    if _cand and (Path(_cand) / "lazybridge").is_dir():
        if _cand not in sys.path:
            sys.path.insert(0, _cand)
        _ECOSYSTEM_ROOT = Path(_cand).parent
        break

# ── 3. Load .env (API keys) — ecosystem first, then a local one ───────────────
for _env in [_ECOSYSTEM_ROOT / ".env", LAZYCRAWLER_ROOT / ".env"]:
    if _env.exists():
        for _line in _env.read_text(encoding="utf-8", errors="ignore").splitlines():
            _line = _line.strip()
            if "=" in _line and not _line.startswith("#"):
                _k, _, _v = _line.partition("=")
                os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))
        break


def status() -> None:
    """Print what's importable and which API keys are set."""
    import importlib.util
    lc = importlib.util.find_spec("lazycrawler") is not None
    lb = importlib.util.find_spec("lazybridge") is not None
    print(f"[setup_paths] lazycrawler importable : {lc}  ({LAZYCRAWLER_ROOT})")
    print(f"[setup_paths] lazybridge  importable : {lb}")
    keys = [k for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
                         "DEEPSEEK_API_KEY") if os.environ.get(k)]
    print(f"[setup_paths] API keys set           : {', '.join(keys) or 'NONE'}")
    if not lb:
        print("[setup_paths] NOTE: LazyBridge not found - smart mode / tools.as_tools() "
              "will be unavailable. Set LAZYBRIDGE_PATH or edit the candidates above.")


status()
